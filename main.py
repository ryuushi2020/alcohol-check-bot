import os
import json
import secrets
import boto3
import requests
from datetime import datetime, timezone, timedelta
from calendar import monthrange

import gspread
from fastapi import FastAPI, Request, HTTPException, Query
from google.oauth2.service_account import Credentials
from linebot.v3.webhook import WebhookParser
from linebot.v3.webhooks import MessageEvent, ImageMessageContent

app = FastAPI()

# --- 環境変数 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_CHANNEL_SECRET       = os.environ["LINE_CHANNEL_SECRET"]
SPREADSHEET_ID            = os.environ["SPREADSHEET_ID"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
R2_ACCESS_KEY_ID          = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY      = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET_NAME            = os.environ["R2_BUCKET_NAME"]
R2_ENDPOINT_URL           = os.environ["R2_ENDPOINT_URL"]
LINE_ADMIN_USER_ID        = os.environ.get("LINE_ADMIN_USER_ID", "")
REPORT_TOKEN              = os.environ.get("REPORT_TOKEN", "")

JST = timezone(timedelta(hours=9))
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def get_google_credentials():
    if os.path.exists(GOOGLE_SERVICE_ACCOUNT_JSON):
        return Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_JSON, scopes=SCOPES)
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    return Credentials.from_service_account_info(info, scopes=SCOPES)


def get_check_type(dt: datetime) -> str | None:
    h = dt.hour
    if 5 <= h < 12:
        return "朝"
    if 17 <= h < 23:
        return "夜"
    return None


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


def get_image_content(message_id: str) -> bytes:
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    res = requests.get(url, headers=headers, timeout=30)
    res.raise_for_status()
    return res.content


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
    return filename


def update_sheet(creds: Credentials, date_str: str, name: str, check_type: str, time_str: str, photo_filename: str):
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.sheet1

    all_values = ws.get_all_values()
    data_rows = all_values[1:] if all_values and all_values[0][0] in ("日付", "date", "A") else all_values

    target_row_index = None
    for i, row in enumerate(data_rows):
        if row[0] == date_str and row[1] == name if len(row) > 1 else False:
            offset = 2 if all_values and all_values[0][0] in ("日付", "date", "A") else 1
            target_row_index = i + offset
            break

    col_map = {"朝": 3, "夜": 4}
    time_col = col_map[check_type]

    if target_row_index:
        ws.update_cell(target_row_index, time_col, time_str)
        ws.update_cell(target_row_index, 5, photo_filename)
    else:
        new_row = [date_str, name, "", "", ""]
        new_row[time_col - 1] = time_str
        new_row[4] = photo_filename
        ws.append_row(new_row, value_input_option="USER_ENTERED")


def send_line_push(user_id: str, text: str):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "to": user_id,
        "messages": [{"type": "text", "text": text}],
    }
    res = requests.post(url, headers=headers, json=payload, timeout=10)
    res.raise_for_status()


def build_monthly_report(target_month: str) -> str:
    """先月分の個人別集計テキストを生成する"""
    creds = get_google_credentials()
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(SPREADSHEET_ID).sheet1
    all_rows = ws.get_all_values()
    if not all_rows:
        return f"{target_month} のデータがありません。"

    has_header = all_rows[0][0] in ("日付", "date")
    data = all_rows[1:] if has_header else all_rows
    month_rows = [r for r in data if r and r[0].startswith(target_month)]

    if not month_rows:
        return f"{target_month} のデータがありません。"

    year, month = map(int, target_month.split("-"))
    total_days = monthrange(year, month)[1]

    # 個人ごとに集計
    stats: dict[str, dict] = {}
    for row in month_rows:
        name    = row[1] if len(row) > 1 else "不明"
        morning = row[2] if len(row) > 2 else ""
        evening = row[3] if len(row) > 3 else ""
        if name not in stats:
            stats[name] = {"morning": 0, "evening": 0}
        if morning:
            stats[name]["morning"] += 1
        if evening:
            stats[name]["evening"] += 1

    lines = [
        f"📋 {target_month} アルコールチェック月次レポート",
        f"対象日数: {total_days}日",
        "─" * 20,
    ]
    for name, s in sorted(stats.items()):
        m_rate = f"{s['morning']}/{total_days}日"
        e_rate = f"{s['evening']}/{total_days}日"
        m_mark = "✅" if s["morning"] >= total_days * 0.8 else "⚠️"
        e_mark = "✅" if s["evening"] >= total_days * 0.8 else "⚠️"
        lines.append(f"👤 {name}")
        lines.append(f"  朝: {m_rate} {m_mark}")
        lines.append(f"  夜: {e_rate} {e_mark}")

    lines += [
        "─" * 20,
        f"計 {len(stats)}名のデータがあります。",
        "",
        "📁 PDF出力:",
        "python3 monthly_export.py",
        "",
        "🗑 印刷後の削除:",
        "python3 monthly_cleanup.py",
    ]
    return "\n".join(lines)


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

        user_id  = event.source.user_id
        group_id = getattr(event.source, "group_id", None)

        # 管理者登録用: user_idをログに出力（初回設定時に確認する）
        print(f"[WEBHOOK] user_id={user_id} group_id={group_id}")

        if group_id:
            display_name = get_group_member_display_name(group_id, user_id)
        else:
            display_name = get_display_name(user_id)

        date_str = now_jst.strftime("%Y-%m-%d")
        time_str = now_jst.strftime("%H:%M:%S")
        filename = f"{display_name}_{date_str}_{check_type}.jpg"

        image_bytes = get_image_content(event.message.id)
        photo_filename = upload_to_r2(image_bytes, filename)
        update_sheet(creds, date_str, display_name, check_type, time_str, photo_filename)

    return {"status": "ok"}


# --- 月次レポートエンドポイント（cron-job.org から毎月1日に叩く） ---
@app.get("/monthly-report")
async def monthly_report(token: str = Query(...)):
    if not REPORT_TOKEN or not secrets.compare_digest(token, REPORT_TOKEN):
        raise HTTPException(status_code=403, detail="Forbidden")

    if not LINE_ADMIN_USER_ID:
        raise HTTPException(status_code=500, detail="LINE_ADMIN_USER_ID not set")

    now_jst = datetime.now(JST)
    # 先月を計算
    first_of_this_month = now_jst.replace(day=1)
    last_month = first_of_this_month - timedelta(days=1)
    target_month = last_month.strftime("%Y-%m")

    report_text = build_monthly_report(target_month)
    send_line_push(LINE_ADMIN_USER_ID, report_text)

    return {"status": "sent", "month": target_month}
