"""顧客専用シートへの書き込み・管理者宛メール送信・実行ログ記録。

顧客へ自動送信するかは settings.email.auto_send_to_customer で切り替える。
既定(False)では、生成した配信メール本文はすべて管理者(settings.email.admin_address)
宛にGmail APIで送信し、管理者が内容を確認したうえで顧客へ転送する運用とする。
Trueにすると顧客へ直接送信し、管理者にはBccで控えが届く(2026-09-11に1社目の
無料トライアル開始に伴い有効化。誤配信リスクを管理者確認で吸収しなくなるため、
マッチング条件の変更時は事前に --dry-run で出力を確認すること)。

メール送信はGmail API(サービスアカウント + ドメイン全体の委任)で行う。
Google Workspaceが2025年にSMTPの基本認証を廃止したため、SMTP+アプリパスワード
方式は使えない。送信元ユーザーをimpersonateしたGmailサービスをmain.py側で
生成し、send_recommend_email に渡す。
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import gspread

from .awards import AWARD_SOURCE_NOTE

# 顧客への送信可否判定はJST基準(顧客の営業日はJSTで動くため)。
_JST = timezone(timedelta(hours=9))
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
_EMPTY_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "recommend_mail_empty.md"


def _resolve_deadline(match: MatchResult) -> str:
    """締切日の表示値。kkj.go.jp API側の構造化値(period_end_time)を優先し、
    それが空のときだけLLM抽出値(llm_deadline)を補完として使う。LLMは公告文を
    読んで抽出するため誤読(幻覚)のリスクがあり、構造化データを上書きしない
    方針(確定値優先・LLM値は「補完」の位置づけ)。
    """
    if match.listing.period_end_time:
        return match.listing.period_end_time
    if match.llm_deadline:
        return f"{match.llm_deadline}(AI抽出・要確認)"
    return "要確認"


def _resolve_price(match: MatchResult) -> str:
    """予定価格の表示値。正規表現抽出(estimated_price)を優先し、それが
    失敗した場合のみLLM抽出値(llm_estimated_price)を補完として使う。
    """
    if match.estimated_price is not None:
        return f"¥{match.estimated_price:,}"
    if match.llm_estimated_price is not None:
        return f"¥{match.llm_estimated_price:,}(AI抽出・要確認)"
    return "要確認"


def _full_reasons(match: MatchResult) -> list[str]:
    """レコメンド理由。LLM判定コメントがあれば末尾に追記する。"""
    reasons = list(match.reasons)
    if match.llm_reason:
        reasons.append(f"AI判定: {match.llm_reason}")
    return reasons


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


def find_new_matches(
    gc: gspread.Client, customer: Customer, matches: list[MatchResult]
) -> list[MatchResult]:
    """既存シートと案件URLで照合し、まだ書き込まれていない新規分だけを返す
    (書き込みは行わない)。

    LLM関連性判定(modules/llm_judge.py)は新規分だけに適用したいため、
    「新規判定」と「シート書き込み」を分離してある。match_customer が返す
    候補には既にシートに書き込み済みの過去案件も含まれており(スコア上位
    max_recommendations_per_run件を毎回返す設計のため)、判定をここより前で
    行うと配信済みの過去案件まで毎回LLMに判定させてしまい、API呼び出しが
    無駄になる(結果の書き込み先も無い)。
    """
    sh = gc.open_by_key(customer.output_sheet_id)
    ws = sh.worksheet(RECOMMEND_TAB)
    existing = _existing_urls(ws)
    return [m for m in matches if m.listing.dedup_key not in existing]


def write_matches(gc: gspread.Client, customer: Customer, new_matches: list[MatchResult]) -> None:
    """新規判定済みのmatchesを顧客専用シートに書き込む。"""
    if not new_matches:
        return
    sh = gc.open_by_key(customer.output_sheet_id)
    ws = sh.worksheet(RECOMMEND_TAB)
    rows = [
        [
            m.listing.project_name,
            m.listing.organization_name or "",
            m.listing.cft_issue_date or "",
            _resolve_deadline(m),
            _resolve_price(m),
            m.listing.dedup_key,
            m.score,
            " / ".join(_full_reasons(m)),
            _award_cell(m.price_stats),
            "未確認",
        ]
        for m in new_matches
    ]
    ws.append_rows(rows, value_input_option="USER_ENTERED")


def append_new_matches(
    gc: gspread.Client,
    customer: Customer,
    matches: list[MatchResult],
    settings: Settings,
    *,
    dry_run: bool = False,
) -> list[MatchResult]:
    """マッチ結果を顧客専用シートに追記する。案件URLで重複チェックし、新規分のみ返す。

    find_new_matches + write_matches の合成(後方互換のため維持。LLM判定を
    間に挟みたい新しい呼び出し元は main.py のようにこの2関数を直接使う)。
    dry_run=True のときは重複チェックまで行い、シートへの追記は行わない
    (本番と同じ新着判定の結果だけを返す)。
    """
    new_matches = find_new_matches(gc, customer, matches)
    if not dry_run:
        write_matches(gc, customer, new_matches)
    return new_matches


def today_jst() -> str:
    """JST基準の今日の日付(YYYY-MM-DD)。1顧客1日1通の判定に使う。"""
    return datetime.now(_JST).strftime("%Y-%m-%d")


def already_sent_today(customer: Customer, today: str) -> bool:
    """今日(JST)、この顧客に既にレコメンドメールを送信済みかどうか。

    スケジュール実行の遅延で同日中に自動実行が複数回走る・手動で追い実行する・
    dry-run確認の直後に本番実行する、といったケースがあるため、実際の送信直前に
    これで確認して二重送信を防ぐ(1顧客1日1通)。
    """
    return bool(customer.last_sent_date) and customer.last_sent_date == today


def record_sent_date(gc: gspread.Client, customer: Customer, settings: Settings, sent_date: str) -> None:
    """顧客マスタの当該顧客行の「最終送信日」列を更新する。

    列が存在しない顧客マスタ(移行前)でも例外にせず静かに何もしない。
    """
    sh = gc.open_by_key(settings.google.customer_master_sheet_id)
    ws = sh.worksheet(settings.google.customer_master_tab)
    headers = ws.row_values(1)
    if "最終送信日" not in headers or "customer_id" not in headers:
        return
    date_col = headers.index("最終送信日") + 1
    id_col = headers.index("customer_id") + 1
    ids = ws.col_values(id_col)
    for row_number, cid in enumerate(ids, start=1):
        if row_number == 1:  # ヘッダー行はスキップ
            continue
        if cid == customer.customer_id:
            ws.update_cell(row_number, date_col, sent_date)
            return


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


def _sheet_view_url(sheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"


def _footer_lines(customer: Customer, settings: Settings) -> str:
    """配信メールのフッター(各種お手続き案内)。

    配信条件の変更は「メール返信」方式のため常に出力する(専用フォームは設けない)。
    1往復で完了するよう、返信の書き方の例と反映タイミングの目安を添える。
    Stripeカスタマーポータル(解約・カード変更)のリンクはURL設定時のみ出力する。
    過去配信分の一覧リンクは、顧客専用シート(output_sheet_id)に顧客本人を
    reader以上で招待した後に出す運用を前提とする(setup_customer_sheet.py参照)。
    """
    lines = []
    if customer.output_sheet_id:
        lines.append(f"・これまでにご案内した案件の一覧: {_sheet_view_url(customer.output_sheet_id)}")
    lines += [
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
    """レコメンドメール本文を組み立てる。

    matches が空でも必ず送る(0件の日も「新着なし」を明示的に伝える)。0件用の
    テンプレートは案件一覧を持たないため、通常テンプレートとは別ファイルにして
    「{listings}の後に0件でも成立する文」を無理に共存させない。
    """
    if not matches:
        template = _EMPTY_TEMPLATE_PATH.read_text(encoding="utf-8")
        return template.format(
            company_name=settings.company.name,
            customer_company_name=customer.company_name,
            footer=_footer_lines(customer, settings),
        )

    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    listing_lines = []
    for i, m in enumerate(matches, start=1):
        listing = m.listing
        listing_lines.append(
            f"{i}. {listing.project_name}\n"
            f"   発注機関: {listing.organization_name or '不明'}\n"
            f"   締切日時: {_resolve_deadline(m)}\n"
            f"   予定価格: {_resolve_price(m)}\n"
            f"   マッチ度: {m.score}点\n"
            f"   案件URL: {listing.dedup_key}\n"
            f"{_award_email_lines(m.price_stats)}"
        )
    body = template.format(
        company_name=settings.company.name,
        customer_company_name=customer.company_name,
        match_count=len(matches),
        listings="\n".join(listing_lines),
        footer=_footer_lines(customer, settings),
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
    """Gmail送信の認証・委任を単体で検証する(管理者宛にヘルスチェックメールを1通送る)。

    send_recommend_email は新着マッチがあるときしか呼ばれないため、新着0件が
    続くと送信経路の不調に気づけない(「実行成功」が誤って安全と解釈される)。
    この関数はマッチの有無に関係なく、本番と同じ messages.send 経路で
    管理者(自分)宛にテストメールを送り、委任・スコープ・送信元ユーザーを検証する。
    gmail.send スコープが許可するのは messages.send のみで、下書き作成
    (drafts.create)は別スコープが要るため、あえて実送信で確認する。
    """
    msg = MIMEMultipart()
    msg["Subject"] = "[bid-service] メール送信ヘルスチェック(自動送信)"
    msg["From"] = settings.email.from_address
    msg["To"] = settings.email.admin_address
    msg.attach(
        MIMEText(
            "これは bid-service の送信経路の確認用に自動送信されたメールです。"
            "このメールが届いていれば、Gmail API での配信基盤は正常です。",
            "plain",
            "utf-8",
        )
    )
    _gmail_send(gmail_service, msg)


def build_recommend_email(
    customer: Customer, matches: list[MatchResult], settings: Settings
) -> MIMEMultipart:
    """送信するレコメンドメールを組み立てて返す(送信はしない)。

    送信経路(send_recommend_email)と事前確認(--dry-run)が同じ関数で宛先・本文を
    組み立てるようにしてある。プレビューと本番で中身がズレると確認の意味がないため。

    settings.email.auto_send_to_customer が
      False(既定): 管理者宛にのみ送る。管理者が内容を確認して顧客へ転送する。
      True       : 顧客(contact_email)へ直接送り、管理者にはBccで同じものを送る。
                   Bccを必ず付けるのは、自動送信に切り替えても「何が社外に出たか」を
                   管理者が事後に確認できる状態を保つため(送信済みの控えが手元に残る)。
                   customer.cc_emails があれば、この場合のみCcとして追加する
                   (管理者確認モードでは、管理者が転送時に自分で宛先を判断するため付けない)。
    """
    body = _render_email_body(customer, matches, settings)
    subject = (
        f"【入札案件レコメンド】{customer.company_name}様 - {len(matches)}件"
        if matches
        else f"【入札案件レコメンド】{customer.company_name}様 - 本日は新着なし"
    )

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = settings.email.from_address

    if settings.email.auto_send_to_customer:
        if not customer.contact_email:
            raise RuntimeError(
                f"顧客 {customer.customer_id} に contact_email が未設定のため自動送信できません。"
                "顧客マスタのメールアドレス列を確認してください。"
            )
        msg["To"] = customer.contact_email
        if customer.cc_emails:
            msg["Cc"] = ", ".join(customer.cc_emails)
        msg["Bcc"] = settings.email.admin_address
        msg.attach(MIMEText(body, "plain", "utf-8"))
    else:
        notice = (
            f"[本メールは管理者確認用です。顧客への自動送信は行っていません。"
            f"内容を確認のうえ、{customer.contact_name}様({customer.contact_email})へ"
            f"転送してください。]\n\n"
        )
        msg["To"] = settings.email.admin_address
        msg.attach(MIMEText(notice + body, "plain", "utf-8"))

    return msg


def send_recommend_email(
    customer: Customer,
    matches: list[MatchResult],
    settings: Settings,
    gmail_service,
) -> None:
    """レコメンドメールを生成してGmail APIで送信する(宛先の決定は build_recommend_email)。"""
    _gmail_send(gmail_service, build_recommend_email(customer, matches, settings))


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
