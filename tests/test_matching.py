from __future__ import annotations

from modules.matching import match_customer, score_listing
from modules.models import BidListing, Customer, CustomerProfile


def _customer(**profile_kwargs) -> Customer:
    return Customer(
        customer_id="C001",
        company_name="サンプル商事",
        contact_name="佐藤",
        contact_email="sato@example.jp",
        plan="standard",
        status="active",
        output_sheet_id="SHEET_C001",
        profile=CustomerProfile(customer_id="C001", **profile_kwargs),
    )


def _listing(**kwargs) -> BidListing:
    defaults = dict(result_id="1", key="k1", external_document_uri="https://example.jp/1", project_name="案件")
    defaults.update(kwargs)
    return BidListing(**defaults)


def test_full_match_scores_100(settings):
    customer = _customer(
        keywords="消耗品", prefecture_codes="13,14", price_min=10000, price_max=500000, qualification_grades="C,D"
    )
    listing = _listing(
        project_name="消耗品(文具)の購入",
        lg_code="13",
        prefecture_name="東京都",
        certification=["C"],
        project_description="予定価格 120,000円",
    )
    result = score_listing(customer, listing, settings)
    assert result is not None
    assert result.score == 100
    assert result.price_confirmed is True
    assert result.estimated_price == 120000


def test_exclude_keyword_hard_excludes(settings):
    customer = _customer(keywords="消耗品", exclude_keywords="工事")
    listing = _listing(project_name="消耗品調達に伴う設置工事")
    assert score_listing(customer, listing, settings) is None


def test_region_outside_target_hard_excludes(settings):
    customer = _customer(keywords="印刷", prefecture_codes="13,14")
    listing = _listing(project_name="印刷業務委託", lg_code="27")
    assert score_listing(customer, listing, settings) is None


def test_region_unknown_is_soft_and_not_excluded(settings):
    customer = _customer(keywords="印刷", prefecture_codes="13,14")
    listing = _listing(project_name="印刷業務委託", lg_code=None)
    result = score_listing(customer, listing, settings)
    assert result is not None
    assert "要確認" in result.reasons[1]


def test_qualification_mismatch_hard_excludes(settings):
    customer = _customer(keywords="印刷", qualification_grades="C,D")
    listing = _listing(project_name="印刷業務委託", certification=["A"])
    assert score_listing(customer, listing, settings) is None


def test_no_restrictions_means_full_credit(settings):
    customer = _customer(keywords="印刷")
    listing = _listing(project_name="印刷業務委託")
    result = score_listing(customer, listing, settings)
    assert result is not None
    # keyword(50) + region(20,無指定) + qualification(20,無指定) + price(10,無指定) = 100
    assert result.score == 100


def test_price_out_of_range_loses_price_points_but_not_excluded(settings):
    customer = _customer(keywords="消耗品", price_min=10000, price_max=50000)
    listing = _listing(project_name="消耗品の購入", project_description="予定価格 900,000円")
    result = score_listing(customer, listing, settings)
    assert result is not None
    assert result.score == 90  # keyword50+region20+qualification20、priceのみ0
    assert result.price_confirmed is True


def test_match_customer_caps_at_max_recommendations_per_run(settings):
    """実E2Eで290件が全通過した問題の回帰テスト: 上位N件に制限される。"""
    settings.matching.max_recommendations_per_run = 5
    customer = _customer(keywords="消耗品")
    listings = [
        _listing(
            result_id=str(i),
            key=f"k{i}",
            external_document_uri=f"https://example.jp/{i}",
            project_name=f"消耗品の購入 その{i}",
        )
        for i in range(30)
    ]
    results = match_customer(customer, listings, settings)
    assert len(results) == 5


def test_match_customer_filters_by_threshold_and_sorts_desc(settings):
    customer = _customer(keywords="消耗品,印刷")
    listings = [
        _listing(result_id="1", key="k1", external_document_uri="https://example.jp/1", project_name="消耗品の購入"),
        _listing(result_id="2", key="k2", external_document_uri="https://example.jp/2", project_name="無関係の案件"),
    ]
    results = match_customer(customer, listings, settings)
    assert len(results) == 1
    assert results[0].listing.project_name == "消耗品の購入"
