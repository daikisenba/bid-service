"""顧客専用シートへの書き込み・管理者宛メール送信・実行ログ記録。

顧客への自動送信は行わない(誤配信リスクの排除)。フェーズ1では、生成した
配信メール本文はすべて管理者(settings.email.admin_address)宛にSMTP送信し、
管理者が内容を確認したうえで顧客へ転送する運用とする。Gmail下書き作成方式
(顧客ごとのGmail下書き)はOAuth設定が別途必要なため、フェーズ2で検討する。
"""
from __future__ import annotations

import smtplib
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
    )
    # 参考落札相場を1件でも掲載したら出典を明記する(利用条件)
    if any(m.price_stats and m.price_stats.count > 0 for m in matches):
        body += f"\n{AWARD_SOURCE_NOTE}\n"
    return body


def send_recommend_email(
    customer: Customer,
    matches: list[MatchResult],
    settings: Settings,
    smtp_user: str,
    smtp_password: str,
) -> None:
    """レコメンドメールを生成し、管理者宛にSMTP送信する(顧客への自動送信は行わない)。"""
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

    with smtplib.SMTP(settings.email.smtp_host, settings.email.smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)


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
