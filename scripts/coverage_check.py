"""過去応札案件のカバレッジ検証スクリプト。

kkj.go.jp APIが、過去に実際応札した案件のキーワード・発注機関名でどの程度
ヒットするかを検証する。これは技術的な確認ではなく商品定義の確認であり、
自社の入札実績が「オープンカウンタ(少額)」中心だった場合、本APIで拾えない
可能性は商品の根幹に関わる。ヒット率が低ければ、トライアル顧客に売る前に
商品説明の見直し(例:「一般競争入札の案件レコメンド」への変更)や
フェーズ2以降での追加データソース検討が必要になる。

入力: config/past_bids_reference.csv (列: keyword, organization_name, note)
     過去に実際に応札した案件を1行1件で追記して使用する。正確な案件名で
     なくてもよく、品目キーワードと発注機関名が分かれば十分。
"""
from __future__ import annotations

import csv
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.config import load_settings  # noqa: E402

_CSV_PATH = Path(__file__).resolve().parent.parent / "config" / "past_bids_reference.csv"


def _read_reference_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with open(_CSV_PATH, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            keyword = (row.get("keyword") or "").strip()
            organization_name = (row.get("organization_name") or "").strip()
            if not keyword and not organization_name:
                continue
            rows.append(
                {"keyword": keyword, "organization_name": organization_name, "note": row.get("note", "") or ""}
            )
    return rows


def _query(settings, keyword: str, organization_name: str) -> tuple[int, list[str]]:
    params: dict[str, str] = {"Count": "20"}
    if keyword:
        params["Query"] = keyword
    if organization_name:
        params["Organization_Name"] = organization_name
    if len(params) == 1:  # Countしか無い = 検索条件が空
        return 0, []

    response = requests.get(
        settings.search.api_base_url, params=params, timeout=settings.search.timeout_seconds
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)

    error = root.find("Error")
    if error is not None:
        return 0, [f"[APIエラー: {error.text}]"]

    hits = int(root.findtext("./SearchResults/SearchHits") or "0")
    titles = [
        (el.findtext("ProjectName") or "").strip() for el in root.findall("./SearchResults/SearchResult")
    ][:5]
    return hits, titles


def main() -> int:
    settings = load_settings("config/settings.yaml")
    reference_rows = _read_reference_rows()

    if not reference_rows:
        print(f"{_CSV_PATH} に過去応札案件が1件も登録されていません。")
        print("keyword, organization_name の列に過去の応札実績を追記してから再実行してください。")
        return 1

    hit_count = 0
    print(f"{'キーワード':<20}{'発注機関':<20}{'ヒット件数':>10}  サンプル案件名")
    print("-" * 100)
    for row in reference_rows:
        hits, titles = _query(settings, row["keyword"], row["organization_name"])
        if hits > 0:
            hit_count += 1
        sample = " / ".join(titles) if titles else "(なし)"
        print(f"{row['keyword']:<20}{row['organization_name']:<20}{hits:>10}  {sample}")

    total = len(reference_rows)
    rate = hit_count / total * 100
    print("-" * 100)
    print(f"カバレッジ: {hit_count}/{total} 件が1件以上ヒット ({rate:.1f}%)")
    if rate < 50:
        print(
            "\n⚠️ ヒット率が低めです。フェーズ2以降に進む前に、商品説明の見直し"
            "(例:「一般競争入札の案件レコメンド」への変更)や追加データソースの"
            "検討を行ってください。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
