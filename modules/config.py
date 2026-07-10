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
    weights: MatchingWeights
    price_regex_patterns: list[str]


class EmailSettings(BaseModel):
    admin_address: str
    from_address: str
    smtp_host: str
    smtp_port: int


class CompanySettings(BaseModel):
    name: str


class Settings(BaseModel):
    google: GoogleSettings
    search: SearchSettings
    matching: MatchingSettings
    email: EmailSettings
    company: CompanySettings


def load_settings(path: str | Path = "config/settings.yaml") -> Settings:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Settings.model_validate(raw)
