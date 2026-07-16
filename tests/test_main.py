"""main.run() のE2Eテスト(ダミー顧客3社)。

受け入れ基準1「ダミー顧客3社に対し、日次バッチが正常完走する」と、
受け入れ基準4「1社の処理でエラーが発生しても、残りの顧客の処理が継続する」を、
実際のGoogle Sheets/Gmail/kkj.go.jp APIに接続せず検証する。

本物のGoogle API・SMTPサーバーに対する実接続確認(受け入れ基準の最終確認)は
README記載の手順に従い、実際の認証情報を用意したうえで手動実行すること。
"""
from __future__ import annotations

import main
from modules.models import AwardRecord, BidListing


def _candidate_pool() -> list[BidListing]:
    return [
        BidListing(
            result_id="1",
            key="k1",
            external_document_uri="https://example.jp/A",
            project_name="消耗品(文具)の購入",
            lg_code="13",
            prefecture_name="東京都",
            organization_name="某省",
            certification=["C"],
            cft_issue_date="2026-07-01",
            period_end_time="2026-07-20",
            project_description="予定価格 120,000円",
        ),
        BidListing(
            result_id="2",
            key="k2",
            external_document_uri="https://example.jp/B",
            project_name="防災用品の調達",
            lg_code="27",
            organization_name="大阪府",
            cft_issue_date="2026-07-02",
            period_end_time="2026-07-25",
        ),
        BidListing(
            result_id="3",
            key="k3",
            external_document_uri="https://example.jp/C",
            project_name="庁舎改修工事",
            lg_code="13",
            certification=["C"],
        ),
    ]


class _FakeSMTP:
    sent: list = []

    def __init__(self, host, port):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg):
        _FakeSMTP.sent.append(msg)


def _patch_common(monkeypatch, settings, fake_gc, candidate_pool, award_records=None):
    monkeypatch.setattr("main.load_settings", lambda path: settings)
    monkeypatch.setattr("main.build_gspread_client", lambda: fake_gc)
    monkeypatch.setattr("main.smtp_credentials", lambda: ("smtp_user", "smtp_pass"))
    monkeypatch.setattr("main.fetch_candidate_pool", lambda customers, settings: candidate_pool)
    # 落札実績取得はネットワークを避けてスタブ化する(既定は空=相場データなし)
    monkeypatch.setattr("main.fetch_awards", lambda settings: award_records or [])
    _FakeSMTP.sent = []
    monkeypatch.setattr("modules.delivery.smtplib.SMTP", _FakeSMTP)


def test_daily_batch_completes_for_three_dummy_customers(monkeypatch, settings, fake_gc):
    _patch_common(monkeypatch, settings, fake_gc, _candidate_pool())

    exit_code = main.run()

    assert exit_code == 0

    c001_rows = fake_gc.spreadsheets["SHEET_C001"].worksheet("レコメンド案件").rows
    assert len(c001_rows) == 1
    assert c001_rows[0][0] == "消耗品(文具)の購入"
    # 除外キーワード「工事」により庁舎改修工事はC001のシートに入らない
    assert all("庁舎改修工事" != row[0] for row in c001_rows)

    c002_rows = fake_gc.spreadsheets["SHEET_C002"].worksheet("レコメンド案件").rows
    assert len(c002_rows) == 1
    assert c002_rows[0][0] == "防災用品の調達"

    c003_rows = fake_gc.spreadsheets["SHEET_C003"].worksheet("レコメンド案件").rows
    assert c003_rows == []  # マッチなし

    # 新着ありのC001・C002分のみメールが生成される(顧客への自動送信ではなく管理者宛)
    assert len(_FakeSMTP.sent) == 2
    assert all(msg["To"] == settings.email.admin_address for msg in _FakeSMTP.sent)

    admin_log_rows = fake_gc.spreadsheets["MASTER_ID"].worksheet("実行ログ").rows
    assert len(admin_log_rows) == 1
    assert admin_log_rows[0][1] == 3  # 処理顧客数
    assert admin_log_rows[0][2] == 0  # スキップ顧客数
    assert admin_log_rows[0][4] == 0  # エラー件数


def test_second_run_does_not_duplicate_rows(monkeypatch, settings, fake_gc):
    pool = _candidate_pool()
    _patch_common(monkeypatch, settings, fake_gc, pool)

    main.run()
    main.run()

    c001_rows = fake_gc.spreadsheets["SHEET_C001"].worksheet("レコメンド案件").rows
    assert len(c001_rows) == 1  # 2回実行しても重複追記されない

    admin_log_rows = fake_gc.spreadsheets["MASTER_ID"].worksheet("実行ログ").rows
    assert len(admin_log_rows) == 2  # 実行ログは毎回追記される


def test_one_customer_failure_does_not_stop_others(monkeypatch, settings, fake_gc):
    # C001の出力先シートIDを、fake_gcに存在しないIDへ差し替えて実行時エラーを発生させる。
    master_ws = fake_gc.spreadsheets["MASTER_ID"].worksheet("顧客マスタ")
    master_ws.rows[0][8] = "SHEET_DOES_NOT_EXIST"

    _patch_common(monkeypatch, settings, fake_gc, _candidate_pool())

    exit_code = main.run()

    assert exit_code == 1  # エラーが発生したことはCIに見えるようにする

    # C001は失敗するが、C002・C003の処理は続行される
    c002_rows = fake_gc.spreadsheets["SHEET_C002"].worksheet("レコメンド案件").rows
    assert len(c002_rows) == 1

    admin_log_rows = fake_gc.spreadsheets["MASTER_ID"].worksheet("実行ログ").rows
    assert admin_log_rows[0][1] == 2  # 処理顧客数(C002・C003)
    assert admin_log_rows[0][4] == 1  # エラー件数(C001)
    assert "C001" in admin_log_rows[0][5]


def test_smtp_check_succeeds_without_touching_sheets_or_matching(monkeypatch, settings):
    # --smtp-check はGoogle Sheets/案件探索に触れず、SMTP接続+認証のみを検証する
    monkeypatch.setattr("main.load_settings", lambda path: settings)
    monkeypatch.setattr("main.smtp_credentials", lambda: ("smtp_user", "smtp_pass"))
    monkeypatch.setattr("main.check_smtp_login", lambda settings, user, password: None)

    exit_code = main.run_smtp_check()

    assert exit_code == 0


def test_smtp_check_fails_when_login_raises(monkeypatch, settings):
    def _raise(settings, user, password):
        raise RuntimeError("SMTP認証に失敗しました(535 BadCredentials)。")

    monkeypatch.setattr("main.load_settings", lambda path: settings)
    monkeypatch.setattr("main.smtp_credentials", lambda: ("smtp_user", "smtp_pass"))
    monkeypatch.setattr("main.check_smtp_login", _raise)

    exit_code = main.run_smtp_check()

    assert exit_code == 1


def test_cli_smtp_check_flag_routes_to_run_smtp_check(monkeypatch):
    monkeypatch.setattr("sys.argv", ["main.py", "--smtp-check"])
    monkeypatch.setattr("main.run_smtp_check", lambda config: 0)
    monkeypatch.setattr("main.run", lambda config: (_ for _ in ()).throw(AssertionError("runが呼ばれてはいけない")))

    assert main.main() == 0


def test_award_stats_flow_into_customer_sheet(monkeypatch, settings, fake_gc):
    """落札実績データが顧客シートの参考落札相場列に反映されることを確認する。"""
    from tests.conftest import AWARD_COL_INDEX

    # C001の「消耗品」に一致する過去落札を4件用意(四分位が出る最小件数)
    awards = [
        AwardRecord(
            project_id=str(i),
            project_name="消耗品の調達",
            award_date=f"2026-06-0{i}",
            award_amount=amount,
            winner_name="某商事",
        )
        for i, amount in enumerate([100000, 200000, 300000, 400000], start=1)
    ]
    _patch_common(monkeypatch, settings, fake_gc, _candidate_pool(), award_records=awards)

    exit_code = main.run()
    assert exit_code == 0

    c001_rows = fake_gc.spreadsheets["SHEET_C001"].worksheet("レコメンド案件").rows
    assert len(c001_rows) == 1
    award_cell = c001_rows[0][AWARD_COL_INDEX]
    assert "同種4件" in award_cell
    assert "中央値¥250,000" in award_cell
