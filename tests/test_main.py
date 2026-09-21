"""main.run() のE2Eテスト(ダミー顧客3社)。

受け入れ基準1「ダミー顧客3社に対し、日次バッチが正常完走する」と、
受け入れ基準4「1社の処理でエラーが発生しても、残りの顧客の処理が継続する」を、
実際のGoogle Sheets/Gmail/kkj.go.jp APIに接続せず検証する。

本物のGoogle API・SMTPサーバーに対する実接続確認(受け入れ基準の最終確認)は
README記載の手順に従い、実際の認証情報を用意したうえで手動実行すること。
"""
from __future__ import annotations

import base64
import email as email_module
from email.header import decode_header, make_header

import main
from modules.models import AwardRecord, BidListing


def _sent_subject(sent_entry: dict) -> str:
    msg = email_module.message_from_bytes(base64.urlsafe_b64decode(sent_entry["body"]["raw"]))
    return str(make_header(decode_header(msg["Subject"])))


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


from tests.test_delivery import FakeGmailService

# _patch_common がセットしたフェイクGmailサービスを、テスト側から参照するための保持箱
_GMAIL: dict = {}


def _patch_common(monkeypatch, settings, fake_gc, candidate_pool, award_records=None):
    monkeypatch.setattr("main.load_settings", lambda path: settings)
    monkeypatch.setattr("main.build_gspread_client", lambda: fake_gc)
    service = FakeGmailService()
    _GMAIL["service"] = service
    monkeypatch.setattr("main.build_gmail_service", lambda sender: service)
    monkeypatch.setattr("main.fetch_candidate_pool", lambda customers, settings: candidate_pool)
    # 落札実績取得はネットワークを避けてスタブ化する(既定は空=相場データなし)
    monkeypatch.setattr("main.fetch_awards", lambda settings: award_records or [])
    # LLM判定はテスト環境のANTHROPIC_API_KEY有無に依存させない(未設定ならfail-open、
    # 実行環境に偶然キーがあってもネットワークへ実際に呼びに行かないようにする)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_daily_batch_completes_for_three_dummy_customers(monkeypatch, settings, fake_gc):
    _patch_common(monkeypatch, settings, fake_gc, _candidate_pool())

    exit_code = main.run()

    assert exit_code == 0

    c001_rows = fake_gc.spreadsheets["SHEET_C001"].worksheet("レコメンド案件").rows
    assert len(c001_rows) == 1
    assert c001_rows[0][0] == "消耗品(文具)の購入"
    # 庁舎改修工事はキーワード(消耗品,印刷,封筒)に一致せず閾値未満のためC001の
    # シートに入らない(除外キーワードのハード除外は2026-09-21に撤廃済み)
    assert all("庁舎改修工事" != row[0] for row in c001_rows)

    c002_rows = fake_gc.spreadsheets["SHEET_C002"].worksheet("レコメンド案件").rows
    assert len(c002_rows) == 1
    assert c002_rows[0][0] == "防災用品の調達"

    c003_rows = fake_gc.spreadsheets["SHEET_C003"].worksheet("レコメンド案件").rows
    assert c003_rows == []  # マッチなし

    # 新着0件のC003分も含め、3社全員にメールが生成される(無音による解約誤解を防ぐ設計。
    # 顧客への自動送信ではなく管理者宛)
    sent = _GMAIL["service"].store.get("sent", [])
    assert len(sent) == 3
    assert all(m["userId"] == "me" for m in sent)
    subjects = [_sent_subject(m) for m in sent]
    assert any(s.endswith("サンプル物産株式会社様 - 本日は新着なし") for s in subjects)

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


def test_mail_check_succeeds_without_touching_sheets_or_matching(monkeypatch, settings):
    # --mail-check はGoogle Sheets/案件探索に触れず、Gmail送信の認証・委任のみを検証する
    monkeypatch.setattr("main.load_settings", lambda path: settings)
    monkeypatch.setattr("main.build_gmail_service", lambda sender: object())
    monkeypatch.setattr("main.check_mail_auth", lambda service, settings: None)

    exit_code = main.run_mail_check()

    assert exit_code == 0


def test_mail_check_fails_when_auth_raises(monkeypatch, settings):
    def _raise(service, settings):
        raise RuntimeError("Gmail APIでの送信に失敗しました。ドメイン全体の委任...")

    monkeypatch.setattr("main.load_settings", lambda path: settings)
    monkeypatch.setattr("main.build_gmail_service", lambda sender: object())
    monkeypatch.setattr("main.check_mail_auth", _raise)

    exit_code = main.run_mail_check()

    assert exit_code == 1


def test_cli_mail_check_flag_routes_to_run_mail_check(monkeypatch):
    monkeypatch.setattr("sys.argv", ["main.py", "--mail-check"])
    monkeypatch.setattr("main.run_mail_check", lambda config: 0)
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


def test_customer_already_sent_today_is_skipped(monkeypatch, settings, fake_gc):
    # C001の「最終送信日」列(10列目, 0-index 9)を今日と同じ日付にしておく
    monkeypatch.setattr("main.today_jst", lambda: "2026-09-16")
    master_ws = fake_gc.spreadsheets["MASTER_ID"].worksheet("顧客マスタ")
    master_ws.rows[0][9] = "2026-09-16"  # C001は送信済み扱い

    _patch_common(monkeypatch, settings, fake_gc, _candidate_pool())
    exit_code = main.run()

    assert exit_code == 0
    sent = _GMAIL["service"].store.get("sent", [])
    subjects = [_sent_subject(m) for m in sent]
    # C001(サンプル商事株式会社)はスキップされ、C002・C003は通常通り送信される
    assert not any("サンプル商事株式会社" in s for s in subjects)
    assert any("テスト工業株式会社" in s for s in subjects)
    assert any("サンプル物産株式会社" in s for s in subjects)
    assert len(sent) == 2

    # スキップされた顧客の最終送信日は上書きされない(既存の日付のまま)
    assert master_ws.rows[0][9] == "2026-09-16"


def test_sending_records_last_sent_date(monkeypatch, settings, fake_gc):
    monkeypatch.setattr("main.today_jst", lambda: "2026-09-16")
    master_ws = fake_gc.spreadsheets["MASTER_ID"].worksheet("顧客マスタ")
    assert master_ws.rows[0][9] == ""  # 事前状態: 未送信

    _patch_common(monkeypatch, settings, fake_gc, _candidate_pool())
    main.run()

    # 送信後、顧客マスタの最終送信日が更新されている
    assert master_ws.rows[0][9] == "2026-09-16"
    assert master_ws.rows[1][9] == "2026-09-16"
    assert master_ws.rows[2][9] == "2026-09-16"


def test_dry_run_does_not_record_last_sent_date(monkeypatch, settings, fake_gc):
    # dry-runは送信済み判定こそ見るが、実際には送信しないので記録も更新しない
    monkeypatch.setattr("main.today_jst", lambda: "2026-09-16")
    master_ws = fake_gc.spreadsheets["MASTER_ID"].worksheet("顧客マスタ")

    _patch_common(monkeypatch, settings, fake_gc, _candidate_pool())
    main.run(dry_run=True)

    assert master_ws.rows[0][9] == ""
    assert _GMAIL["service"].store.get("sent", []) == []


def test_llm_judge_failure_does_not_stop_customer_processing(monkeypatch, settings, fake_gc):
    """judge_relevance が想定外の例外を投げても、main.run() 全体はfail-openで
    継続する(llm_judgeモジュール内部のfail-openとは別に、main.py側でも
    二重に守っていることの確認)。"""
    _patch_common(monkeypatch, settings, fake_gc, _candidate_pool())
    monkeypatch.setattr(
        "main.judge_relevance",
        lambda customer, matches, settings: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    exit_code = main.run()

    assert exit_code == 0
    # LLM判定が例外を投げても、キーワードマッチした案件は通常通り配信される
    c001_rows = fake_gc.spreadsheets["SHEET_C001"].worksheet("レコメンド案件").rows
    assert len(c001_rows) == 1
    assert c001_rows[0][0] == "消耗品(文具)の購入"


def test_llm_relevant_false_excludes_from_sheet_and_email(monkeypatch, settings, fake_gc):
    """llm_relevant=False と判定された候補は、シート追記・メール本文の
    両方から除外される。"""
    _patch_common(monkeypatch, settings, fake_gc, _candidate_pool())

    def _fake_judge(customer, matches, settings):
        for m in matches:
            m.llm_relevant = False
            m.llm_reason = "テストで強制的に無関係と判定"

    monkeypatch.setattr("main.judge_relevance", _fake_judge)

    exit_code = main.run()

    assert exit_code == 0
    c001_rows = fake_gc.spreadsheets["SHEET_C001"].worksheet("レコメンド案件").rows
    assert c001_rows == []

    sent = _GMAIL["service"].store.get("sent", [])
    subjects = [_sent_subject(m) for m in sent]
    assert any(s.endswith("サンプル商事株式会社様 - 本日は新着なし") for s in subjects)
