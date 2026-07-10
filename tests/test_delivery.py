from __future__ import annotations

from datetime import datetime, timezone

from modules.delivery import append_new_matches, send_recommend_email, write_admin_summary
from modules.models import BidListing, Customer, CustomerError, CustomerProfile, MatchResult, SkipReason


def _customer(output_sheet_id: str = "SHEET_C001") -> Customer:
    return Customer(
        customer_id="C001",
        company_name="サンプル商事株式会社",
        contact_name="佐藤一郎",
        contact_email="sato@example.jp",
        plan="standard",
        status="active",
        output_sheet_id=output_sheet_id,
        profile=CustomerProfile(customer_id="C001", keywords="消耗品"),
    )


def _match(url: str = "https://example.jp/1", score: int = 90) -> MatchResult:
    listing = BidListing(
        result_id="1",
        key="k1",
        external_document_uri=url,
        project_name="消耗品の購入",
        organization_name="某省",
        period_end_time="2026-08-01",
    )
    return MatchResult(
        listing=listing, customer_id="C001", score=score, reasons=["ok"], estimated_price=120000, price_confirmed=True
    )


def test_append_new_matches_writes_row(fake_gc, settings):
    customer = _customer()
    new = append_new_matches(fake_gc, customer, [_match()], settings)
    assert len(new) == 1

    ws = fake_gc.spreadsheets["SHEET_C001"].worksheet("レコメンド案件")
    assert len(ws.rows) == 1
    assert ws.rows[0][0] == "消耗品の購入"
    assert ws.rows[0][5] == "https://example.jp/1"
    assert ws.rows[0][8] == "未確認"


def test_append_new_matches_dedups_by_url_on_second_run(fake_gc, settings):
    customer = _customer()
    append_new_matches(fake_gc, customer, [_match()], settings)
    second_run_new = append_new_matches(fake_gc, customer, [_match()], settings)

    assert second_run_new == []
    ws = fake_gc.spreadsheets["SHEET_C001"].worksheet("レコメンド案件")
    assert len(ws.rows) == 1  # 重複追記されていない


def test_render_and_send_recommend_email(monkeypatch, settings):
    customer = _customer()
    sent_messages = []

    class FakeSMTP:
        def __init__(self, host, port):
            assert host == settings.email.smtp_host
            assert port == settings.email.smtp_port

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            pass

        def login(self, user, password):
            assert user == "smtp_user"
            assert password == "smtp_pass"

        def send_message(self, msg):
            sent_messages.append(msg)

    monkeypatch.setattr("modules.delivery.smtplib.SMTP", FakeSMTP)

    send_recommend_email(customer, [_match()], settings, "smtp_user", "smtp_pass")

    assert len(sent_messages) == 1
    msg = sent_messages[0]
    assert msg["To"] == settings.email.admin_address
    assert customer.company_name in msg["Subject"]
    body = msg.get_payload()[0].get_payload(decode=True).decode("utf-8")
    assert "自動送信は行っていません" in body
    assert customer.contact_email in body
    assert "消耗品の購入" in body


def test_write_admin_summary_appends_row(fake_gc, settings):
    write_admin_summary(
        fake_gc,
        settings,
        run_started_at=datetime(2026, 7, 10, 7, 30, tzinfo=timezone.utc),
        processed=2,
        skipped=[SkipReason(customer_id="C999", reason="条件プロファイルが見つかりません")],
        total_matches=3,
        errors=[CustomerError(customer_id="C998", error="boom")],
    )

    ws = fake_gc.spreadsheets["MASTER_ID"].worksheet("実行ログ")
    assert len(ws.rows) == 1
    row = ws.rows[0]
    assert row[1] == 2  # 処理顧客数
    assert row[2] == 1  # スキップ顧客数
    assert row[3] == 3  # 総マッチ件数
    assert row[4] == 1  # エラー件数
    assert "C999" in row[5] and "C998" in row[5]
