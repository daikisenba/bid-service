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


def _searchable_text(listing: BidListing) -> str:
    return " ".join(filter(None, [listing.project_name, listing.project_description]))


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
    """1顧客・1案件をスコアリングする。除外キーワード一致・地域/資格等級の
    ハード不一致の場合は None を返す(=候補から除外)。
    """
    text = _searchable_text(listing)
    excluded = _contains_any(text, customer.profile.exclude_keywords)
    if excluded:
        return None

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

    return MatchResult(
        listing=listing,
        customer_id=customer.customer_id,
        score=score,
        reasons=[keyword_reason, region_reason, qualification_reason, price_reason],
        estimated_price=estimated_price,
        price_confirmed=price_confirmed,
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
