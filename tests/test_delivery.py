from __future__ import annotations

import base64
import email
import json
from datetime import datetime, timezone
from email.header import decode_header, make_header

import httplib2
import pytest
from googleapiclient.errors import HttpError

from modules.delivery import (
    append_new_matches,
    check_mail_auth,
    send_recommend_email,
    write_admin_summary,
)
from modules.models import (
    BidListing,
    Customer,
    CustomerError,
    CustomerProfile,
    MatchResult,
    PriceStats,
    SkipReason,
)
from tests.conftest import AWARD_COL_INDEX, STATUS_COL_INDEX


def _http_error(status: int = 403, message: str = "delegation denied") -> HttpError:
    resp = httplib2.Response({"status": status})
    resp.reason = "Forbidden"
    return HttpError(resp, json.dumps({"error": {"message": message}}).encode())


class _FakeExecutable:
    def __init__(self, result, error: HttpError | None):
        self._result = result
        self._error = error

    def execute(self):
        if self._error is not None:
            raise self._error
        return self._result


class _FakeMessages:
    def __init__(self, store, error):
        self._store, self._error = store, error

    def send(self, userId, body):
        self._store.setdefault("sent", []).append({"userId": userId, "body": body})
        return _FakeExecutable({"id": "msg1"}, self._error)


class _FakeDrafts:
    def __init__(self, store, error):
        self._store, self._error = store, error

    def create(self, userId, body):
        self._store.setdefault("drafts_created", []).append({"userId": userId, "body": body})
        return _FakeExecutable({"id": "draft1"}, self._error)

    def delete(self, userId, id):
        self._store.setdefault("drafts_deleted", []).append(id)
        return _FakeExecutable("", None)


class _FakeUsers:
    def __init__(self, store, error):
        self._store, self._error = store, error

    def messages(self):
        return _FakeMessages(self._store, self._error)

    def drafts(self):
        return _FakeDrafts(self._store, self._error)


class FakeGmailService:
    """Gmail API サービスの最小フェイク(users().messages()/drafts() をサポート)。"""

    def __init__(self, error: HttpError | None = None):
        self.store: dict = {}
        self._error = error

    def users(self):
        return _FakeUsers(self.store, self._error)


def _decode_sent_body(service: FakeGmailService) -> str:
    raw = service.store["sent"][0]["body"]["raw"]
    msg = email.message_from_bytes(base64.urlsafe_b64decode(raw))
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            return part.get_payload(decode=True).decode("utf-8")
    raise AssertionError("text/plain パートが見つかりません")


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


def _match(url: str = "https://example.jp/1", score: int = 90, price_stats: PriceStats | None = None) -> MatchResult:
    listing = BidListing(
        result_id="1",
        key="k1",
        external_document_uri=url,
        project_name="消耗品の購入",
        organization_name="某省",
        period_end_time="2026-08-01",
    )
    return MatchResult(
        listing=listing,
        customer_id="C001",
        score=score,
        reasons=["ok"],
        estimated_price=120000,
        price_confirmed=True,
        price_stats=price_stats,
    )


def test_append_new_matches_writes_row(fake_gc, settings):
    customer = _customer()
    new = append_new_matches(fake_gc, customer, [_match()], settings)
    assert len(new) == 1

    ws = fake_gc.spreadsheets["SHEET_C001"].worksheet("レコメンド案件")
    assert len(ws.rows) == 1
    assert ws.rows[0][0] == "消耗品の購入"
    assert ws.rows[0][5] == "https://example.jp/1"
    assert ws.rows[0][STATUS_COL_INDEX] == "未確認"


def test_award_cell_written_when_stats_present(fake_gc, settings):
    stats = PriceStats(count=12, median=248000, p25=180000, p75=310000)
    new = append_new_matches(fake_gc, _customer(), [_match(price_stats=stats)], settings)
    assert len(new) == 1
    ws = fake_gc.spreadsheets["SHEET_C001"].worksheet("レコメンド案件")
    cell = ws.rows[0][AWARD_COL_INDEX]
    assert "同種12件" in cell
    assert "¥248,000" in cell
    assert "¥180,000〜¥310,000" in cell


def test_award_cell_shows_no_data_when_zero_comparables(fake_gc, settings):
    new = append_new_matches(fake_gc, _customer(), [_match(price_stats=PriceStats(count=0))], settings)
    assert new[0].price_stats.count == 0
    ws = fake_gc.spreadsheets["SHEET_C001"].worksheet("レコメンド案件")
    assert ws.rows[0][AWARD_COL_INDEX] == "相場データなし"


def test_award_cell_blank_when_stats_none(fake_gc, settings):
    append_new_matches(fake_gc, _customer(), [_match(price_stats=None)], settings)
    ws = fake_gc.spreadsheets["SHEET_C001"].worksheet("レコメンド案件")
    assert ws.rows[0][AWARD_COL_INDEX] == ""


def test_append_new_matches_dedups_by_url_on_second_run(fake_gc, settings):
    customer = _customer()
    append_new_matches(fake_gc, customer, [_match()], settings)
    second_run_new = append_new_matches(fake_gc, customer, [_match()], settings)

    assert second_run_new == []
    ws = fake_gc.spreadsheets["SHEET_C001"].worksheet("レコメンド案件")
    assert len(ws.rows) == 1  # 重複追記されていない


def test_render_and_send_recommend_email(settings):
    customer = _customer()
    service = FakeGmailService()

    send_recommend_email(customer, [_match()], settings, service)

    sent = service.store["sent"]
    assert len(sent) == 1
    assert sent[0]["userId"] == "me"
    msg = email.message_from_bytes(base64.urlsafe_b64decode(sent[0]["body"]["raw"]))
    assert msg["To"] == settings.email.admin_address
    subject = str(make_header(decode_header(msg["Subject"])))
    assert customer.company_name in subject
    body = _decode_sent_body(service)
    assert "自動送信は行っていません" in body
    assert customer.contact_email in body
    assert "消耗品の購入" in body


def test_send_recommend_email_wraps_delegation_error(settings):
    # 委任未設定などで Gmail API が失敗したら、分かりやすい RuntimeError に包む
    service = FakeGmailService(error=_http_error(403, "Delegation denied"))
    with pytest.raises(RuntimeError, match="ドメイン全体の委任"):
        send_recommend_email(_customer(), [_match()], settings, service)


def _capture_email_body(settings, matches) -> str:
    service = FakeGmailService()
    send_recommend_email(_customer(), matches, settings, service)
    return _decode_sent_body(service)


def test_email_includes_award_stats_and_source_note(settings):
    stats = PriceStats(
        count=5,
        median=248000,
        p25=180000,
        p75=310000,
        examples=[{"project_name": "文具一式の購入", "amount": 250000, "winner": "〇〇商事"}],
    )
    body = _capture_email_body(settings, [_match(price_stats=stats)])
    assert "参考落札相場: 同種5件 中央値¥248,000" in body
    assert "実例: 文具一式の購入 ¥250,000（〇〇商事）" in body
    assert "出典: 調達ポータル(デジタル庁)落札実績オープンデータ" in body


def test_email_omits_source_note_when_no_award_data(settings):
    body = _capture_email_body(settings, [_match(price_stats=PriceStats(count=0))])
    assert "参考落札相場" not in body
    assert "出典:" not in body


def test_email_footer_always_includes_reply_guidance(settings):
    # 配信条件の変更は「メール返信」方式。案内文はURL設定の有無に関わらず常に出力される
    body = _capture_email_body(settings, [_match()])
    assert "【各種お手続き】" in body
    assert "このメールにそのままご返信ください" in body
    # 1往復で完了させるための返信例と反映タイミングの目安も載せる
    assert "対象エリアに神奈川県を追加" in body
    assert "翌営業日までに" in body


def test_email_footer_includes_portal_link_when_url_set(settings):
    settings.email.customer_portal_url = "https://billing.stripe.com/p/login/test123"
    body = _capture_email_body(settings, [_match()])
    assert "・配信の解約・お支払い方法の変更: https://billing.stripe.com/p/login/test123" in body


def test_email_footer_omits_portal_link_when_url_empty(settings):
    # ポータルURL未設定でも条件変更の案内は出るが、解約リンク行は出ない(空リンク防止)
    body = _capture_email_body(settings, [_match()])
    assert "このメールにそのままご返信ください" in body
    assert "解約・お支払い方法の変更" not in body


def test_check_mail_auth_sends_healthcheck_via_messages_send(settings):
    # gmail.send スコープで通る messages.send を使い、管理者(自分)宛に1通送る
    service = FakeGmailService()
    check_mail_auth(service, settings)
    sent = service.store.get("sent", [])
    assert len(sent) == 1
    assert sent[0]["userId"] == "me"
    assert "drafts_created" not in service.store  # 下書きAPIは使わない(別スコープ回避)
    msg = email.message_from_bytes(base64.urlsafe_b64decode(sent[0]["body"]["raw"]))
    assert msg["To"] == settings.email.admin_address
    subject = str(make_header(decode_header(msg["Subject"])))
    assert "ヘルスチェック" in subject


def test_check_mail_auth_raises_helpful_error_on_delegation_failure(settings):
    service = FakeGmailService(error=_http_error(403, "insufficientPermissions"))
    with pytest.raises(RuntimeError, match="ドメイン全体の委任"):
        check_mail_auth(service, settings)


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
