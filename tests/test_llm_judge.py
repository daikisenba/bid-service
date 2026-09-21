from __future__ import annotations

import json

import pytest

from modules.customer import _build_profile
from modules.llm_judge import judge_relevance
from modules.models import BidListing, Customer, CustomerProfile, MatchResult


def _customer(business_description: str = "") -> Customer:
    return Customer(
        customer_id="C004",
        company_name="日建リース工業株式会社",
        contact_name="渡邊",
        contact_email="showsuke@example.jp",
        plan="standard",
        status="active",
        output_sheet_id="SHEET_C004",
        profile=CustomerProfile(
            customer_id="C004",
            keywords="防災,備蓄",
            exclude_keywords="工事",
            business_description=business_description,
        ),
    )


def _match(dedup_key: str = "https://example.jp/1", project_name: str = "防災備蓄倉庫整備業務") -> MatchResult:
    listing = BidListing(
        result_id="1",
        # dedup_key は常に key を返す仕様(2026-09-21〜)なので、このヘルパーの
        # dedup_key引数はkeyフィールドにそのまま渡す。external_document_uriは
        # 別の値(URLの体裁)を持たせ、display_url経由でのみ使われることを示す。
        key=dedup_key,
        external_document_uri=f"{dedup_key}-detail",
        project_name=project_name,
        organization_name="某市",
        project_description="防災備蓄用の簡易ベッド・毛布等の購入業務",
    )
    return MatchResult(
        listing=listing,
        customer_id="C004",
        score=80,
        reasons=["キーワード一致(案件名): 防災"],
        exclude_keywords_matched=["工事"],
    )


class _FakeResponse:
    def __init__(self, body: dict, status_ok: bool = True):
        self._body = body
        self._status_ok = status_ok

    def raise_for_status(self):
        if not self._status_ok:
            raise RuntimeError("HTTP error")

    def json(self):
        return self._body


def _anthropic_response(text: str) -> _FakeResponse:
    return _FakeResponse({"content": [{"text": text}]})


def test_judge_relevance_marks_relevant_and_irrelevant(monkeypatch, settings):
    customer = _customer()
    relevant_match = _match(dedup_key="https://example.jp/1")
    irrelevant_match = _match(dedup_key="https://example.jp/2", project_name="庁舎解体工事")
    matches = [relevant_match, irrelevant_match]

    response_json = json.dumps(
        {
            "results": [
                {
                    "dedup_key": "https://example.jp/1",
                    "relevant": True,
                    "reason": "防災用品の物品購入案件",
                    "deadline": "2026-10-15",
                    "estimated_price": 500000,
                },
                {
                    "dedup_key": "https://example.jp/2",
                    "relevant": False,
                    "reason": "解体工事であり物品購入ではない",
                    "deadline": None,
                    "estimated_price": None,
                },
            ]
        }
    )
    monkeypatch.setattr(
        "modules.llm_judge.requests.post",
        lambda url, headers=None, json=None, timeout=None: _anthropic_response(response_json),
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    judge_relevance(customer, matches, settings)

    assert relevant_match.llm_relevant is True
    assert relevant_match.llm_reason == "防災用品の物品購入案件"
    assert relevant_match.llm_deadline == "2026-10-15"
    assert relevant_match.llm_estimated_price == 500000

    assert irrelevant_match.llm_relevant is False
    assert irrelevant_match.llm_reason == "解体工事であり物品購入ではない"


def test_judge_relevance_parses_response_wrapped_in_code_fence(monkeypatch, settings):
    customer = _customer()
    matches = [_match()]
    wrapped = "```json\n" + json.dumps(
        {"results": [{"dedup_key": "https://example.jp/1", "relevant": True, "reason": "ok",
                      "deadline": None, "estimated_price": None}]}
    ) + "\n```"
    monkeypatch.setattr(
        "modules.llm_judge.requests.post",
        lambda url, headers=None, json=None, timeout=None: _anthropic_response(wrapped),
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    judge_relevance(customer, matches, settings)
    assert matches[0].llm_relevant is True


def test_judge_relevance_matches_by_dedup_key_not_order(monkeypatch, settings):
    """レスポンスの順序が候補順と一致しなくてもdedup_keyで正しく紐付く。"""
    customer = _customer()
    m1 = _match(dedup_key="https://example.jp/1")
    m2 = _match(dedup_key="https://example.jp/2")
    response_json = json.dumps(
        {
            "results": [
                {"dedup_key": "https://example.jp/2", "relevant": False, "reason": "x",
                 "deadline": None, "estimated_price": None},
                {"dedup_key": "https://example.jp/1", "relevant": True, "reason": "y",
                 "deadline": None, "estimated_price": None},
            ]
        }
    )
    monkeypatch.setattr(
        "modules.llm_judge.requests.post",
        lambda url, headers=None, json=None, timeout=None: _anthropic_response(response_json),
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    judge_relevance(customer, [m1, m2], settings)
    assert m1.llm_relevant is True
    assert m2.llm_relevant is False


def test_judge_relevance_fails_open_on_http_error(monkeypatch, settings):
    customer = _customer()
    matches = [_match()]

    def boom(url, headers=None, json=None, timeout=None):
        raise RuntimeError("network down")

    monkeypatch.setattr("modules.llm_judge.requests.post", boom)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    judge_relevance(customer, matches, settings)  # 例外を投げない
    assert matches[0].llm_relevant is None


def test_judge_relevance_fails_open_on_malformed_json(monkeypatch, settings):
    customer = _customer()
    matches = [_match()]
    monkeypatch.setattr(
        "modules.llm_judge.requests.post",
        lambda url, headers=None, json=None, timeout=None: _anthropic_response("これはJSONではありません"),
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    judge_relevance(customer, matches, settings)
    assert matches[0].llm_relevant is None


def test_judge_relevance_fails_open_on_missing_api_key(monkeypatch, settings):
    customer = _customer()
    matches = [_match()]
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    called = []
    monkeypatch.setattr(
        "modules.llm_judge.requests.post",
        lambda *a, **kw: called.append(1),
    )

    judge_relevance(customer, matches, settings)
    assert matches[0].llm_relevant is None
    assert called == []  # APIキーが無い時点で呼び出し自体をしない


def test_judge_relevance_skips_when_disabled(monkeypatch, settings):
    settings.llm.enabled = False
    customer = _customer()
    matches = [_match()]

    called = []
    monkeypatch.setattr("modules.llm_judge.requests.post", lambda *a, **kw: called.append(1))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    judge_relevance(customer, matches, settings)
    assert called == []
    assert matches[0].llm_relevant is None


def test_judge_relevance_skips_when_no_matches(monkeypatch, settings):
    customer = _customer()
    called = []
    monkeypatch.setattr("modules.llm_judge.requests.post", lambda *a, **kw: called.append(1))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    judge_relevance(customer, [], settings)
    assert called == []


def test_business_description_loaded_from_profile_row():
    """事業内容列が未設定の既存顧客でも壊れない(後方互換)。"""
    profile = _build_profile("C001", {"対象業種・品目キーワード": "消耗品"})
    assert profile.business_description == ""

    profile2 = _build_profile("C004", {"対象業種・品目キーワード": "防災", "事業内容": "防災用品のレンタル・販売"})
    assert profile2.business_description == "防災用品のレンタル・販売"
