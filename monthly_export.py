"""
月次エクスポートスクリプト
使い方:
  python monthly_export.py          # 先月分
  python monthly_export.py 2026-05  # 指定月
"""

import os
import sys
import json
import io
import boto3
import gspread
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime, date
from dateutil.relativedelta import relativedelta
from google.oauth2.service_account import Credentials
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Image, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont

# --- 環境変数 ---
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
R2_ACCESS_KEY_ID = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET_NAME = os.environ["R2_BUCKET_NAME"]
R2_ENDPOINT_URL = os.environ["R2_ENDPOINT_URL"]

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


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


def fetch_month_rows(target_month: str):
    """スプレッドシートから指定月の行を返す（target_month: "2026-05"）"""
    creds = get_google_credentials()
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.sheet1
    all_rows = ws.get_all_values()
    if not all_rows:
        return []
    header = all_rows[0]
    data = all_rows[1:] if header[0] in ("日付", "date") else all_rows
    return [row for row in data if row and row[0].startswith(target_month)]


def download_photo(s3, filename: str) -> bytes | None:
    try:
        obj = s3.get_object(Bucket=R2_BUCKET_NAME, Key=filename)
        return obj["Body"].read()
    except Exception as e:
        print(f"  写真取得失敗: {filename} — {e}")
        return None


def build_pdf(rows, target_month: str, output_path: str):
    pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))

    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
    )

    styles = getSampleStyleSheet()
    jp_style = ParagraphStyle("jp", fontName="HeiseiKakuGo-W5", fontSize=10)
    title_style = ParagraphStyle("title", fontName="HeiseiKakuGo-W5", fontSize=14, spaceAfter=6)

    s3 = get_r2_client()
    story = []

    story.append(Paragraph(f"アルコールチェック記録　{target_month}", title_style))
    story.append(Spacer(1, 4 * mm))

    for row in rows:
        date_str = row[0] if len(row) > 0 else ""
        name = row[1] if len(row) > 1 else ""
        morning = row[2] if len(row) > 2 else ""
        evening = row[3] if len(row) > 3 else ""
        filename = row[4] if len(row) > 4 else ""

        # テキスト行
        check_text = []
        if morning:
            check_text.append(f"朝: {morning}")
        if evening:
            check_text.append(f"夜: {evening}")
        check_str = "　".join(check_text) if check_text else "（記録なし）"

        info = Paragraph(f"{date_str}　{name}　{check_str}", jp_style)

        # 写真
        photo_img = None
        if filename:
            photo_bytes = download_photo(s3, filename)
            if photo_bytes:
                img_buf = io.BytesIO(photo_bytes)
                photo_img = Image(img_buf, width=40 * mm, height=40 * mm)
                photo_img.hAlign = "LEFT"

        if photo_img:
            table_data = [[info, photo_img]]
            t = Table(table_data, colWidths=[120 * mm, 45 * mm])
            t.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]))
            story.append(t)
        else:
            story.append(info)

        story.append(Spacer(1, 2 * mm))

    doc.build(story)
    print(f"PDF生成完了: {output_path}")


def main():
    if len(sys.argv) > 1:
        target_month = sys.argv[1]  # 例: "2026-05"
    else:
        last_month = date.today() - relativedelta(months=1)
        target_month = last_month.strftime("%Y-%m")

    print(f"対象月: {target_month}")
    rows = fetch_month_rows(target_month)
    if not rows:
        print("対象データなし")
        return

    print(f"{len(rows)}件取得")
    output_path = f"alcohol_check_{target_month}.pdf"
    build_pdf(rows, target_month, output_path)


if __name__ == "__main__":
    main()
