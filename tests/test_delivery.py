from __future__ import annotations

import base64
import email
import json
from datetime import date, datetime, timezone
from email.header import decode_header, make_header

import httplib2
import pytest
from googleapiclient.errors import HttpError

from modules.delivery import (
    RECOMMEND_TAB,
    append_new_matches,
    build_deadline_reminder_email,
    check_mail_auth,
    find_new_matches,
    find_upcoming_deadlines,
    send_deadline_reminder_email,
    send_recommend_email,
    write_admin_summary,
    write_matches,
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


def _customer(output_sheet_id: str = "SHEET_C001", cc_emails: str = "") -> Customer:
    return Customer(
        customer_id="C001",
        company_name="サンプル商事株式会社",
        contact_name="佐藤一郎",
        contact_email="sato@example.jp",
        cc_emails=cc_emails,
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


def _sent_headers(service: FakeGmailService) -> email.message.Message:
    raw = service.store["sent"][0]["body"]["raw"]
    return email.message_from_bytes(base64.urlsafe_b64decode(raw))


def test_recommend_email_goes_to_admin_when_auto_send_disabled(settings):
    # 既定(False)は従来どおり管理者宛のみ。顧客アドレスは宛先に入らない
    settings.email.auto_send_to_customer = False
    service = FakeGmailService()
    send_recommend_email(_customer(), [_match()], settings, service)

    msg = _sent_headers(service)
    assert msg["To"] == settings.email.admin_address
    assert msg["Bcc"] is None
    assert "自動送信は行っていません" in _decode_sent_body(service)


def test_recommend_email_goes_to_customer_with_admin_bcc_when_enabled(settings):
    # Trueなら顧客へ直送し、管理者にはBccで控えが残る(何が社外に出たか追跡できる)
    settings.email.auto_send_to_customer = True
    service = FakeGmailService()
    send_recommend_email(_customer(), [_match()], settings, service)

    msg = _sent_headers(service)
    assert msg["To"] == "sato@example.jp"
    assert msg["Bcc"] == settings.email.admin_address
    # 顧客宛には管理者向けの但し書きを絶対に含めない
    assert "自動送信は行っていません" not in _decode_sent_body(service)
    assert "転送してください" not in _decode_sent_body(service)


def test_auto_send_fails_loudly_when_customer_email_missing(settings):
    # 宛先が空のまま送信を試みると、Gmail側で曖昧に失敗する前にここで止める
    settings.email.auto_send_to_customer = True
    customer = _customer().model_copy(update={"contact_email": ""})
    with pytest.raises(RuntimeError, match="contact_email"):
        send_recommend_email(customer, [_match()], settings, FakeGmailService())


def test_dry_run_does_not_write_to_sheet_but_reports_new_matches(fake_gc, settings):
    # dry-runでは重複判定まで行い、シートへの追記はしない
    customer = _customer()
    new = append_new_matches(fake_gc, customer, [_match()], settings, dry_run=True)
    assert len(new) == 1
    ws = fake_gc.spreadsheets["SHEET_C001"].worksheet(RECOMMEND_TAB)
    assert ws.rows == []


def test_footer_includes_link_to_past_matches_sheet(settings):
    # 過去にご案内した案件を顧客自身が見られるよう、専用シートのURLを毎回載せる
    body = _capture_email_body(settings, [_match()])
    assert "これまでにご案内した案件の一覧: https://docs.google.com/spreadsheets/d/SHEET_C001/edit" in body


def test_empty_matches_sends_no_new_listings_notice(settings):
    # 新着0件の日も、無音にせず「新着なし」を明示するメールを送る
    body = _capture_email_body(settings, [])
    assert "新規公開案件はございませんでした" in body
    # 0件の日も過去分の一覧リンクは出す(見返す手段がなくなるわけではない)
    assert "これまでにご案内した案件の一覧" in body
    # 通常テンプレートの文言(案件リスト前提の文)が紛れ込んでいないこと
    assert "各案件の詳細・応札要否のご検討をお願いいたします" not in body


def test_empty_matches_subject_says_no_new_listings(settings):
    service = FakeGmailService()
    send_recommend_email(_customer(), [], settings, service)
    msg = _sent_headers(service)
    subject = str(make_header(decode_header(msg["Subject"])))
    assert subject == "【入札案件レコメンド】サンプル商事株式会社様 - 本日は新着なし"


def test_empty_matches_still_goes_to_customer_when_auto_send_enabled(settings):
    # 0件でも auto_send_to_customer=True なら顧客へ直接届く(無音による解約誤解を防ぐ設計)
    settings.email.auto_send_to_customer = True
    service = FakeGmailService()
    send_recommend_email(_customer(), [], settings, service)
    msg = _sent_headers(service)
    assert msg["To"] == "sato@example.jp"
    assert msg["Bcc"] == settings.email.admin_address


def test_cc_emails_added_when_auto_send_enabled(settings):
    # Ccは顧客への直送時のみ意味を持つ(管理者確認モードでは付けない)
    settings.email.auto_send_to_customer = True
    customer = _customer(cc_emails="y.kaneko@nrg.co.jp")
    service = FakeGmailService()
    send_recommend_email(customer, [_match()], settings, service)

    msg = _sent_headers(service)
    assert msg["Cc"] == "y.kaneko@nrg.co.jp"


def test_cc_emails_supports_multiple_addresses(settings):
    settings.email.auto_send_to_customer = True
    customer = _customer(cc_emails="a@example.jp, b@example.jp")
    service = FakeGmailService()
    send_recommend_email(customer, [_match()], settings, service)

    msg = _sent_headers(service)
    assert msg["Cc"] == "a@example.jp, b@example.jp"


def test_cc_header_omitted_when_no_cc_emails(settings):
    settings.email.auto_send_to_customer = True
    service = FakeGmailService()
    send_recommend_email(_customer(), [_match()], settings, service)

    msg = _sent_headers(service)
    assert msg["Cc"] is None


def test_cc_emails_ignored_in_admin_confirmation_mode(settings):
    # auto_send_to_customer=False(既定)では、Ccが設定されていても付けない
    customer = _customer(cc_emails="y.kaneko@nrg.co.jp")
    service = FakeGmailService()
    send_recommend_email(customer, [_match()], settings, service)

    msg = _sent_headers(service)
    assert msg["Cc"] is None


def test_find_new_matches_does_not_write(fake_gc, settings):
    """find_new_matches は新規判定のみ行い、シートには書き込まない
    (LLM判定を挟むための分離。main.py参照)。"""
    customer = _customer()
    new = find_new_matches(fake_gc, customer, [_match()])
    assert len(new) == 1

    ws = fake_gc.spreadsheets["SHEET_C001"].worksheet("レコメンド案件")
    assert ws.rows == []


def test_write_matches_writes_row(fake_gc, settings):
    customer = _customer()
    new = find_new_matches(fake_gc, customer, [_match()])
    write_matches(fake_gc, customer, new)

    ws = fake_gc.spreadsheets["SHEET_C001"].worksheet("レコメンド案件")
    assert len(ws.rows) == 1
    assert ws.rows[0][0] == "消耗品の購入"
    assert ws.rows[0][STATUS_COL_INDEX] == "未確認"


def test_write_matches_noop_on_empty_list(fake_gc, settings):
    customer = _customer()
    write_matches(fake_gc, customer, [])
    ws = fake_gc.spreadsheets["SHEET_C001"].worksheet("レコメンド案件")
    assert ws.rows == []


def test_append_new_matches_is_find_and_write_composed(fake_gc, settings):
    """append_new_matches は find_new_matches + write_matches の合成(後方互換)。
    別シートで、append_new_matches 経由でも find_new_matches+write_matches と
    同じ結果(新規判定・シート書き込み)になることを確認する。"""
    customer = _customer(output_sheet_id="SHEET_C002")
    via_append = append_new_matches(fake_gc, customer, [_match(url="https://example.jp/2")], settings)
    assert len(via_append) == 1
    ws = fake_gc.spreadsheets["SHEET_C002"].worksheet("レコメンド案件")
    assert len(ws.rows) == 1
    assert ws.rows[0][0] == "消耗品の購入"


def test_resolve_deadline_prefers_api_value_over_llm(settings):
    from modules.delivery import _resolve_deadline

    m = _match()
    m.listing.period_end_time = "2026-10-01T17:00"
    m.llm_deadline = "2026-10-05"
    assert _resolve_deadline(m) == "2026-10-01T17:00"


def test_resolve_deadline_falls_back_to_llm_when_api_empty(settings):
    from modules.delivery import _resolve_deadline

    m = _match()
    m.listing.period_end_time = None
    m.llm_deadline = "2026-10-05"
    assert _resolve_deadline(m) == "2026-10-05(AI抽出・要確認)"


def test_resolve_deadline_returns_kakunin_when_both_empty(settings):
    from modules.delivery import _resolve_deadline

    m = _match()
    m.listing.period_end_time = None
    m.llm_deadline = None
    assert _resolve_deadline(m) == "要確認"


def test_resolve_price_prefers_regex_over_llm(settings):
    from modules.delivery import _resolve_price

    m = _match()
    m.estimated_price = 120000
    m.llm_estimated_price = 999999
    assert _resolve_price(m) == "¥120,000"


def test_resolve_price_falls_back_to_llm_when_regex_failed(settings):
    from modules.delivery import _resolve_price

    m = _match()
    m.estimated_price = None
    m.llm_estimated_price = 500000
    assert _resolve_price(m) == "¥500,000(AI抽出・要確認)"


def test_reasons_include_llm_reason_when_present(fake_gc, settings):
    customer = _customer()
    m = _match()
    m.reasons = ["キーワード一致(案件名): 消耗品"]
    m.llm_reason = "文具の物品購入案件のため関連あり"
    new = find_new_matches(fake_gc, customer, [m])
    write_matches(fake_gc, customer, new)

    ws = fake_gc.spreadsheets["SHEET_C001"].worksheet("レコメンド案件")
    reasons_cell = ws.rows[0][7]  # レコメンド理由列
    assert "AI判定: 文具の物品購入案件のため関連あり" in reasons_cell


def _reminder_row(project_name, org, deadline, status, url="https://example.jp/x"):
    # RECOMMEND_HEADERS: 案件名,発注機関,公告日,締切日,予定価格,案件URL,マッチ度スコア,レコメンド理由,参考落札相場,ステータス
    return [project_name, org, "", deadline, "要確認", url, 100, "ok", "", status]


def test_find_upcoming_deadlines_within_threshold(fake_gc, settings):
    ws = fake_gc.spreadsheets["SHEET_C001"].worksheet(RECOMMEND_TAB)
    ws.rows.append(_reminder_row("3日後締切の案件", "某省", "2026-09-24", "未確認"))

    customer = _customer()
    result = find_upcoming_deadlines(fake_gc, customer, today=date(2026, 9, 21))

    assert len(result) == 1
    assert result[0].project_name == "3日後締切の案件"
    assert result[0].deadline == date(2026, 9, 24)


def test_find_upcoming_deadlines_excludes_beyond_threshold(fake_gc, settings):
    ws = fake_gc.spreadsheets["SHEET_C001"].worksheet(RECOMMEND_TAB)
    ws.rows.append(_reminder_row("4日後締切の案件", "某省", "2026-09-25", "未確認"))

    customer = _customer()
    result = find_upcoming_deadlines(fake_gc, customer, today=date(2026, 9, 21))
    assert result == []


def test_find_upcoming_deadlines_excludes_past_deadline(fake_gc, settings):
    ws = fake_gc.spreadsheets["SHEET_C001"].worksheet(RECOMMEND_TAB)
    ws.rows.append(_reminder_row("締切済みの案件", "某省", "2026-09-20", "未確認"))

    customer = _customer()
    result = find_upcoming_deadlines(fake_gc, customer, today=date(2026, 9, 21))
    assert result == []


def test_find_upcoming_deadlines_excludes_non_unconfirmed_status(fake_gc, settings):
    ws = fake_gc.spreadsheets["SHEET_C001"].worksheet(RECOMMEND_TAB)
    ws.rows.append(_reminder_row("検討中の案件", "某省", "2026-09-22", "検討中"))

    customer = _customer()
    result = find_upcoming_deadlines(fake_gc, customer, today=date(2026, 9, 21))
    assert result == []


def test_find_upcoming_deadlines_excludes_unparseable_deadline(fake_gc, settings):
    ws = fake_gc.spreadsheets["SHEET_C001"].worksheet(RECOMMEND_TAB)
    ws.rows.append(_reminder_row("締切不明の案件", "某省", "要確認", "未確認"))

    customer = _customer()
    result = find_upcoming_deadlines(fake_gc, customer, today=date(2026, 9, 21))
    assert result == []


def test_find_upcoming_deadlines_parses_llm_suffix_format(fake_gc, settings):
    """LLM抽出値は 'YYYY-MM-DD(AI抽出・要確認)' サフィックス付きで書かれる。
    先頭の日付部分だけを正しくパースできること。"""
    ws = fake_gc.spreadsheets["SHEET_C001"].worksheet(RECOMMEND_TAB)
    ws.rows.append(_reminder_row("AI抽出締切の案件", "某省", "2026-09-23(AI抽出・要確認)", "未確認"))

    customer = _customer()
    result = find_upcoming_deadlines(fake_gc, customer, today=date(2026, 9, 21))
    assert len(result) == 1
    assert result[0].deadline == date(2026, 9, 23)


def test_find_upcoming_deadlines_sorted_by_deadline_ascending(fake_gc, settings):
    ws = fake_gc.spreadsheets["SHEET_C001"].worksheet(RECOMMEND_TAB)
    ws.rows.append(_reminder_row("遠い案件", "某省", "2026-09-24", "未確認", url="https://example.jp/far"))
    ws.rows.append(_reminder_row("近い案件", "某省", "2026-09-22", "未確認", url="https://example.jp/near"))

    customer = _customer()
    result = find_upcoming_deadlines(fake_gc, customer, today=date(2026, 9, 21))
    assert [r.project_name for r in result] == ["近い案件", "遠い案件"]


def test_deadline_reminder_email_body_includes_days_left(settings):
    from modules.delivery import UpcomingDeadline

    customer = _customer()
    upcoming = [UpcomingDeadline("備蓄品の購入", "某市", date(2026, 9, 24), "https://example.jp/1")]
    msg = build_deadline_reminder_email(customer, upcoming, settings)
    body = msg.get_payload(0).get_payload(decode=True).decode("utf-8")
    assert "備蓄品の購入" in body
    assert "締切間近" in str(msg["Subject"])


def test_send_deadline_reminder_email_uses_gmail_send(settings):
    from modules.delivery import UpcomingDeadline

    customer = _customer()
    upcoming = [UpcomingDeadline("備蓄品の購入", "某市", date(2026, 9, 24), "https://example.jp/1")]
    service = FakeGmailService()
    send_deadline_reminder_email(customer, upcoming, settings, service)
    assert len(service.store["sent"]) == 1
