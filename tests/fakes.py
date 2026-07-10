"""実際のGoogle Sheets/Gmailに接続せずロジックを検証するためのフェイク。

本物のgspread.Client/Worksheetが提供するメソッドのうち、本プロジェクトが
実際に使う範囲だけを再現した最小限のスタブ。
"""
from __future__ import annotations

from dataclasses import dataclass, field


class WorksheetNotFound(Exception):
    pass


@dataclass
class FakeWorksheet:
    header: list[str]
    rows: list[list[object]] = field(default_factory=list)

    def get_all_records(self, numericise_ignore: list | None = None) -> list[dict[str, object]]:
        if numericise_ignore == ["all"]:
            # 本物のgspreadと同様、数値変換なし=全セル文字列で返す
            return [dict(zip(self.header, [str(v) for v in row])) for row in self.rows]
        return [dict(zip(self.header, row)) for row in self.rows]

    def col_values(self, col: int) -> list[str]:
        values = [self.header[col - 1]]
        for row in self.rows:
            values.append(str(row[col - 1]) if col - 1 < len(row) else "")
        return values

    def append_row(self, row: list[object], value_input_option: str | None = None) -> None:
        self.rows.append(row)

    def append_rows(self, rows: list[list[object]], value_input_option: str | None = None) -> None:
        self.rows.extend(rows)

    def row_values(self, row_number: int) -> list[object]:
        if row_number == 1:
            return self.header
        idx = row_number - 2
        return self.rows[idx] if 0 <= idx < len(self.rows) else []

    def update(self, values: list[list[object]], range_name: str | None = None) -> None:
        self.header = list(values[0])

    def freeze(self, rows: int | None = None, cols: int | None = None) -> None:
        pass

    def add_validation(self, *args, **kwargs) -> None:
        pass


@dataclass
class FakeSpreadsheet:
    worksheets: dict[str, FakeWorksheet]

    def worksheet(self, name: str) -> FakeWorksheet:
        if name not in self.worksheets:
            raise WorksheetNotFound(name)
        return self.worksheets[name]

    def add_worksheet(self, title: str, rows: int, cols: int) -> FakeWorksheet:
        ws = FakeWorksheet(header=[])
        self.worksheets[title] = ws
        return ws


@dataclass
class FakeGspreadClient:
    spreadsheets: dict[str, FakeSpreadsheet]

    def open_by_key(self, key: str) -> FakeSpreadsheet:
        if key not in self.spreadsheets:
            raise KeyError(f"スプレッドシートが見つかりません(アクセス権がない可能性): {key}")
        return self.spreadsheets[key]
