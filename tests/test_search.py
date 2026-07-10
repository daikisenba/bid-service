from __future__ import annotations

import pytest

from modules.search import _build_query, _parse_response, _sanitize_keyword, fetch_candidate_pool

_SAMPLE_XML = """<Results><Version>1.0</Version><SearchResults><SearchHits>2</SearchHits>
<SearchResult><ResultId>1</ResultId><Key>k1</Key>
<ExternalDocumentURI>https://example.jp/1</ExternalDocumentURI>
<ProjectName>消耗品の購入</ProjectName><LgCode>13</LgCode><PrefectureName>東京都</PrefectureName>
<OrganizationName>某省</OrganizationName><Certification>C D</Certification>
<CftIssueDate>2026-07-01</CftIssueDate><Category>物品</Category></SearchResult>
<SearchResult><ResultId>2</ResultId><Key>k2</Key>
<ProjectName>印刷業務</ProjectName></SearchResult>
</SearchResults></Results>"""

_ERROR_XML = "<Results><Error>invalid sort</Error></Results>"


def test_sanitize_keyword_strips_reserved_tokens_and_parens():
    assert _sanitize_keyword("消耗品") == "消耗品"
    assert _sanitize_keyword("印刷 AND 封筒") == "印刷 封筒"
    assert _sanitize_keyword("文具(事務用品)") == "文具事務用品"


def test_build_query_combines_with_or():
    query = _build_query(["消耗品", "印刷"])
    assert query == "(消耗品) OR (印刷)"


def test_build_query_raises_on_no_keywords():
    with pytest.raises(ValueError):
        _build_query([])


def test_parse_response_parses_fields_and_dedup_key():
    listings = _parse_response(_SAMPLE_XML.encode("utf-8"))
    assert len(listings) == 2

    first = listings[0]
    assert first.project_name == "消耗品の購入"
    assert first.lg_code == "13"
    assert first.certification == ["C", "D"]
    assert first.dedup_key == "https://example.jp/1"

    second = listings[1]
    # ExternalDocumentURIが無い場合はKeyがdedup_keyのフォールバックになる
    assert second.external_document_uri is None
    assert second.dedup_key == "k2"


def test_parse_response_raises_on_error_xml():
    with pytest.raises(RuntimeError, match="invalid sort"):
        _parse_response(_ERROR_XML.encode("utf-8"))


def test_fetch_candidate_pool_calls_api_with_combined_params(monkeypatch, settings):
    from modules.models import Customer, CustomerProfile

    customers = [
        Customer(
            customer_id="C001",
            company_name="A",
            contact_name="a",
            contact_email="a@example.jp",
            plan="standard",
            status="active",
            output_sheet_id="S1",
            profile=CustomerProfile(customer_id="C001", keywords="消耗品", prefecture_codes="13"),
        ),
        Customer(
            customer_id="C002",
            company_name="B",
            contact_name="b",
            contact_email="b@example.jp",
            plan="standard",
            status="active",
            output_sheet_id="S2",
            profile=CustomerProfile(customer_id="C002", keywords="印刷", prefecture_codes="27"),
        ),
    ]

    captured = {}

    class FakeResponse:
        content = _SAMPLE_XML.encode("utf-8")

        def raise_for_status(self):
            pass

    def fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("modules.search.requests.get", fake_get)

    listings = fetch_candidate_pool(customers, settings)

    assert len(listings) == 2
    assert captured["url"] == settings.search.api_base_url
    assert "消耗品" in captured["params"]["Query"]
    assert "印刷" in captured["params"]["Query"]
    assert captured["params"]["LG_Code"] == "13,27"
    assert captured["timeout"] == settings.search.timeout_seconds
