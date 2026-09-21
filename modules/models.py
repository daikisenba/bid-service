"""顧客マスタ・案件・マッチング結果の Pydantic モデル。"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# 半角カンマのほか、シート手入力で紛れ込みやすい全角カンマ・読点も区切りとして扱う
_CSV_SEPARATORS = re.compile(r"[,，、]")


def _split_csv(value: object) -> object:
    """カンマ区切り文字列をリスト化する。

    Google Sheets/gspreadの数値自動変換で「13,14」がint(1314)になるケースが
    あるため、int/floatで来ても文字列にキャストして処理する(読み込み側でも
    numericise_ignoreで変換を止めているが、モデル側でも防御する)。
    ただし「13,14,11,12」→13141112のようにカンマ位置が既に失われた値は復元
    できないため、正しい取り込みは読み込み側の設定に依存する。
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, float) and value.is_integer():
        value = int(value)  # str(13.0)が"13.0"になるのを防ぐ
    if isinstance(value, (int, str)):
        return [item.strip() for item in _CSV_SEPARATORS.split(str(value)) if item.strip()]
    return value


class CustomerProfile(BaseModel):
    customer_id: str
    keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    prefecture_codes: list[str] = Field(default_factory=list)
    price_min: int | None = None
    price_max: int | None = None
    organization_types: list[str] = Field(default_factory=list)
    qualification_grades: list[str] = Field(default_factory=list)
    # 自由記述の事業内容(1段落程度)。keywordsだけでは伝わらない文脈を
    # LLM判定(modules/llm_judge.py)のプロンプトに渡すために使う。
    # 空文字でもkeywordsだけで動く(後方互換、未設定の既存顧客も壊れない)。
    business_description: str = ""

    _split_fields = field_validator(
        "keywords",
        "exclude_keywords",
        "prefecture_codes",
        "organization_types",
        "qualification_grades",
        mode="before",
    )(_split_csv)

    @field_validator("price_min", "price_max", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if value == "" or value is None:
            return None
        return value

    def is_empty(self) -> bool:
        """条件が何も設定されていない(=マッチしようがない)プロファイルかどうか。"""
        return not (self.keywords or self.prefecture_codes or self.qualification_grades)


class Customer(BaseModel):
    customer_id: str
    company_name: str
    contact_name: str
    contact_email: str
    # 主担当者(contact_email)以外にも同じ内容を届けたい場合のCc宛先。
    # カンマ区切りで複数指定可(_split_csvで正規化)。空なら通常通りCcなしで送る。
    cc_emails: list[str] = Field(default_factory=list)
    plan: Literal["standard", "premium"]
    status: Literal["active", "paused", "trial"]
    output_sheet_id: str
    profile: CustomerProfile

    _split_cc = field_validator("cc_emails", mode="before")(_split_csv)
    # JST基準の日付文字列(YYYY-MM-DD)。この顧客に最後にレコメンドメールを送った日。
    # 1日1通に制限するための判定に使う(スケジュール実行の遅延・手動再実行・
    # dry-run後の本番実行など、1日に複数回バッチが走っても二重送信しないため)。
    last_sent_date: str = ""


class BidListing(BaseModel):
    """官公需情報ポータルサイト検索APIの1件分(SearchResult)。"""

    result_id: str
    key: str
    external_document_uri: str | None = None
    project_name: str
    date: str | None = None
    lg_code: str | None = None
    prefecture_name: str | None = None
    city_code: str | None = None
    city_name: str | None = None
    organization_name: str | None = None
    certification: list[str] = Field(default_factory=list)
    cft_issue_date: str | None = None
    period_end_time: str | None = None
    category: str | None = None
    procedure_type: str | None = None
    location: str | None = None
    tender_submission_deadline: str | None = None
    opening_tenders_event: str | None = None
    item_code: str | None = None
    project_description: str | None = None

    @property
    def dedup_key(self) -> str:
        """顧客シートへの重複書き込みチェック・LLM判定結果の突合に使う一意キー。

        常に key(kkj.go.jp APIが払い出す内部識別子)を使う。2026-09-21実測
        (候補389件)でkeyの重複はゼロで完全に一意。一方 external_document_uri は
        発注機関によっては個別の詳細ページではなくp-portal等の検索トップページを
        指すことがあり、無関係な複数案件が同一URLを持つケースが実測で見つかった
        (例: 内閣官房の複数案件が全て同じp-portalトップページを指していた)。
        これを重複キーに使うと、URLが同じというだけで別案件が「配信済み」と
        誤判定され握りつぶされる不具合になっていた(2026-09-21修正)。
        表示用のリンクが必要な場合は display_url を使うこと。
        """
        return self.key

    @property
    def display_url(self) -> str:
        """メール本文・シートに表示する、顧客がクリックする案件詳細リンク。

        可能な限り external_document_uri(発注機関の公告詳細ページ)を優先し、
        取得できない場合のみ内部識別子(key)にフォールバックする
        (keyはURLではなくBase64文字列のため、開いても詳細は見られない)。
        """
        return self.external_document_uri or self.key


class AwardRecord(BaseModel):
    """落札実績オープンデータ(調達ポータル)の1件。列は生値のまま保持する
    (種別コード・機関コードの公式コード表は現状デコードせず生値表示)。"""

    project_id: str
    project_name: str
    award_date: str | None = None
    award_amount: int | None = None  # 落札金額。公表値(税込/税抜は非明示のため加工しない)
    type_code: str | None = None
    org_code: str | None = None
    winner_name: str | None = None
    corporate_number: str | None = None


class AwardExample(BaseModel):
    """相場欄に載せる落札実例。"""

    project_name: str
    amount: int
    winner: str | None = None


class PriceStats(BaseModel):
    """同種過去落札の相場統計。count=0 は「相場データなし」を意味する。"""

    count: int
    median: int | None = None
    p25: int | None = None
    p75: int | None = None
    examples: list[AwardExample] = Field(default_factory=list)


class MatchResult(BaseModel):
    listing: BidListing
    customer_id: str
    score: int
    reasons: list[str]
    estimated_price: int | None = None
    price_confirmed: bool = False
    # 参考落札相場(フェーズ2 ステップ①)。None は相場照合を行わなかったことを表し、
    # count=0 は照合したが同種案件が見つからなかったことを表す(両者は区別する)。
    price_stats: PriceStats | None = None
    # 除外キーワードに一致した語(案件名のみ対象)。2026-09-21〜、ハード除外はせず
    # ここに記録してLLM判定(modules/llm_judge.py)のシグナルとして渡す。
    exclude_keywords_matched: list[str] = Field(default_factory=list)
    # LLM関連性判定。None=未判定/判定失敗(fail-open、この状態では除外しない)。
    llm_relevant: bool | None = None
    llm_reason: str | None = None
    # 公告文からLLMが抽出した締切日・予定価格(API構造化値が取れない案件の補完用)。
    llm_deadline: str | None = None
    llm_estimated_price: int | None = None


class SkipReason(BaseModel):
    """処理をスキップした顧客とその理由(管理者ログ用)。"""

    customer_id: str
    reason: str


class CustomerError(BaseModel):
    """顧客単位の処理中に発生したエラー(管理者ログ用)。"""

    customer_id: str
    error: str


class CustomerLoadResult(BaseModel):
    customers: list[Customer]
    skipped: list[SkipReason]
