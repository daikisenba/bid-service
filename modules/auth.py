"""Google認証情報の読み込み(main.py・scripts/ から共用)。

Gmail送信は「サービスアカウント + ドメイン全体の委任」方式を用いる。
Google Workspaceは2025年にSMTPの基本認証(ユーザー名+アプリパスワード)を
廃止したため、Sheets用と同じサービスアカウントに gmail.send スコープを
ドメイン全体の委任で付与し、送信元ユーザーを impersonate して送る。
"""
from __future__ import annotations

import json
import os

import gspread
from google.oauth2.service_account import Credentials

_SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


def _service_account_info() -> dict:
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError("環境変数 GOOGLE_SERVICE_ACCOUNT_JSON が設定されていません")
    return json.loads(raw)


def build_gspread_client() -> gspread.Client:
    creds = Credentials.from_service_account_info(_service_account_info(), scopes=_SHEETS_SCOPES)
    return gspread.authorize(creds)


def build_gmail_service(sender: str):
    """送信元ユーザー(sender)を impersonate する Gmail API サービスを返す。

    ドメイン全体の委任で gmail.send を承認済みのサービスアカウントが前提。
    委任が未設定/未反映の場合は、送信・認証チェック時に HttpError(403 等)になる。
    """
    from googleapiclient.discovery import build

    creds = Credentials.from_service_account_info(
        _service_account_info(), scopes=_GMAIL_SCOPES, subject=sender
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)
