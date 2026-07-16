"""サービスアカウント情報の読み込みテスト。

Gmail送信は「サービスアカウント + ドメイン全体の委任」方式に移行したため、
SMTPのユーザー名/アプリパスワードは扱わない。ここでは環境変数
GOOGLE_SERVICE_ACCOUNT_JSON の読み込みと、未設定時のエラーを検証する。
"""
from __future__ import annotations

import json

import pytest

from modules.auth import _service_account_info


def test_service_account_info_parses_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", json.dumps({"client_email": "svc@example.iam"}))
    info = _service_account_info()
    assert info["client_email"] == "svc@example.iam"


def test_service_account_info_raises_when_missing(monkeypatch):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    with pytest.raises(RuntimeError):
        _service_account_info()
