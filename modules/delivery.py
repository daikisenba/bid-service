"""顧客専用シートへの書き込み・管理者宛メール送信・実行ログ記録。

顧客への自動送信は行わない(誤配信リスクの排除)。フェーズ1では、生成した
配信メール本文はすべて管理者(settings.email.admin_address)宛にGmail APIで送信し、
管理者が内容を確認したうえで顧客へ転送する運用とする。

メール送信はGmail API(サービスアカウント + ドメイン全体の委任)で行う。
Google Workspaceが2025年にSMTPの基本認証を廃止したため、SMTP+アプリパスワード
方式は使えない。送信元ユーザーをimpersonateしたGmailサービスをmain.py側で
生成し、send_recommend_email に渡す。
"""
from __future__ import annotations

import base64
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import gspread

from .awards import AWARD_SOURCE_NOTE
from .config import Settings
from .models import Customer, CustomerError, MatchResult, PriceStats, SkipReason

RECOMMEND_TAB = "レコメンド案件"
# 参考落札相場の列見出しには出典を併記し、シート上でも出典が常に見えるようにする
_AWARD_COL_HEADER = f"参考落札相場\n（{AWARD_SOURCE_NOTE}）"
RECOMMEND_HEADERS = [
    "案件名",
    "発注機関",
    "公告日",
    "締切日",
    "予定価格",
    "案件URL",
    "マッチ度スコア",
    "レコメンド理由",
    _AWARD_COL_HEADER,
    "ステータス",
]
_URL_COLUMN = RECOMMEND_HEADERS.index("案件URL") + 1

_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "recommend_mail.md"


def _price_display(match: MatchResult) -> str:
    if match.estimated_price is None:
        return "要確認"
    return f"¥{match.estimated_price:,}"


def _award_cell(stats: PriceStats | None) -> str:
    """シート用の参考落札相場セル文字列。None は照合なし(空欄)、count=0 は相場データなし。"""
    if stats is None:
        return ""
    if stats.count == 0:
        return "相場データなし"
    text = f"同種{stats.count}件 中央値¥{stats.median:,}"
    if stats.p25 is not None and stats.p75 is not None:
        text += f"（¥{stats.p25:,}〜¥{stats.p75:,}）"
    return text


def _existing_urls(ws: gspread.Worksheet) -> set[str]:
    values = ws.col_values(_URL_COLUMN)
    return {v.strip() for v in values[1:] if v.strip()}


def append_new_matches(
    gc: gspread.Client, customer: Customer, matches: list[MatchResult], settings: Settings
) -> list[MatchResult]:
    """マッチ結果を顧客専用シートに追記する。案件URLで重複チェックし、新規分のみ返す。"""
    sh = gc.open_by_key(customer.output_sheet_id)
    ws = sh.worksheet(RECOMMEND_TAB)
    existing = _existing_urls(ws)

    new_matches = [m for m in matches if m.listing.dedup_key not in existing]
    if not new_matches:
        return []

    rows = [
        [
            m.listing.project_name,
            m.listing.organization_name or "",
            m.listing.cft_issue_date or "",
            m.listing.period_end_time or "",
            _price_display(m),
            m.listing.dedup_key,
            m.score,
            " / ".join(m.reasons),
            _award_cell(m.price_stats),
            "未確認",
        ]
        for m in new_matches
    ]
    ws.append_rows(rows, value_input_option="USER_ENTERED")
    return new_matches


def _award_email_lines(stats: PriceStats | None) -> str:
    """メール用の参考落札相場ブロック。相場照合なし(None)または0件のときは空文字。"""
    if stats is None or stats.count == 0:
        return ""
    line = f"   参考落札相場: 同種{stats.count}件 中央値¥{stats.median:,}"
    if stats.p25 is not None and stats.p75 is not None:
        line += f"（¥{stats.p25:,}〜¥{stats.p75:,}）"
    line += "\n"
    for ex in stats.examples:
        winner = f"（{ex.winner}）" if ex.winner else ""
        line += f"     実例: {ex.project_name} ¥{ex.amount:,}{winner}\n"
    return line


def _footer_lines(settings: Settings) -> str:
    """配信メールのフッター(各種お手続き案内)。

    配信条件の変更は「メール返信」方式のため常に出力する(専用フォームは設けない)。
    1往復で完了するよう、返信の書き方の例と反映タイミングの目安を添える。
    Stripeカスタマーポータル(解約・カード変更)のリンクはURL設定時のみ出力する。"""
    lines = [
        "・配信条件(対象エリア・品目など)の変更: このメールにそのままご返信ください",
        "  例:「対象エリアに神奈川県を追加してください」",
        "     「キーワードに『印刷』を追加、『保守』は除外してください」",
        "  変更したい内容を箇条書きでお送りいただければ、翌営業日までに担当が反映し、",
        "  完了をご連絡いたします。",
    ]
    if settings.email.customer_portal_url:
        lines.append(
            f"・配信の解約・お支払い方法の変更: {settings.email.customer_portal_url}"
        )
    divider = "----------------------------------------"
    return f"\n{divider}\n【各種お手続き】\n" + "\n".join(lines) + "\n"


def _render_email_body(customer: Customer, matches: list[MatchResult], settings: Settings) -> str:
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    listing_lines = []
    for i, m in enumerate(matches, start=1):
        listing = m.listing
        listing_lines.append(
            f"{i}. {listing.project_name}\n"
            f"   発注機関: {listing.organization_name or '不明'}\n"
            f"   締切日時: {listing.period_end_time or '要確認'}\n"
            f"   予定価格: {_price_display(m)}\n"
            f"   マッチ度: {m.score}点\n"
            f"   案件URL: {listing.dedup_key}\n"
            f"{_award_email_lines(m.price_stats)}"
        )
    body = template.format(
        company_name=settings.company.name,
        customer_company_name=customer.company_name,
        match_count=len(matches),
        listings="\n".join(listing_lines),
        footer=_footer_lines(settings),
    )
    # 参考落札相場を1件でも掲載したら出典を明記する(利用条件)
    if any(m.price_stats and m.price_stats.count > 0 for m in matches):
        body += f"\n{AWARD_SOURCE_NOTE}\n"
    return body


_DELEGATION_HINT = (
    "Gmail APIでの送信に失敗しました。以下を確認してください: "
    "(1) Google Cloudプロジェクトで Gmail API が有効であること、"
    "(2) 管理コンソールの『ドメイン全体の委任』にサービスアカウントのクライアントIDと "
    "スコープ https://www.googleapis.com/auth/gmail.send が登録されていること、"
    "(3) settings.email.from_address が実在するWorkspaceユーザーであること。"
)


def _gmail_send(gmail_service, msg: MIMEMultipart) -> None:
    """MIMEメッセージをGmail APIで送信する。委任未設定等の失敗は分かりやすく包む。"""
    from googleapiclient.errors import HttpError

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    try:
        gmail_service.users().messages().send(userId="me", body={"raw": raw}).execute()
    except HttpError as exc:
        raise RuntimeError(f"{_DELEGATION_HINT} [Gmail API応答: {exc}]") from exc


def check_mail_auth(gmail_service, settings: Settings) -> None:
    """Gmail送信の認証・委任を単体で検証する(実際の配信はしない)。

    send_recommend_email は新着マッチがあるときしか呼ばれないため、新着0件が
    続くと送信経路の不調に気づけない(「実行成功」が誤って安全と解釈される)。
    この関数はマッチの有無に関係なく、下書きを作成→即削除することで、
    委任・スコープ・送信元ユーザーの妥当性をエンドツーエンドで確認する。
    """
    from googleapiclient.errors import HttpError

    msg = MIMEMultipart()
    msg["Subject"] = "[bid-service] メール送信ヘルスチェック"
    msg["From"] = settings.email.from_address
    msg["To"] = settings.email.admin_address
    msg.attach(MIMEText("これは送信経路の確認用の下書きです(送信されません)。", "plain", "utf-8"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    try:
        draft = gmail_service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
        gmail_service.users().drafts().delete(userId="me", id=draft["id"]).execute()
    except HttpError as exc:
        raise RuntimeError(f"{_DELEGATION_HINT} [Gmail API応答: {exc}]") from exc


def send_recommend_email(
    customer: Customer,
    matches: list[MatchResult],
    settings: Settings,
    gmail_service,
) -> None:
    """レコメンドメールを生成し、管理者宛にGmail APIで送信する(顧客への自動送信は行わない)。"""
    body = _render_email_body(customer, matches, settings)
    notice = (
        f"[本メールは管理者確認用です。顧客への自動送信は行っていません。"
        f"内容を確認のうえ、{customer.contact_name}様({customer.contact_email})へ"
        f"転送してください。]\n\n"
    )

    msg = MIMEMultipart()
    msg["Subject"] = f"【入札案件レコメンド】{customer.company_name}様 - {len(matches)}件"
    msg["From"] = settings.email.from_address
    msg["To"] = settings.email.admin_address
    msg.attach(MIMEText(notice + body, "plain", "utf-8"))

    _gmail_send(gmail_service, msg)


def write_admin_summary(
    gc: gspread.Client,
    settings: Settings,
    *,
    run_started_at: datetime,
    processed: int,
    skipped: list[SkipReason],
    total_matches: int,
    errors: list[CustomerError],
) -> None:
    """今回の実行結果を管理者向けシート(実行ログタブ)に1行追記する。"""
    sh = gc.open_by_key(settings.google.customer_master_sheet_id)
    ws = sh.worksheet(settings.google.admin_log_tab)

    details = [f"スキップ({s.customer_id}): {s.reason}" for s in skipped]
    details += [f"エラー({e.customer_id}): {e.error}" for e in errors]

    ws.append_row(
        [
            run_started_at.isoformat(timespec="seconds"),
            processed,
            len(skipped),
            total_matches,
            len(errors),
            " / ".join(details),
        ],
        value_input_option="USER_ENTERED",
    )
