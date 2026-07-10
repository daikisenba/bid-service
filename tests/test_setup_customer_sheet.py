from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

from tests.conftest import ADMIN_LOG_HEADERS, MASTER_HEADERS, MASTER_ID, PROFILE_HEADERS, RECOMMEND_HEADERS
from tests.fakes import FakeGspreadClient, FakeSpreadsheet, FakeWorksheet

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "setup_customer_sheet.py"
_spec = importlib.util.spec_from_file_location("setup_customer_sheet", _SCRIPT_PATH)
setup_customer_sheet = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup_customer_sheet)


def _empty_master_gc() -> FakeGspreadClient:
    master_sheet = FakeSpreadsheet(
        worksheets={
            "顧客マスタ": FakeWorksheet(header=list(MASTER_HEADERS), rows=[]),
            "条件プロファイル": FakeWorksheet(header=list(PROFILE_HEADERS), rows=[]),
            "実行ログ": FakeWorksheet(header=list(ADMIN_LOG_HEADERS), rows=[]),
        }
    )
    output_sheet = FakeSpreadsheet(
        worksheets={"レコメンド案件": FakeWorksheet(header=list(RECOMMEND_HEADERS), rows=[])}
    )
    return FakeGspreadClient(spreadsheets={MASTER_ID: master_sheet, "SHEET_C004": output_sheet})


def test_append_master_row_adds_customer_and_profile(settings):
    gc = _empty_master_gc()
    args = argparse.Namespace(
        customer_id="C004",
        company_name="新規株式会社",
        contact_name="田中三郎",
        contact_email="tanaka@example.jp",
        plan="standard",
        status="trial",
        sheet_id="SHEET_C004",
        keywords="消耗品",
        exclude_keywords="",
        prefecture_codes="13",
        price_min="",
        price_max="",
        organization_types="",
        qualification_grades="",
        contract_start="2026-07-10",
        next_billing_date="2026-08-10",
    )

    setup_customer_sheet._append_master_row(gc, settings, args)

    master_rows = gc.spreadsheets[MASTER_ID].worksheet("顧客マスタ").rows
    assert len(master_rows) == 1
    assert master_rows[0][0] == "C004"
    assert master_rows[0][8] == "SHEET_C004"

    profile_rows = gc.spreadsheets[MASTER_ID].worksheet("条件プロファイル").rows
    assert len(profile_rows) == 1
    assert profile_rows[0][0] == "C004"
    assert profile_rows[0][1] == "消耗品"


def test_init_recommend_sheet_writes_headers(settings):
    gc = _empty_master_gc()
    setup_customer_sheet._init_recommend_sheet(gc, "SHEET_C004")

    ws = gc.spreadsheets["SHEET_C004"].worksheet("レコメンド案件")
    assert ws.header == RECOMMEND_HEADERS
