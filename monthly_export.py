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
from datetime import date
from dateutil.relativedelta import relativedelta
from google.oauth2.service_account import Credentials
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Image,
    Paragraph, Spacer, HRFlowable
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont

SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
R2_ACCESS_KEY_ID = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET_NAME = os.environ["R2_BUCKET_NAME"]
R2_ENDPOINT_URL = os.environ["R2_ENDPOINT_URL"]

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# レイアウト定数
PHOTO_SIZE = 38 * mm       # 写真の正方形サイズ
COL_DATE   = 28 * mm       # 日付列幅
COL_NAME   = 30 * mm       # 氏名列幅
COL_CHECK  = 40 * mm       # チェック時刻列幅
COL_PHOTO  = PHOTO_SIZE + 4 * mm  # 写真列幅
PAGE_W     = A4[0] - 30 * mm      # 印刷幅（左右余白15mmずつ）


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


def download_photo(s3, filename: str):
    if not filename or filename.startswith("http"):
        return None
    try:
        obj = s3.get_object(Bucket=R2_BUCKET_NAME, Key=filename)
        return obj["Body"].read()
    except Exception as e:
        print(f"  写真取得失敗: {filename} — {e}")
        return None


def make_photo_image(photo_bytes: bytes):
    """アスペクト比を保持しつつ PHOTO_SIZE の正方形に収める"""
    from PIL import Image as PILImage
    pil = PILImage.open(io.BytesIO(photo_bytes))
    w, h = pil.size
    # 短辺を基準にクロップして正方形にする
    side = min(w, h)
    left = (w - side) // 2
    top  = (h - side) // 2
    pil = pil.crop((left, top, left + side, top + side))
    buf = io.BytesIO()
    pil.save(buf, format="JPEG", quality=85)
    buf.seek(0)
    return Image(buf, width=PHOTO_SIZE, height=PHOTO_SIZE)


def build_pdf(rows, target_month: str, output_path: str):
    pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))

    FONT = "HeiseiKakuGo-W5"
    title_style  = ParagraphStyle("title",  fontName=FONT, fontSize=14, spaceAfter=2)
    header_style = ParagraphStyle("hdr",    fontName=FONT, fontSize=9,  textColor=colors.white)
    cell_style   = ParagraphStyle("cell",   fontName=FONT, fontSize=10)
    date_style   = ParagraphStyle("date",   fontName=FONT, fontSize=11, textColor=colors.HexColor("#333333"))

    def cell(text):
        return Paragraph(text or "—", cell_style)

    s3 = get_r2_client()

    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont(FONT, 8)
        canvas.setFillColor(colors.grey)
        canvas.drawRightString(A4[0] - 15 * mm, 8 * mm, f"{doc.page} ページ")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=15 * mm, bottomMargin=18 * mm,
    )

    story = []

    # タイトル
    story.append(Paragraph(f"アルコールチェック記録　{target_month}", title_style))
    story.append(Spacer(1, 2 * mm))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#4a90d9")))
    story.append(Spacer(1, 3 * mm))

    # ヘッダー行
    hdr_data = [[
        Paragraph("日付",         header_style),
        Paragraph("氏名",         header_style),
        Paragraph("朝チェック",   header_style),
        Paragraph("夜チェック",   header_style),
        Paragraph("写真",         header_style),
    ]]
    col_widths = [COL_DATE, COL_NAME, COL_CHECK, COL_CHECK, COL_PHOTO]
    hdr_table = Table(hdr_data, colWidths=col_widths, rowHeights=7 * mm)
    hdr_table.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, -1), colors.HexColor("#4a90d9")),
        ("TEXTCOLOR",    (0, 0), (-1, -1), colors.white),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(hdr_table)

    # データ行（日付ごとにグループ化）
    rows_by_date: dict[str, list] = {}
    for row in rows:
        d = row[0] if row else ""
        rows_by_date.setdefault(d, []).append(row)

    alt = False
    for date_str in sorted(rows_by_date.keys()):
        group = rows_by_date[date_str]
        for row in group:
            name    = row[1] if len(row) > 1 else ""
            morning = row[2] if len(row) > 2 else ""
            evening = row[3] if len(row) > 3 else ""
            filename= row[4] if len(row) > 4 else ""

            # 写真取得
            photo_cell = Paragraph("なし", cell_style)
            photo_bytes = download_photo(s3, filename)
            if photo_bytes:
                try:
                    photo_cell = make_photo_image(photo_bytes)
                except Exception as e:
                    print(f"  画像変換失敗: {e}")

            row_data = [[
                cell(date_str),
                cell(name),
                cell(morning),
                cell(evening),
                photo_cell,
            ]]
            row_height = PHOTO_SIZE + 4 * mm if photo_bytes else 10 * mm
            bg = colors.HexColor("#f0f5ff") if alt else colors.white
            alt = not alt

            t = Table(row_data, colWidths=col_widths, rowHeights=row_height)
            t.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, -1), bg),
                ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN",         (4, 0), (4, -1),  "CENTER"),
                ("GRID",          (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
                ("LEFTPADDING",   (0, 0), (-1, -1), 4),
                ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
                ("TOPPADDING",    (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]))
            story.append(t)

        # 日付の区切り
        story.append(Spacer(1, 0.5 * mm))

    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(f"以上　{target_month}　全{len(rows)}件", cell_style))

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    print(f"PDF生成完了: {output_path}  ({len(rows)}件)")


def main():
    if len(sys.argv) > 1:
        target_month = sys.argv[1]
    else:
        last_month = date.today() - relativedelta(months=1)
        target_month = last_month.strftime("%Y-%m")

    print(f"対象月: {target_month}")
    rows = fetch_month_rows(target_month)
    if not rows:
        print("対象データなし")
        return

    print(f"{len(rows)}件取得、PDF生成中...")
    output_path = f"alcohol_check_{target_month}.pdf"
    build_pdf(rows, target_month, output_path)


if __name__ == "__main__":
    main()
