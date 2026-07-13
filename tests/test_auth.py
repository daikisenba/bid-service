"""SMTP認証情報の正規化テスト。

535 BadCredentials の典型原因(アプリパスワードの空白・改行混入)を、
smtp_credentials() が正規化して吸収することを保証する。
"""
from __future__ import annotations

import pytest

from modules.auth import smtp_credentials


def test_strips_spaces_from_app_password(monkeypatch):
    monkeypatch.setenv("BID_SERVICE_SMTP_USER", "d.senba@souki-cp.co.jp")
    # Gmailは「xxxx xxxx xxxx xxxx」と空白区切りで表示する
    monkeypatch.setenv("BID_SERVICE_SMTP_PASSWORD", "abcd efgh ijkl mnop")
    user, password = smtp_credentials()
    assert user == "d.senba@souki-cp.co.jp"
    assert password == "abcdefghijklmnop"


def test_strips_leading_trailing_whitespace_and_newline(monkeypatch):
    monkeypatch.setenv("BID_SERVICE_SMTP_USER", "  d.senba@souki-cp.co.jp\n")
    monkeypatch.setenv("BID_SERVICE_SMTP_PASSWORD", "  abcdefghijklmnop\n")
    user, password = smtp_credentials()
    assert user == "d.senba@souki-cp.co.jp"
    assert password == "abcdefghijklmnop"


def test_raises_when_missing(monkeypatch):
    monkeypatch.delenv("BID_SERVICE_SMTP_USER", raising=False)
    monkeypatch.delenv("BID_SERVICE_SMTP_PASSWORD", raising=False)
    with pytest.raises(RuntimeError):
        smtp_credentials()


def test_raises_when_password_is_only_whitespace(monkeypatch):
    monkeypatch.setenv("BID_SERVICE_SMTP_USER", "d.senba@souki-cp.co.jp")
    monkeypatch.setenv("BID_SERVICE_SMTP_PASSWORD", "    ")
    with pytest.raises(RuntimeError):
        smtp_credentials()
