"""Google/SMTP認証情報の読み込み(main.py・scripts/ から共用)。"""
from __future__ import annotations

import json
import os

import gspread
from google.oauth2.service_account import Credentials

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def build_gspread_client() -> gspread.Client:
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError("環境変数 GOOGLE_SERVICE_ACCOUNT_JSON が設定されていません")
    creds = Credentials.from_service_account_info(json.loads(raw), scopes=_SCOPES)
    return gspread.authorize(creds)


def smtp_credentials() -> tuple[str, str]:
    user = os.environ.get("BID_SERVICE_SMTP_USER")
    password = os.environ.get("BID_SERVICE_SMTP_PASSWORD")
    if not user or not password:
        raise RuntimeError(
            "環境変数 BID_SERVICE_SMTP_USER / BID_SERVICE_SMTP_PASSWORD が設定されていません"
        )
    return user, password
