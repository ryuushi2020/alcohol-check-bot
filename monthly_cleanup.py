"""
月次クリーンアップスクリプト（プリントアウト確認後に実行）
使い方:
  python monthly_cleanup.py          # 先月分
  python monthly_cleanup.py 2026-05  # 指定月

実行前に「本当に削除しますか？ (yes/no)」と確認を求めます。
"""

import os
import sys
import json
import boto3
import gspread
from datetime import date
from dateutil.relativedelta import relativedelta
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
R2_ACCESS_KEY_ID = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET_NAME = os.environ["R2_BUCKET_NAME"]
R2_ENDPOINT_URL = os.environ["R2_ENDPOINT_URL"]

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def get_google_credentials():
    if os.path.exists(GOOGLE_SERVICE_ACCOUNT_JSON):
        return Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_JSON, scopes=SCOPES)
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    return Credentials.from_service_account_info(info, scopes=SCOPES)


def get_r2_client():
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT_URL,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


def main():
    if len(sys.argv) > 1:
        target_month = sys.argv[1]
    else:
        last_month = date.today() - relativedelta(months=1)
        target_month = last_month.strftime("%Y-%m")

    print(f"対象月: {target_month}")

    # スプレッドシートから対象行を取得
    creds = get_google_credentials()
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.sheet1
    all_rows = ws.get_all_values()
    if not all_rows:
        print("データなし")
        return

    has_header = all_rows[0][0] in ("日付", "date")
    data_start = 1 if has_header else 0
    data_rows = all_rows[data_start:]

    # 対象行のインデックス（スプレッドシート上の1-based行番号）とファイル名を収集
    target = []
    for i, row in enumerate(data_rows):
        if row and row[0].startswith(target_month):
            sheet_row_num = i + data_start + 1  # 1-based
            filename = row[4] if len(row) > 4 else ""
            target.append((sheet_row_num, filename))

    if not target:
        print("対象データなし")
        return

    print(f"\n削除対象: {len(target)}件")
    for row_num, fname in target:
        print(f"  行{row_num}: {fname or '（写真なし）'}")

    confirm = input("\nプリントアウト済みですか？本当に削除しますか？ (yes/no): ").strip().lower()
    if confirm != "yes":
        print("キャンセルしました")
        return

    # R2から写真削除
    s3 = get_r2_client()
    deleted_photos = 0
    for _, filename in target:
        if not filename:
            continue
        try:
            s3.delete_object(Bucket=R2_BUCKET_NAME, Key=filename)
            print(f"  R2削除: {filename}")
            deleted_photos += 1
        except Exception as e:
            print(f"  R2削除失敗: {filename} — {e}")

    # スプレッドシートの行を後ろから削除（行番号がずれないよう逆順）
    deleted_rows = 0
    for row_num, _ in sorted(target, key=lambda x: x[0], reverse=True):
        try:
            ws.delete_rows(row_num)
            deleted_rows += 1
        except Exception as e:
            print(f"  行削除失敗: 行{row_num} — {e}")

    print(f"\n完了: R2写真 {deleted_photos}件削除、スプレッドシート {deleted_rows}行削除")


if __name__ == "__main__":
    main()
