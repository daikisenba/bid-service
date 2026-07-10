from __future__ import annotations

import pytest

from modules.config import (
    CompanySettings,
    EmailSettings,
    GoogleSettings,
    MatchingSettings,
    MatchingWeights,
    SearchSettings,
    Settings,
)
from tests.fakes import FakeGspreadClient, FakeSpreadsheet, FakeWorksheet

MASTER_ID = "MASTER_ID"

MASTER_HEADERS = [
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
PROFILE_HEADERS = [
    "customer_id",
    "対象業種・品目キーワード",
    "除外キーワード",
    "対象地域",
    "予定価格下限",
    "予定価格上限",
    "発注機関の種別",
    "資格等級",
]
ADMIN_LOG_HEADERS = ["実行日時", "処理顧客数", "スキップ顧客数", "総マッチ件数", "エラー件数", "詳細"]
RECOMMEND_HEADERS = [
    "案件名",
    "発注機関",
    "公告日",
    "締切日",
    "予定価格",
    "案件URL",
    "マッチ度スコア",
    "レコメンド理由",
    "ステータス",
]


@pytest.fixture
def settings() -> Settings:
    """フィクスチャ専用の設定。実際のconfig/settings.yamlとは独立させ、
    本番設定の変更でテストが壊れないようにする(本番ファイル自体の妥当性は
    test_config.pyで別途検証する)。
    """
    return Settings(
        google=GoogleSettings(
            customer_master_sheet_id=MASTER_ID,
            customer_master_tab="顧客マスタ",
            profile_tab="条件プロファイル",
            admin_log_tab="実行ログ",
        ),
        search=SearchSettings(
            api_base_url="https://www.kkj.go.jp/api/",
            lookback_days=7,
            max_count=1000,
            timeout_seconds=20,
        ),
        matching=MatchingSettings(
            score_threshold=60,
            weights=MatchingWeights(keyword=50, region=20, qualification=20, price=10),
            price_regex_patterns=["予定価格[^0-9]{0,10}([0-9,]+)\\s*円"],
        ),
        email=EmailSettings(
            admin_address="admin@example.jp",
            from_address="admin@example.jp",
            smtp_host="smtp.example.jp",
            smtp_port=587,
        ),
        company=CompanySettings(name="テスト株式会社"),
    )


@pytest.fixture
def dummy_master_rows() -> list[list[object]]:
    return [
        ["C001", "サンプル商事株式会社", "佐藤一郎", "sato@example.jp", "standard", "active", "2026-04-01", "2026-08-01", "SHEET_C001"],
        ["C002", "テスト工業株式会社", "鈴木花子", "suzuki@example.jp", "premium", "active", "2026-05-01", "2026-08-01", "SHEET_C002"],
        ["C003", "サンプル物産株式会社", "高橋次郎", "takahashi@example.jp", "standard", "active", "2026-06-01", "2026-08-01", "SHEET_C003"],
    ]


@pytest.fixture
def dummy_profile_rows() -> list[list[object]]:
    return [
        ["C001", "消耗品,印刷,封筒", "工事,保守", "13,14", "10000", "1000000", "国,独法", "C,D"],
        ["C002", "防災,衛生用品", "", "27", "", "", "", ""],
        ["C003", "文房具", "", "01", "", "", "", ""],
    ]


@pytest.fixture
def fake_gc(dummy_master_rows, dummy_profile_rows) -> FakeGspreadClient:
    master_ws = FakeWorksheet(header=list(MASTER_HEADERS), rows=[list(r) for r in dummy_master_rows])
    profile_ws = FakeWorksheet(header=list(PROFILE_HEADERS), rows=[list(r) for r in dummy_profile_rows])
    admin_log_ws = FakeWorksheet(header=list(ADMIN_LOG_HEADERS), rows=[])

    master_sheet = FakeSpreadsheet(
        worksheets={
            "顧客マスタ": master_ws,
            "条件プロファイル": profile_ws,
            "実行ログ": admin_log_ws,
        }
    )

    output_sheets = {
        sheet_id: FakeSpreadsheet(worksheets={"レコメンド案件": FakeWorksheet(header=list(RECOMMEND_HEADERS), rows=[])})
        for sheet_id in ("SHEET_C001", "SHEET_C002", "SHEET_C003")
    }

    return FakeGspreadClient(spreadsheets={MASTER_ID: master_sheet, **output_sheets})
