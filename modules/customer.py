"""顧客マスタ・条件プロファイルの読み込みとバリデーション。

不正・空のプロファイルを持つ顧客はここで弾き、後続処理(探索・マッチング・配信)には
一切登場させない。弾いた理由は SkipReason として呼び出し側に返し、管理者ログに記録する。
"""
from __future__ import annotations

import gspread
from pydantic import ValidationError

from .config import Settings
from .models import Customer, CustomerLoadResult, CustomerProfile, SkipReason

_ACTIVE_STATUS = "active"


def _build_profile(customer_id: str, row: dict[str, object]) -> CustomerProfile:
    return CustomerProfile(
        customer_id=customer_id,
        keywords=row.get("対象業種・品目キーワード", ""),
        exclude_keywords=row.get("除外キーワード", ""),
        prefecture_codes=row.get("対象地域", ""),
        price_min=row.get("予定価格下限") or None,
        price_max=row.get("予定価格上限") or None,
        organization_types=row.get("発注機関の種別", ""),
        qualification_grades=row.get("資格等級", ""),
    )


def load_active_customers(gc: gspread.Client, settings: Settings) -> CustomerLoadResult:
    """顧客マスタから status=active の顧客一覧を、条件プロファイルと結合して返す。

    プロファイルが存在しない/不正/空の顧客はスキップし、理由を記録する。
    """
    sh = gc.open_by_key(settings.google.customer_master_sheet_id)
    # numericise_ignore=["all"]: gspreadの数値自動変換を無効化し全セルを文字列で
    # 受け取る。変換を許すと「13,14,11,12」(都道府県コード)がint(13141112)に
    # 潰れてカンマ位置が失われ、「01」(北海道)の先頭ゼロも欠落するため。
    master_rows = sh.worksheet(settings.google.customer_master_tab).get_all_records(
        numericise_ignore=["all"]
    )
    profile_rows = sh.worksheet(settings.google.profile_tab).get_all_records(
        numericise_ignore=["all"]
    )

    profiles_by_id: dict[str, dict[str, object]] = {}
    for row in profile_rows:
        cid = str(row.get("customer_id", "")).strip()
        if cid:
            profiles_by_id[cid] = row

    customers: list[Customer] = []
    skipped: list[SkipReason] = []

    for row in master_rows:
        customer_id = str(row.get("customer_id", "")).strip()
        if not customer_id:
            continue

        status = str(row.get("ステータス", "")).strip()
        if status != _ACTIVE_STATUS:
            continue

        profile_row = profiles_by_id.get(customer_id)
        if profile_row is None:
            skipped.append(SkipReason(customer_id=customer_id, reason="条件プロファイルが見つかりません"))
            continue

        try:
            profile = _build_profile(customer_id, profile_row)
        except ValidationError as exc:
            skipped.append(SkipReason(customer_id=customer_id, reason=f"条件プロファイルの形式が不正です: {exc}"))
            continue

        if profile.is_empty():
            skipped.append(SkipReason(customer_id=customer_id, reason="条件プロファイルが空です"))
            continue

        try:
            customer = Customer(
                customer_id=customer_id,
                company_name=str(row.get("会社名", "")),
                contact_name=str(row.get("担当者名", "")),
                contact_email=str(row.get("メールアドレス", "")),
                plan=row.get("プラン", "standard"),
                status=status,
                output_sheet_id=str(row.get("出力先スプレッドシートID", "")),
                profile=profile,
            )
        except ValidationError as exc:
            skipped.append(SkipReason(customer_id=customer_id, reason=f"顧客マスタの形式が不正です: {exc}"))
            continue

        if not customer.output_sheet_id:
            skipped.append(SkipReason(customer_id=customer_id, reason="出力先スプレッドシートIDが未設定です"))
            continue

        customers.append(customer)

    return CustomerLoadResult(customers=customers, skipped=skipped)
