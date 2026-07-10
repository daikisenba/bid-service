"""外販版入札支援サービス フェーズ1 日次バッチのエントリポイント。

処理フロー:
1. 顧客マスタから status=active の顧客一覧を取得する(不正・空プロファイルはスキップ)
2. kkj.go.jp APIで案件プールを1回だけ取得する(顧客ごとに探索し直さない)
3. 顧客ごとにマッチング→重複チェック付きでシート追記→新着があれば管理者宛メール送信
   (1顧客の処理で例外が発生しても、残りの顧客の処理は継続する)
4. 実行結果サマリを顧客マスタの実行ログタブに記録する
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

from modules.customer import load_active_customers
from modules.delivery import append_new_matches, send_recommend_email, write_admin_summary
from modules.matching import match_customer
from modules.models import CustomerError
from modules.search import fetch_candidate_pool
from modules.config import load_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _build_gspread_client() -> gspread.Client:
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError("環境変数 GOOGLE_SERVICE_ACCOUNT_JSON が設定されていません")
    creds = Credentials.from_service_account_info(json.loads(raw), scopes=_SCOPES)
    return gspread.authorize(creds)


def _smtp_credentials() -> tuple[str, str]:
    user = os.environ.get("BID_SERVICE_SMTP_USER")
    password = os.environ.get("BID_SERVICE_SMTP_PASSWORD")
    if not user or not password:
        raise RuntimeError(
            "環境変数 BID_SERVICE_SMTP_USER / BID_SERVICE_SMTP_PASSWORD が設定されていません"
        )
    return user, password


def run(settings_path: str = "config/settings.yaml") -> int:
    run_started_at = datetime.now(timezone.utc)
    settings = load_settings(settings_path)
    gc = _build_gspread_client()
    smtp_user, smtp_password = _smtp_credentials()

    load_result = load_active_customers(gc, settings)
    customers = load_result.customers
    skipped = load_result.skipped
    for s in skipped:
        logger.warning("顧客 %s をスキップしました: %s", s.customer_id, s.reason)

    if not customers:
        logger.warning("有効な顧客がいないため、案件探索をスキップします。")
        write_admin_summary(
            gc,
            settings,
            run_started_at=run_started_at,
            processed=0,
            skipped=skipped,
            total_matches=0,
            errors=[],
        )
        return 0

    try:
        candidate_pool = fetch_candidate_pool(customers, settings)
    except Exception as exc:  # noqa: BLE001 - 全体を止めず管理者ログに記録する
        logger.error("案件探索(kkj.go.jp API)に失敗しました: %s", exc)
        write_admin_summary(
            gc,
            settings,
            run_started_at=run_started_at,
            processed=0,
            skipped=skipped,
            total_matches=0,
            errors=[CustomerError(customer_id="(全体)", error=f"案件探索失敗: {exc}")],
        )
        return 1

    logger.info("案件プール取得: %d件", len(candidate_pool))

    total_matches = 0
    errors: list[CustomerError] = []
    processed = 0

    for customer in customers:
        try:
            matches = match_customer(customer, candidate_pool, settings)
            new_matches = append_new_matches(gc, customer, matches, settings)
            if new_matches:
                send_recommend_email(customer, new_matches, settings, smtp_user, smtp_password)
            total_matches += len(new_matches)
            processed += 1
            logger.info(
                "顧客 %s (%s): マッチ%d件中 新着%d件",
                customer.customer_id,
                customer.company_name,
                len(matches),
                len(new_matches),
            )
        except Exception as exc:  # noqa: BLE001 - 1顧客の失敗で全体を止めない
            logger.exception("顧客 %s の処理でエラーが発生しました", customer.customer_id)
            errors.append(CustomerError(customer_id=customer.customer_id, error=str(exc)))
            continue

    write_admin_summary(
        gc,
        settings,
        run_started_at=run_started_at,
        processed=processed,
        skipped=skipped,
        total_matches=total_matches,
        errors=errors,
    )
    logger.info(
        "実行完了: 処理%d件 / スキップ%d件 / 新着マッチ%d件 / エラー%d件",
        processed,
        len(skipped),
        total_matches,
        len(errors),
    )
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="外販版入札支援サービス 日次バッチ")
    parser.add_argument("--config", default="config/settings.yaml", help="settings.yamlのパス")
    args = parser.parse_args()
    return run(args.config)


if __name__ == "__main__":
    sys.exit(main())
