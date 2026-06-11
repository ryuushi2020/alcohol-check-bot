"""
スプレッドシートの写真URLを更新するスクリプト
実行すると E列 に7日間有効な署名付きURLが入り、
Google Sheetsからクリックして写真を閲覧できるようになる。

使い方:
  python refresh_photo_urls.py
"""

import os
import json
import boto3
import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

load_dotenv()

SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
R2_ACCESS_KEY_ID = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET_NAME = os.environ["R2_BUCKET_NAME"]
R2_ENDPOINT_URL = os.environ["R2_ENDPOINT_URL"]

URL_EXPIRY_SECONDS = 7 * 24 * 60 * 60  # 7日間

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def get_google_credentials():
    if os.path.exists(GOOGLE_SERVICE_ACCOUNT_JSON):
        return Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_JSON, scopes=SCOPES)
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    return Credentials.from_service_account_info(info, scopes=SCOPES)


def main():
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

    s3 = boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT_URL,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )

    updated = 0
    for i, row in enumerate(all_rows[data_start:], start=data_start + 1):
        filename = row[4] if len(row) > 4 else ""
        if not filename:
            continue

        # ファイル名っぽい文字列かチェック（URLが入っている行はスキップ）
        if filename.startswith("http"):
            continue

        try:
            url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": R2_BUCKET_NAME, "Key": filename},
                ExpiresIn=URL_EXPIRY_SECONDS,
            )
            ws.update_cell(i, 5, url)
            print(f"  更新: 行{i} {row[0]} {row[1]} → URL発行")
            updated += 1
        except Exception as e:
            print(f"  失敗: 行{i} {filename} — {e}")

    print(f"\n完了: {updated}件のURLを更新しました（7日間有効）")
    print("Google Sheetsを開いてE列のリンクをクリックすると写真を閲覧できます。")


if __name__ == "__main__":
    main()
