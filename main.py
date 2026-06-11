import os
import json
import boto3
import requests
from datetime import datetime, timezone, timedelta

import gspread
from fastapi import FastAPI, Request, HTTPException
from google.oauth2.service_account import Credentials
from linebot.v3.webhook import WebhookParser
from linebot.v3.webhooks import MessageEvent, ImageMessageContent

app = FastAPI()

# --- 環境変数 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
R2_ACCESS_KEY_ID = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET_NAME = os.environ["R2_BUCKET_NAME"]
R2_ENDPOINT_URL = os.environ["R2_ENDPOINT_URL"]

JST = timezone(timedelta(hours=9))

# --- Google認証（Sheets専用） ---
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def get_google_credentials():
    if os.path.exists(GOOGLE_SERVICE_ACCOUNT_JSON):
        return Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_JSON, scopes=SCOPES)
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    return Credentials.from_service_account_info(info, scopes=SCOPES)

# --- チェック種別判定 ---
def get_check_type(dt: datetime) -> str | None:
    h = dt.hour
    if 5 <= h < 12:
        return "朝"
    if 17 <= h < 23:
        return "夜"
    return None

# --- LINE表示名取得 ---
def get_display_name(user_id: str) -> str:
    url = f"https://api.line.me/v2/bot/profile/{user_id}"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    res = requests.get(url, headers=headers, timeout=10)
    if res.status_code == 200:
        return res.json().get("displayName", user_id)
    return user_id

def get_group_member_display_name(group_id: str, user_id: str) -> str:
    url = f"https://api.line.me/v2/bot/group/{group_id}/member/{user_id}"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    res = requests.get(url, headers=headers, timeout=10)
    if res.status_code == 200:
        return res.json().get("displayName", user_id)
    return user_id

# --- 写真バイナリ取得 ---
def get_image_content(message_id: str) -> bytes:
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    res = requests.get(url, headers=headers, timeout=30)
    res.raise_for_status()
    return res.content

# --- Cloudflare R2 に写真保存（プライベートバケット） ---
def upload_to_r2(image_bytes: bytes, filename: str) -> str:
    s3 = boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT_URL,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )
    s3.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=filename,
        Body=image_bytes,
        ContentType="image/jpeg",
    )
    # ファイル名だけ返す（月次エクスポート時にR2から取得する）
    return filename

# --- スプレッドシート更新 ---
def update_sheet(creds: Credentials, date_str: str, name: str, check_type: str, time_str: str, photo_url: str):
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.sheet1

    all_values = ws.get_all_values()
    data_rows = all_values[1:] if all_values and all_values[0][0] in ("日付", "date", "A") else all_values

    target_row_index = None
    for i, row in enumerate(data_rows):
        row_date = row[0] if len(row) > 0 else ""
        row_name = row[1] if len(row) > 1 else ""
        if row_date == date_str and row_name == name:
            offset = 2 if all_values and all_values[0][0] in ("日付", "date", "A") else 1
            target_row_index = i + offset
            break

    col_map = {"朝": 3, "夜": 4}
    time_col = col_map[check_type]

    if target_row_index:
        ws.update_cell(target_row_index, time_col, time_str)
        ws.update_cell(target_row_index, 5, photo_url)
    else:
        new_row = [date_str, name, "", "", ""]
        new_row[time_col - 1] = time_str
        new_row[4] = photo_url
        ws.append_row(new_row, value_input_option="USER_ENTERED")

# --- Webhook エンドポイント ---
parser = WebhookParser(LINE_CHANNEL_SECRET)

@app.post("/webhook")
async def webhook(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()

    try:
        events = parser.parse(body.decode("utf-8"), signature)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid signature")

    creds = get_google_credentials()

    for event in events:
        if not isinstance(event, MessageEvent):
            continue
        if not isinstance(event.message, ImageMessageContent):
            continue

        now_jst = datetime.fromtimestamp(event.timestamp / 1000, tz=JST)
        check_type = get_check_type(now_jst)
        if check_type is None:
            continue

        user_id = event.source.user_id
        group_id = getattr(event.source, "group_id", None)
        if group_id:
            display_name = get_group_member_display_name(group_id, user_id)
        else:
            display_name = get_display_name(user_id)

        date_str = now_jst.strftime("%Y-%m-%d")
        time_str = now_jst.strftime("%H:%M:%S")
        filename = f"{display_name}_{date_str}_{check_type}.jpg"

        image_bytes = get_image_content(event.message.id)
        photo_url = upload_to_r2(image_bytes, filename)
        update_sheet(creds, date_str, display_name, check_type, time_str, photo_url)

    return {"status": "ok"}
