from __future__ import annotations

import io
import zipfile
from datetime import date

from modules.awards import (
    _parse_amount,
    _parse_csv,
    compute_stats,
    current_fiscal_year,
    fetch_awards,
    find_comparables,
)
from modules.models import AwardRecord

# ヘッダー行なし・8列(案件ID,案件名,落札日,落札金額,種別コード,機関コード,落札者名,法人番号)
_SAMPLE_CSV = (
    "0001,消耗品の購入,2026-07-09,120000.00,S5,8002010,某商事,5370001003340\n"
    "0002,庁舎清掃業務,2026-07-08,880000.00,P1,8002010,清掃社,4010001115346\n"
    "0003,文具一式,2026-07-07,,S1,8002010,,\n"  # 落札金額欠損
)


def test_current_fiscal_year_april_boundary():
    assert current_fiscal_year(date(2026, 7, 1)) == 2026
    assert current_fiscal_year(date(2026, 3, 31)) == 2025
    assert current_fiscal_year(date(2026, 4, 1)) == 2026


def test_parse_amount():
    assert _parse_amount("21777000.00") == 21777000
    assert _parse_amount("") is None
    assert _parse_amount("要問合せ") is None


def test_parse_csv_skips_short_rows_and_parses_fields():
    records = _parse_csv(_SAMPLE_CSV.encode("utf-8-sig"))
    assert len(records) == 3
    assert records[0].project_name == "消耗品の購入"
    assert records[0].award_amount == 120000
    assert records[0].type_code == "S5"  # コードは生値のまま保持
    assert records[2].award_amount is None  # 欠損は None


def test_find_comparables_matches_name_and_requires_amount():
    records = _parse_csv(_SAMPLE_CSV.encode("utf-8-sig"))
    comps = find_comparables(["消耗品"], records)
    assert len(comps) == 1
    assert comps[0].project_name == "消耗品の購入"
    # 金額欠損の「文具一式」はキーワード一致しても相場計算対象外
    assert find_comparables(["文具"], records) == []


def test_find_comparables_empty_keywords():
    records = _parse_csv(_SAMPLE_CSV.encode("utf-8-sig"))
    assert find_comparables([], records) == []


def _award(amount: int, name: str = "消耗品", d: str = "2026-06-01") -> AwardRecord:
    return AwardRecord(project_id="x", project_name=name, award_date=d, award_amount=amount, winner_name="社")


def test_compute_stats_zero():
    stats = compute_stats([])
    assert stats.count == 0
    assert stats.median is None
    assert stats.examples == []


def test_compute_stats_single_has_median_no_quartiles():
    stats = compute_stats([_award(100000)])
    assert stats.count == 1
    assert stats.median == 100000
    assert stats.p25 is None and stats.p75 is None


def test_compute_stats_quartiles_with_four_or_more():
    stats = compute_stats([_award(a) for a in [100000, 200000, 300000, 400000]])
    assert stats.count == 4
    assert stats.median == 250000
    assert stats.p25 is not None and stats.p75 is not None
    assert stats.p25 < stats.median < stats.p75


def test_compute_stats_examples_are_recent_first_and_capped_at_three():
    records = [
        _award(100000, d="2026-01-01"),
        _award(200000, d="2026-05-01"),
        _award(300000, d="2026-03-01"),
        _award(400000, d="2026-06-01"),
    ]
    stats = compute_stats(records)
    assert len(stats.examples) == 3
    assert stats.examples[0].amount == 400000  # 2026-06-01 が最新


def _zip_bytes(csv_text: str, inner_name: str = "successful_bid_record_info_all_2026.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(inner_name, csv_text.encode("utf-8-sig"))
    return buf.getvalue()


def test_fetch_awards_downloads_and_parses(monkeypatch, settings):
    settings.awards.fiscal_year_lookback = 1

    class FakeResponse:
        content = _zip_bytes(_SAMPLE_CSV)

        def raise_for_status(self):
            pass

    monkeypatch.setattr("modules.awards.requests.get", lambda url, params=None, timeout=None: FakeResponse())
    records = fetch_awards(settings)
    assert len(records) == 3
    assert records[0].project_name == "消耗品の購入"


def test_fetch_awards_survives_year_failure(monkeypatch, settings):
    settings.awards.fiscal_year_lookback = 2

    def boom(url, params=None, timeout=None):
        raise RuntimeError("network down")

    monkeypatch.setattr("modules.awards.requests.get", boom)
    # 全年度失敗しても例外を投げず空リストを返す(本体を止めないため)
    assert fetch_awards(settings) == []
