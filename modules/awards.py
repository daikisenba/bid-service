"""落札実績オープンデータ(調達ポータル)の取得と参考落札相場の計算。

データソース(2026-07-11実測): api.p-portal.go.jp の落札実績オープンデータ。
申請・認証不要、商用利用可(出典表記必須)。年度別全件ZIP内のCSVは
ヘッダー行なし・UTF-8(BOM付き)で、列は以下の順:
  案件ID, 案件名, 落札日, 落札金額, 種別コード, 機関コード, 落札者名, 法人番号

設計メモ: docs/phase2_award_price_design.md
- DBを持たず実行時に必要年度分をダウンロードするステートレス構成
- 種別コード・機関コードは公式コード表を引かず生値のまま保持(MVP割り切り)
- 落札金額は公表値として扱い、税計算等の加工はしない
"""
from __future__ import annotations

import csv
import io
import statistics
import zipfile
from datetime import date

import requests

from .config import Settings
from .models import AwardExample, AwardRecord, Customer, MatchResult, PriceStats

# 出典表記(利用条件)。メール・シートの分かりやすい場所に必ず入れる
AWARD_SOURCE_NOTE = "出典: 調達ポータル(デジタル庁)落札実績オープンデータ"

_COLUMN_COUNT = 8


def current_fiscal_year(today: date | None = None) -> int:
    """日本の年度(4月始まり)。7月なら当年、1〜3月なら前年を返す。"""
    today = today or date.today()
    return today.year if today.month >= 4 else today.year - 1


def _target_fiscal_years(settings: Settings) -> list[int]:
    fy = current_fiscal_year()
    return [fy - i for i in range(max(1, settings.awards.fiscal_year_lookback))]


def _parse_amount(raw: str) -> int | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def _parse_csv(data: bytes) -> list[AwardRecord]:
    text = data.decode("utf-8-sig")
    records: list[AwardRecord] = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < _COLUMN_COUNT:
            continue
        records.append(
            AwardRecord(
                project_id=row[0],
                project_name=row[1],
                award_date=row[2] or None,
                award_amount=_parse_amount(row[3]),
                type_code=row[4] or None,
                org_code=row[5] or None,
                winner_name=row[6] or None,
                corporate_number=row[7] or None,
            )
        )
    return records


def _fetch_fiscal_year(fy: int, settings: Settings) -> list[AwardRecord]:
    filename = f"successful_bid_record_info_all_{fy}.zip"
    resp = requests.get(
        settings.awards.api_base_url,
        params={"fileversion": "v001", "filename": filename},
        timeout=settings.awards.timeout_seconds,
    )
    resp.raise_for_status()
    records: list[AwardRecord] = []
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        for name in z.namelist():
            if name.lower().endswith(".csv"):
                records.extend(_parse_csv(z.read(name)))
    return records


def fetch_awards(settings: Settings) -> list[AwardRecord]:
    """対象年度分の落札実績を取得する。年度単位で失敗しても他年度は続行する
    (新年度直後で当年度ファイルが未生成のケース等を吸収)。"""
    records: list[AwardRecord] = []
    for fy in _target_fiscal_years(settings):
        try:
            records.extend(_fetch_fiscal_year(fy, settings))
        except Exception:  # noqa: BLE001 - 1年度の失敗で全体を止めない
            continue
    return records


def find_comparables(keywords: list[str], records: list[AwardRecord]) -> list[AwardRecord]:
    """案件名にキーワードを含み、落札金額が取得できている落札実績を返す。"""
    kws = [k for k in keywords if k]
    if not kws:
        return []
    return [
        r
        for r in records
        if r.award_amount is not None and any(k in (r.project_name or "") for k in kws)
    ]


def compute_stats(comparables: list[AwardRecord]) -> PriceStats:
    """同種落札の件数・中央値・四分位(p25/p75)・直近実例3件を計算する。"""
    amounts = sorted(r.award_amount for r in comparables if r.award_amount is not None)
    n = len(amounts)
    if n == 0:
        return PriceStats(count=0)

    median = int(statistics.median(amounts))
    p25 = p75 = None
    if n >= 4:  # 四分位が意味を持つ最小サンプル数
        q = statistics.quantiles(amounts, n=4)  # [p25, p50, p75]
        p25, p75 = int(q[0]), int(q[2])

    recent = sorted(comparables, key=lambda r: r.award_date or "", reverse=True)[:3]
    examples = [
        AwardExample(project_name=r.project_name, amount=r.award_amount, winner=r.winner_name)
        for r in recent
    ]
    return PriceStats(count=n, median=median, p25=p25, p75=p75, examples=examples)


def _comparable_keywords(customer: Customer, match: MatchResult) -> list[str]:
    """同種判定に使うキーワード。まず案件名に現れた顧客キーワードを優先し、
    無ければ(公告文のみ一致のケース)顧客キーワード全体で引く。"""
    name = match.listing.project_name or ""
    in_name = [k for k in customer.profile.keywords if k and k in name]
    return in_name or customer.profile.keywords


def attach_price_stats(
    customer: Customer, matches: list[MatchResult], award_records: list[AwardRecord]
) -> None:
    """各マッチに参考落札相場を付与する(スコアには影響させない)。"""
    for match in matches:
        comparables = find_comparables(_comparable_keywords(customer, match), award_records)
        match.price_stats = compute_stats(comparables)
