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
    plan: Literal["standard", "premium"]
    status: Literal["active", "paused", "trial"]
    output_sheet_id: str
    profile: CustomerProfile


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
        """顧客シートへの重複書き込みチェックに使うキー(案件URL優先)。"""
        return self.external_document_uri or self.key


class MatchResult(BaseModel):
    listing: BidListing
    customer_id: str
    score: int
    reasons: list[str]
    estimated_price: int | None = None
    price_confirmed: bool = False


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
