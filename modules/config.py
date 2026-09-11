"""settings.yaml の読み込みと型付け。"""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel


class GoogleSettings(BaseModel):
    customer_master_sheet_id: str
    customer_master_tab: str
    profile_tab: str
    admin_log_tab: str


class SearchSettings(BaseModel):
    api_base_url: str
    lookback_days: int
    max_count: int
    timeout_seconds: int


class MatchingWeights(BaseModel):
    keyword: int
    region: int
    qualification: int
    price: int


class MatchingSettings(BaseModel):
    score_threshold: int
    max_recommendations_per_run: int = 20
    weights: MatchingWeights
    price_regex_patterns: list[str]


class EmailSettings(BaseModel):
    # 送信はGmail API(サービスアカウント+ドメイン全体の委任)で行う。
    # from_address は「送信元」であると同時に、委任でimpersonateする実在の
    # Workspaceユーザーでもある(この人として送る)。
    admin_address: str
    from_address: str
    # Stripeカスタマーポータル(解約・カード変更)。空文字のうちはフッターに
    # リンク行を出力しない。なお配信条件の変更は「メール返信」方式のため
    # 専用URLは持たない(フッターに固定の案内文を常時出力する)。
    customer_portal_url: str = ""
    # True にすると顧客(contact_email)へ直接自動送信し、管理者にはBccで同じものが
    # 届く。False(既定)だと管理者宛にだけ送られ、管理者が内容を確認して手で転送する。
    # 既定をFalseにしているのは、設定を1つ書き忘れただけで社外へ自動送信が始まる
    # 事故を防ぐため(明示的に有効化したときだけ自動送信になる)。
    auto_send_to_customer: bool = False


class CompanySettings(BaseModel):
    name: str


class AwardsSettings(BaseModel):
    """落札実績オープンデータ(参考落札相場)の設定。"""

    enabled: bool = True
    api_base_url: str = "https://api.p-portal.go.jp/pps-web-biz/UAB03/OAB0301"
    # 今年度を含め何年度分をさかのぼって相場計算に使うか(2 = 今年度+前年度)
    fiscal_year_lookback: int = 2
    timeout_seconds: int = 60


class Settings(BaseModel):
    google: GoogleSettings
    search: SearchSettings
    matching: MatchingSettings
    email: EmailSettings
    company: CompanySettings
    awards: AwardsSettings = AwardsSettings()


def load_settings(path: str | Path = "config/settings.yaml") -> Settings:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Settings.model_validate(raw)
