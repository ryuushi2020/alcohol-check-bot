import os
import json
import requests
from datetime import datetime, timezone, timedelta

import gspread
from fastapi import FastAPI, Request, HTTPException
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from linebot.v3.webhook import WebhookParser
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi
from linebot.v3.webhooks import MessageEvent, ImageMessageContent

app = FastAPI()

# --- 環境変数 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
DRIVE_FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]  # JSON文字列 or ファイルパス

JST = timezone(timedelta(hours=9))

# --- Google認証 ---
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

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
    # グループメンバーの場合はグループIDが必要なので呼び出し元で処理
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

# --- Google Drive に写真保存 ---
DRIVE_OWNER_EMAIL = os.environ.get("DRIVE_OWNER_EMAIL", "katsuto.uehara@gmail.com")

def upload_to_drive(image_bytes: bytes, filename: str, creds: Credentials) -> str:
    import io
    drive = build("drive", "v3", credentials=creds)
    file_metadata = {"name": filename, "parents": [DRIVE_FOLDER_ID]}
    media = MediaIoBaseUpload(io.BytesIO(image_bytes), mimetype="image/jpeg")
    f = drive.files().create(body=file_metadata, media_body=media, fields="id, webViewLink").execute()
    file_id = f["id"]
    # オーナーをGoogleアカウントに移転（サービスアカウントはクォータゼロのため必須）
    drive.permissions().create(
        fileId=file_id,
        body={"type": "user", "role": "owner", "emailAddress": DRIVE_OWNER_EMAIL},
        transferOwnership=True,
    ).execute()
    # 公開設定（閲覧可能リンク）
    drive.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
    ).execute()
    return f["webViewLink"]

# --- スプレッドシート更新 ---
def update_sheet(creds: Credentials, date_str: str, name: str, check_type: str, time_str: str, photo_url: str):
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.sheet1

    all_values = ws.get_all_values()
    # ヘッダー行があれば1行目はスキップ（なければそのまま）
    data_rows = all_values[1:] if all_values and all_values[0][0] in ("日付", "date", "A") else all_values

    # 同日・同名の行を探す
    target_row_index = None
    for i, row in enumerate(data_rows):
        row_date = row[0] if len(row) > 0 else ""
        row_name = row[1] if len(row) > 1 else ""
        if row_date == date_str and row_name == name:
            # all_values上のインデックス（ヘッダーオフセット込み・1-based）
            offset = 2 if all_values and all_values[0][0] in ("日付", "date", "A") else 1
            target_row_index = i + offset
            break

    col_map = {"朝": 3, "夜": 4}  # C列=3, D列=4 (1-based)
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
            continue  # 対象時間外は無視

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
        photo_url = upload_to_drive(image_bytes, filename, creds)
        update_sheet(creds, date_str, display_name, check_type, time_str, photo_url)

    return {"status": "ok"}
