from __future__ import annotations

from modules.customer import load_active_customers
from tests.conftest import ADMIN_LOG_HEADERS, MASTER_HEADERS, MASTER_ID, PROFILE_HEADERS
from tests.fakes import FakeGspreadClient, FakeSpreadsheet, FakeWorksheet


def test_load_active_customers_happy_path(fake_gc, settings):
    result = load_active_customers(fake_gc, settings)
    assert {c.customer_id for c in result.customers} == {"C001", "C002", "C003"}
    assert result.skipped == []


def test_paused_customer_is_excluded(fake_gc, settings):
    master_ws = fake_gc.spreadsheets[MASTER_ID].worksheet("顧客マスタ")
    master_ws.rows[0][5] = "paused"  # C001をpausedに変更

    result = load_active_customers(fake_gc, settings)
    ids = {c.customer_id for c in result.customers}
    assert "C001" not in ids
    assert ids == {"C002", "C003"}


def test_missing_profile_row_is_skipped_with_reason(settings):
    master_ws = FakeWorksheet(
        header=list(MASTER_HEADERS),
        rows=[["C999", "行方不明株式会社", "不明太郎", "x@example.jp", "standard", "active", "", "", "SHEET_X"]],
    )
    profile_ws = FakeWorksheet(header=list(PROFILE_HEADERS), rows=[])  # C999のプロファイル行がない
    admin_log_ws = FakeWorksheet(header=list(ADMIN_LOG_HEADERS), rows=[])
    gc = FakeGspreadClient(
        spreadsheets={
            MASTER_ID: FakeSpreadsheet(
                worksheets={"顧客マスタ": master_ws, "条件プロファイル": profile_ws, "実行ログ": admin_log_ws}
            )
        }
    )

    result = load_active_customers(gc, settings)
    assert result.customers == []
    assert len(result.skipped) == 1
    assert result.skipped[0].customer_id == "C999"
    assert "見つかりません" in result.skipped[0].reason


def test_empty_profile_is_skipped_with_reason(settings):
    master_ws = FakeWorksheet(
        header=list(MASTER_HEADERS),
        rows=[["C998", "空条件株式会社", "空条件太郎", "y@example.jp", "standard", "active", "", "", "SHEET_Y"]],
    )
    profile_ws = FakeWorksheet(
        header=list(PROFILE_HEADERS),
        rows=[["C998", "", "", "", "", "", "", ""]],  # キーワード・地域・資格等級すべて空
    )
    admin_log_ws = FakeWorksheet(header=list(ADMIN_LOG_HEADERS), rows=[])
    gc = FakeGspreadClient(
        spreadsheets={
            MASTER_ID: FakeSpreadsheet(
                worksheets={"顧客マスタ": master_ws, "条件プロファイル": profile_ws, "実行ログ": admin_log_ws}
            )
        }
    )

    result = load_active_customers(gc, settings)
    assert result.customers == []
    assert len(result.skipped) == 1
    assert "空です" in result.skipped[0].reason


def test_missing_output_sheet_id_is_skipped(settings):
    master_ws = FakeWorksheet(
        header=list(MASTER_HEADERS),
        rows=[["C997", "出力先未設定株式会社", "未設定太郎", "z@example.jp", "standard", "active", "", "", ""]],
    )
    profile_ws = FakeWorksheet(header=list(PROFILE_HEADERS), rows=[["C997", "消耗品", "", "", "", "", "", ""]])
    admin_log_ws = FakeWorksheet(header=list(ADMIN_LOG_HEADERS), rows=[])
    gc = FakeGspreadClient(
        spreadsheets={
            MASTER_ID: FakeSpreadsheet(
                worksheets={"顧客マスタ": master_ws, "条件プロファイル": profile_ws, "実行ログ": admin_log_ws}
            )
        }
    )

    result = load_active_customers(gc, settings)
    assert result.customers == []
    assert "出力先スプレッドシートID" in result.skipped[0].reason
