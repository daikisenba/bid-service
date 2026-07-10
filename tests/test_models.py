"""CustomerProfileのカンマ区切りフィールドの回帰テスト。

背景: Google Sheets/gspreadの数値自動変換により「13,14,11,12」がint(13141112)
として読み込まれ、バリデーションエラーで顧客がスキップされる障害が実データの
E2Eで発生した(2026-07)。読み込み側はnumericise_ignore=["all"]で対策済みだが、
モデル側でも型に依らず受けられることをここで保証する。
"""
from __future__ import annotations

from modules.models import CustomerProfile


def _profile(**kwargs) -> CustomerProfile:
    return CustomerProfile(customer_id="C001", **kwargs)


def test_int_input_does_not_crash():
    """int(13141112)で来てもバリデーションエラーにしない。

    カンマ位置は既に失われているため1要素のリストになる(復元はできない。
    正しい値の取り込みはnumericise_ignore側の責務)。
    """
    profile = _profile(prefecture_codes=13141112)
    assert profile.prefecture_codes == ["13141112"]


def test_str_csv_input():
    profile = _profile(prefecture_codes="13,14,11,12")
    assert profile.prefecture_codes == ["13", "14", "11", "12"]


def test_leading_zero_is_preserved():
    profile = _profile(prefecture_codes="01,02")
    assert profile.prefecture_codes == ["01", "02"]


def test_fullwidth_separators():
    assert _profile(keywords="消耗品、印刷、封筒").keywords == ["消耗品", "印刷", "封筒"]
    assert _profile(keywords="消耗品，印刷").keywords == ["消耗品", "印刷"]


def test_empty_string_gives_empty_list():
    profile = _profile(prefecture_codes="", keywords="")
    assert profile.prefecture_codes == []
    assert profile.keywords == []
    assert profile.is_empty()


def test_float_input_drops_decimal_point():
    # gspreadがfloatで返すケース(例: 13.0)。"13.0"ではなく"13"として扱う
    profile = _profile(prefecture_codes=13.0)
    assert profile.prefecture_codes == ["13"]


def test_list_input_passes_through_with_str_cast():
    profile = _profile(prefecture_codes=[13, " 14 ", ""])
    assert profile.prefecture_codes == ["13", "14"]


def test_none_input_gives_empty_list():
    profile = _profile(prefecture_codes=None)
    assert profile.prefecture_codes == []


def test_whitespace_and_empty_elements_are_dropped():
    profile = _profile(keywords=" 消耗品 , , 印刷 ,")
    assert profile.keywords == ["消耗品", "印刷"]
