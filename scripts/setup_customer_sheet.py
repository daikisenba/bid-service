"""顧客追加セットアップスクリプト。

事前に手動で行っておくこと(このスクリプトはGoogle Drive上にファイルを
新規作成しない。Drive MCPの読み取り専用制約と同様、サービスアカウントによる
ファイル作成はストレージ容量の問題を起こしやすいため、作成は人が行う):

  1. Google Sheetsで顧客専用の空スプレッドシートを新規作成する
  2. その共有設定で、サービスアカウントのclient_email
     (GOOGLE_SERVICE_ACCOUNT_JSON 内の "client_email" の値)を
     「編集者」として追加する

このスクリプトが自動で行うこと:

  1. 手順1で作成したシートに「レコメンド案件」タブを用意し、ヘッダー行と
     ステータス列のドロップダウン入力規則を設定する
  2. 顧客マスタの「顧客マスタ」「条件プロファイル」各タブに、この顧客の
     行を追記する

実行例:

  python scripts/setup_customer_sheet.py \\
      --customer-id C004 --company-name "株式会社サンプル" \\
      --contact-name "山田太郎" --contact-email yamada@example.co.jp \\
      --plan standard --sheet-id <手順1で作成したシートのID> \\
      --keywords "消耗品,印刷,文具" --exclude-keywords "工事,保守" \\
      --prefecture-codes "13,14,11,12" --qualification-grades "C,D" \\
      --price-min 10000 --price-max 1000000 --organization-types "国,独法"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gspread  # noqa: E402
from gspread.utils import ValidationConditionType  # noqa: E402

from modules.auth import build_gspread_client  # noqa: E402
from modules.config import load_settings  # noqa: E402
from modules.delivery import RECOMMEND_HEADERS, RECOMMEND_TAB  # noqa: E402

_STATUS_OPTIONS = ["未確認", "検討中", "応札", "見送り"]
_MASTER_HEADERS = [
    "customer_id",
    "会社名",
    "担当者名",
    "メールアドレス",
    "プラン",
    "ステータス",
    "契約開始日",
    "次回請求日",
    "出力先スプレッドシートID",
]
_PROFILE_HEADERS = [
    "customer_id",
    "対象業種・品目キーワード",
    "除外キーワード",
    "対象地域",
    "予定価格下限",
    "予定価格上限",
    "発注機関の種別",
    "資格等級",
]
_ADMIN_LOG_HEADERS = ["実行日時", "処理顧客数", "スキップ顧客数", "総マッチ件数", "エラー件数", "詳細"]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="顧客追加セットアップスクリプト")
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--customer-id", required=True)
    parser.add_argument("--company-name", required=True)
    parser.add_argument("--contact-name", required=True)
    parser.add_argument("--contact-email", required=True)
    parser.add_argument("--plan", choices=["standard", "premium"], default="standard")
    parser.add_argument("--status", choices=["active", "paused", "trial"], default="trial")
    parser.add_argument("--sheet-id", required=True, help="手動作成・共有済みの顧客専用シートID")
    parser.add_argument("--keywords", default="", help="カンマ区切り")
    parser.add_argument("--exclude-keywords", default="", help="カンマ区切り")
    parser.add_argument("--prefecture-codes", default="", help="カンマ区切り(JIS X0401)")
    parser.add_argument("--price-min", default="")
    parser.add_argument("--price-max", default="")
    parser.add_argument("--organization-types", default="", help="カンマ区切り: 国/都道府県/市区町村/独法")
    parser.add_argument("--qualification-grades", default="", help="カンマ区切り: A/B/C/D")
    parser.add_argument("--contract-start", default="")
    parser.add_argument("--next-billing-date", default="")
    return parser.parse_args()


def _init_recommend_sheet(gc: gspread.Client, sheet_id: str) -> None:
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(RECOMMEND_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=RECOMMEND_TAB, rows=1000, cols=len(RECOMMEND_HEADERS))

    ws.update([RECOMMEND_HEADERS], "A1")
    ws.freeze(rows=1)

    status_col = RECOMMEND_HEADERS.index("ステータス") + 1
    status_range = f"{gspread.utils.rowcol_to_a1(2, status_col)}:{gspread.utils.rowcol_to_a1(1000, status_col)}"
    ws.add_validation(
        status_range,
        ValidationConditionType.one_of_list,
        _STATUS_OPTIONS,
        showCustomUi=True,
    )


def _ensure_headers(ws: gspread.Worksheet, headers: list[str]) -> None:
    if not ws.row_values(1):
        ws.update([headers], "A1")
        ws.freeze(rows=1)


def _append_master_row(gc: gspread.Client, settings, args: argparse.Namespace) -> None:
    sh = gc.open_by_key(settings.google.customer_master_sheet_id)

    master_ws = sh.worksheet(settings.google.customer_master_tab)
    _ensure_headers(master_ws, _MASTER_HEADERS)
    master_ws.append_row(
        [
            args.customer_id,
            args.company_name,
            args.contact_name,
            args.contact_email,
            args.plan,
            args.status,
            args.contract_start,
            args.next_billing_date,
            args.sheet_id,
        ],
        value_input_option="USER_ENTERED",
    )

    profile_ws = sh.worksheet(settings.google.profile_tab)
    _ensure_headers(profile_ws, _PROFILE_HEADERS)
    profile_ws.append_row(
        [
            args.customer_id,
            args.keywords,
            args.exclude_keywords,
            args.prefecture_codes,
            args.price_min,
            args.price_max,
            args.organization_types,
            args.qualification_grades,
        ],
        value_input_option="USER_ENTERED",
    )

    admin_log_ws = sh.worksheet(settings.google.admin_log_tab)
    _ensure_headers(admin_log_ws, _ADMIN_LOG_HEADERS)


def main() -> int:
    args = _parse_args()
    settings = load_settings(args.config)
    gc = build_gspread_client()

    _init_recommend_sheet(gc, args.sheet_id)
    _append_master_row(gc, settings, args)

    print(f"顧客 {args.customer_id} ({args.company_name}) を追加しました。")
    print(f"  出力先シート: https://docs.google.com/spreadsheets/d/{args.sheet_id}")
    print(f"  ステータス: {args.status} (activeにするとバッチ処理対象になります)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
