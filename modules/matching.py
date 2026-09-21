"""顧客条件プロファイルと案件プールのマッチング・スコアリング。

既存のnyusatsu-searchスキルの判定思想(地域要件・資格等級は最優先のハード条件、
不明な項目は断定せず「要確認」として減点しない)を、顧客ごとに設定可能な
ルールとして一般化したもの。

kkj.go.jp API は「予定価格」を構造化データとして提供しないため、価格判定は
ProjectDescription からの正規表現ベストエフォート抽出に留まる。抽出できない
場合はスコアに加点も減点もせず「要確認」として reasons に明記する。
"""
from __future__ import annotations

import re

from .config import Settings
from .models import BidListing, Customer, MatchResult

_FULL = 1.0
_HALF = 0.5
_NONE = 0.0


def _contains_any(haystack: str, needles: list[str]) -> list[str]:
    lowered = haystack.lower()
    return [n for n in needles if n and n.lower() in lowered]


def _exclusion_text(listing: BidListing) -> str:
    """除外キーワードの判定対象は案件名のみ(公告文は見ない)。

    公告文まで見ると除外が効きすぎて破綻する。官公庁の公告文には、物品調達の
    案件であっても「工事」「調査」「委託」といった語がほぼ必ずどこかに出現する
    ため(入札心得・関連法令・提出先部署名など)、公告文を対象にすると除外語を
    1つ入れただけで正しい案件まで大量に巻き添えで消える。
    実測(防災系キーワード・30日分1,000件): 公告文まで対象にすると
    ヒット1,000件→99件まで落ち、案件名一致の良質な案件も124件→33件に減った。

    一方、実際に混入するノイズ(「〜点検整備業務」「〜工事」「〜業務委託」
    「〜の売却」)は案件名そのものに現れる。案件名だけを見れば、狙ったものだけを
    落とせる。
    """
    return listing.project_name or ""


def _extract_price(listing: BidListing, patterns: list[str]) -> int | None:
    text = listing.project_description or ""
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            digits = m.group(1).replace(",", "")
            if digits.isdigit():
                return int(digits)
    return None


def _keyword_component(customer: Customer, listing: BidListing) -> tuple[float, str]:
    """案件名一致は満点、公告文のみの一致は半分。

    kkj.go.jp APIの全文検索は公告文・添付由来の過剰マッチを多く含む
    (実測: 「シュレッダー」で4,694件ヒットするが上位案件名は無関係)。
    案件名に現れるキーワードが商品性の実体であるため、公告文のみの一致は
    参考扱いに格下げする。
    """
    name_matched = _contains_any(listing.project_name or "", customer.profile.keywords)
    if name_matched:
        return _FULL, f"キーワード一致(案件名): {', '.join(name_matched)}"
    desc_matched = _contains_any(listing.project_description or "", customer.profile.keywords)
    if desc_matched:
        return _HALF, f"キーワード一致(公告文のみ・要確認): {', '.join(desc_matched)}"
    return _NONE, "対象キーワード不一致"


def _region_component(customer: Customer, listing: BidListing) -> tuple[float, str] | None:
    """戻り値が None の場合はハード除外(対象地域外)。"""
    target_codes = customer.profile.prefecture_codes
    if not target_codes:
        return _FULL, "対象地域指定なし"
    if listing.lg_code is None:
        return _HALF, "地域情報が取得できず要確認"
    if listing.lg_code in target_codes:
        return _FULL, f"対象地域内({listing.prefecture_name or listing.lg_code})"
    return None


def _qualification_component(customer: Customer, listing: BidListing) -> tuple[float, str] | None:
    """戻り値が None の場合はハード除外(資格等級不一致)。"""
    target_grades = customer.profile.qualification_grades
    if not target_grades:
        return _FULL, "資格等級指定なし"
    if not listing.certification:
        return _HALF, "資格等級情報が取得できず要確認"
    if set(listing.certification) & set(target_grades):
        return _FULL, f"資格等級一致({'/'.join(listing.certification)})"
    return None


def _price_component(
    customer: Customer, listing: BidListing, patterns: list[str]
) -> tuple[float, str, int | None, bool]:
    price_min = customer.profile.price_min
    price_max = customer.profile.price_max
    if price_min is None and price_max is None:
        return _FULL, "価格レンジ指定なし", None, False

    extracted = _extract_price(listing, patterns)
    if extracted is None:
        return _HALF, "予定価格が公告文から取得できず要確認", None, False

    lower_ok = price_min is None or extracted >= price_min
    upper_ok = price_max is None or extracted <= price_max
    if lower_ok and upper_ok:
        return _FULL, f"予定価格レンジ内(¥{extracted:,})", extracted, True
    return _NONE, f"予定価格レンジ外(¥{extracted:,})", extracted, True


def score_listing(customer: Customer, listing: BidListing, settings: Settings) -> MatchResult | None:
    """1顧客・1案件をスコアリングする。地域/資格等級のハード不一致の場合は
    None を返す(=候補から除外)。

    除外キーワード一致は2026-09-21〜ハード除外しない(最終判定はLLMに委ねる)。
    実測(30日・防災系キーワード)で、除外キーワードにより76件が機械的に消えており、
    その中に「防災備蓄倉庫整備業務」のような本物の物品購入案件が誤って巻き添えに
    なっている疑いがあったため。一致有無は exclude_keywords_matched に記録し、
    modules/llm_judge.py のプロンプトへ渡すシグナルとして使う。LLM判定が失敗した
    場合はfail-openでこの案件も通す(=旧来の「除外しない」動作と同じになる)。
    """
    exclude_keywords_matched = _contains_any(_exclusion_text(listing), customer.profile.exclude_keywords)

    region = _region_component(customer, listing)
    if region is None:
        return None
    qualification = _qualification_component(customer, listing)
    if qualification is None:
        return None

    weights = settings.matching.weights
    keyword_mult, keyword_reason = _keyword_component(customer, listing)
    region_mult, region_reason = region
    qualification_mult, qualification_reason = qualification
    price_mult, price_reason, estimated_price, price_confirmed = _price_component(
        customer, listing, settings.matching.price_regex_patterns
    )

    score = round(
        keyword_mult * weights.keyword
        + region_mult * weights.region
        + qualification_mult * weights.qualification
        + price_mult * weights.price
    )

    reasons = [keyword_reason, region_reason, qualification_reason, price_reason]
    if exclude_keywords_matched:
        reasons.append(f"除外キーワード一致(案件名・要LLM確認): {', '.join(exclude_keywords_matched)}")

    return MatchResult(
        listing=listing,
        customer_id=customer.customer_id,
        score=score,
        reasons=reasons,
        estimated_price=estimated_price,
        price_confirmed=price_confirmed,
        exclude_keywords_matched=exclude_keywords_matched,
    )


def match_customer(
    customer: Customer, listings: list[BidListing], settings: Settings
) -> list[MatchResult]:
    """1顧客に対するマッチング結果を、閾値以上・スコア降順・上位N件で返す。

    上位N件(max_recommendations_per_run)に切るのは、レコメンドの価値が
    絞り込みにあるため。プールが顧客キーワードのOR検索で作られる以上、
    閾値だけでは初回実行時などに数百件が通過してしまう。
    """
    results = [score_listing(customer, listing, settings) for listing in listings]
    filtered = [r for r in results if r is not None and r.score >= settings.matching.score_threshold]
    filtered.sort(key=lambda r: r.score, reverse=True)
    return filtered[: settings.matching.max_recommendations_per_run]
