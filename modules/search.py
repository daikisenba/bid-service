"""官公需情報ポータルサイト検索API(https://www.kkj.go.jp/api/)クライアント。

案件探索は顧客ごとに行わない。全アクティブ顧客のキーワード・対象地域を1つの
OR検索式に統合し、API呼び出し1回で候補案件プールを作成する(サイトへの
負荷を最小化するため。処理フロー仕様どおり)。

p-portal.go.jp のスクレイピングではなくこのAPIを使う理由: p-portal.go.jpは
ログインを前提としたブラウザ操作でしか案件検索ができず、無人バッチには適さない。
官公需情報ポータルサイト検索APIは認証不要・公式ドキュメント提供済みで、
キーワード・都道府県・カテゴリー・資格等級・期間による絞り込みが可能。
利用にあたっては官公需情報ポータルサイトの利用規約に従うこと。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import requests

from .config import Settings
from .models import BidListing, Customer

_RESERVED_TOKENS = ("ANDNOT", "AND", "OR", "NOT")


def _sanitize_keyword(raw: str) -> str:
    """顧客が入力したキーワードがAPI検索式の演算子と衝突しないように無害化する。"""
    cleaned = raw.strip().replace("(", "").replace(")", "")
    for token in _RESERVED_TOKENS:
        cleaned = re.sub(rf"(?i)(?:^|\s){token}(?:\s|$)", " ", cleaned)
    return cleaned.strip()


def _build_query(keywords: list[str]) -> str:
    sanitized = [k for k in (_sanitize_keyword(kw) for kw in keywords) if k]
    if not sanitized:
        raise ValueError("検索キーワードが1件もありません(顧客プロファイルを確認してください)")
    return " OR ".join(f"({kw})" for kw in sanitized)


def _text(el: ET.Element, tag: str) -> str | None:
    child = el.find(tag)
    if child is None or child.text is None:
        return None
    return child.text.strip()


def _parse_result(el: ET.Element) -> BidListing:
    certification_raw = _text(el, "Certification") or ""
    return BidListing(
        result_id=_text(el, "ResultId") or "",
        key=_text(el, "Key") or "",
        external_document_uri=_text(el, "ExternalDocumentURI"),
        project_name=_text(el, "ProjectName") or "",
        date=_text(el, "Date"),
        lg_code=_text(el, "LgCode"),
        prefecture_name=_text(el, "PrefectureName"),
        city_code=_text(el, "CityCode"),
        city_name=_text(el, "CityName"),
        organization_name=_text(el, "OrganizationName"),
        certification=certification_raw.split() if certification_raw else [],
        cft_issue_date=_text(el, "CftIssueDate"),
        period_end_time=_text(el, "PeriodEndTime"),
        category=_text(el, "Category"),
        procedure_type=_text(el, "ProcedureType"),
        location=_text(el, "Location"),
        tender_submission_deadline=_text(el, "TenderSubmissionDeadline"),
        opening_tenders_event=_text(el, "OpeningTendersEvent"),
        item_code=_text(el, "ItemCode"),
        project_description=_text(el, "ProjectDescription"),
    )


def _parse_response(xml_bytes: bytes) -> list[BidListing]:
    root = ET.fromstring(xml_bytes)
    error = root.find("Error")
    if error is not None:
        raise RuntimeError(f"kkj.go.jp API がエラーを返しました: {error.text}")
    return [_parse_result(el) for el in root.findall("./SearchResults/SearchResult")]


def fetch_candidate_pool(customers: list[Customer], settings: Settings) -> list[BidListing]:
    """全アクティブ顧客の条件を統合した1回のAPI呼び出しで案件プールを取得する。"""
    keyword_set: set[str] = set()
    prefecture_set: set[str] = set()
    for customer in customers:
        keyword_set.update(customer.profile.keywords)
        prefecture_set.update(customer.profile.prefecture_codes)

    params: dict[str, str] = {
        "Query": _build_query(sorted(keyword_set)),
        "Count": str(settings.search.max_count),
    }
    if prefecture_set:
        params["LG_Code"] = ",".join(sorted(prefecture_set))

    lookback_start = date.today() - timedelta(days=settings.search.lookback_days)
    params["CFT_Issue_Date"] = f"{lookback_start.isoformat()}/"

    response = requests.get(
        settings.search.api_base_url,
        params=params,
        timeout=settings.search.timeout_seconds,
    )
    response.raise_for_status()
    return _parse_response(response.content)
