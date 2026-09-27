import os
import sys
import json
from datetime import datetime, timedelta, timezone
import requests

import google.auth
from googleapiclient.discovery import build
import vertexai
from vertexai.generative_models import GenerativeModel

# ==============================================================================
# CONFIGURATION
# ==============================================================================
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "prj-s-ai-bot-4rl8")
GCP_LOCATION = os.getenv("GCP_LOCATION", "us-central1")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

DAILY_SHEET_ID = os.getenv("GOOGLE_SHEET_FILE_ID", "1NwJd8uzGIcDTxE0XJ_VYbqJfQnjuqxB4wh5UR97SIlk")
DAILY_TAB_NAME = os.getenv("GOOGLE_WORKSHEET_NAME", "Daily Changes")
JIRA_SERVER = os.getenv("JIRA_SERVER", "https://osttra.atlassian.net")

IST = timezone(timedelta(hours=5, minutes=30))

# ==============================================================================
# EMOJI RESOLVER WITH SIREN & CONSTRUCTION EMOJIS
# ==============================================================================
def resolve_status_and_emoji(raw_status):
    """
    - Done / Completed -> ✅
    - In Progress / Implementing / Started -> ⚙️
    - Pending / Scheduled / To Do -> ⏳
    - Failed -> 🚨 (Siren)
    - Backed Out / Rescheduled / Cancelled -> 🚧 (Construction)
    """
    s = str(raw_status).strip().lower()
    if any(w in s for w in ["done", "completed", "closed", "resolved", "success"]):
        return "Done", "✅"
    elif any(w in s for w in ["in progress", "work in progress", "implementing", "started"]):
        return "In Progress", "⚙️"
    elif "fail" in s:
        return "Failed", "🚨"
    elif any(w in s for w in ["back", "reschedule", "cancel"]):
        return "Backed Out/Rescheduled", "🚧"
    else:
        return "Pending", "⏳"

# ==============================================================================
# SHEET READER (SHEET IS SOURCE OF TRUTH)
# ==============================================================================
def fetch_active_deployments():
    scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive.readonly']
    creds, _ = google.auth.default(scopes=scopes)
    sheets_api = build('sheets', 'v4', credentials=creds)
    drive_api = build('drive', 'v3', credentials=creds)

    now = datetime.now(IST)
    weekday = now.weekday()
    sheet_id = DAILY_SHEET_ID
    tab_name = DAILY_TAB_NAME
    is_weekend = weekday >= 5

    if is_weekend:
        sat = now if weekday == 5 else now - timedelta(days=1)
        sun = now + timedelta(days=1) if weekday == 5 else now
        filename = f"{sat.strftime('%d-%m-%Y')} & {sun.strftime('%d-%m-%Y')}"
        q = f"name contains '{filename}' and mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false"
        res = drive_api.files().list(q=q, fields="files(id, name)").execute()
        files = res.get('files', [])
        if files:
            sheet_id = files[0]['id']
            tab_name = "Sheet1"

    vals = sheets_api.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:L"
    ).execute().get('values', [])

    deployments = []
    today_str = now.strftime("%d/%m/%Y")
    today_day_name = now.strftime("%A")

    if is_weekend:
        current_section = None
        for row in vals:
            if not row or not row[0]: continue
            c0 = str(row[0]).strip()
            if c0 in ["Saturday", "Sunday"]:
                current_section = c0
                continue
            if current_section == today_day_name:
                key = c0.split('", "')[-1].replace('")', '').strip() if '", "' in c0 else c0
                if key not in ["Change", "CR"]:
                    raw_stat = row[7].strip() if len(row) > 7 else "Pending"
                    st_name, st_emoji = resolve_status_and_emoji(raw_stat)
                    deployments.append({
                        "change_no": key,
                        "env": row[1].strip() if len(row) > 1 else "PROD",
                        "application": row[4].strip() if len(row) > 4 else "",
                        "desc": row[3].strip() if len(row) > 3 else "",
                        "owner": row[9].strip() if len(row) > 9 else "TOC",
                        "status": st_name,
                        "status_emoji": st_emoji,
                        "comments": row[11].strip() if len(row) > 11 else ""
                    })
    else:
        in_block = False
        for row in vals:
            if not row or not row[0]: continue
            c0 = str(row[0]).strip()
            if today_str in c0:
                in_block = True
                continue
            elif in_block and ('/' in c0 or '-' in c0):
                break

            if in_block and len(row) > 1 and row[1]:
                raw_k = str(row[1]).strip()
                key = raw_k.split('", "')[-1].replace('")', '').strip() if '", "' in raw_k else raw_k
                if key not in ["CR", "Change"]:
                    raw_stat = row[8].strip() if len(row) > 8 else "Pending"
                    st_name, st_emoji = resolve_status_and_emoji(raw_stat)
                    deployments.append({
                        "change_no": key,
                        "env": row[2].strip() if len(row) > 2 else "PROD",
                        "application": row[4].strip() if len(row) > 4 else "",
                        "desc": row[5].strip() if len(row) > 5 else "",
                        "owner": row[6].strip() if len(row) > 6 else "TOC",
                        "status": st_name,
                        "status_emoji": st_emoji,
                        "comments": ""
                    })

    return deployments

# ==============================================================================
# VERTEX AI (GEMINI 2.5 FLASH) BRAIN
# ==============================================================================
def run_gemini_analysis(deployments):
    if not deployments:
        return None

    vertexai.init(project=GCP_PROJECT_ID, location=GCP_LOCATION)
    model = GenerativeModel("gemini-2.5-flash")

    prompt = f"""
    Analyze this active deployment status data from Google Sheets (The Source of Truth).
    Data:
    {json.dumps(deployments, indent=2)}

    You are an intelligent Release Manager. Do the following:
    1. Write a 1-sentence "executive_summary" summarizing today's overall deployment health.
    2. Create a "short_desc" (MAX 3 to 5 words, strictly under 20 characters).
    3. Retain the exact "status_emoji" provided in the input!

    OUTPUT STRICT VALID JSON ONLY (No markdown):
    {{
        "executive_summary": "...",
        "deployments": [
            {{
                "change_no": "...",
                "application": "...",
                "short_desc": "...",
                "environment": "...",
                "implementer": "...",
                "status": "...",
                "status_emoji": "...",
                "comments": "..."
            }}
        ]
    }}
    """
    try:
        resp = model.generate_content(prompt)
        clean = resp.text.strip().replace("```json", "").replace("```", "")
        return json.loads(clean)
    except Exception as e:
        print(f"❌ Gemini analysis error: {e}", file=sys.stderr)
        return None

# ==============================================================================
# CHAT SENDER (MATCHING INPUT_FILE_28.PNG)
# ==============================================================================
def broadcast_to_gchat(card_data):
    if not card_data or not WEBHOOK_URL:
        print("⚠️ No card data or WEBHOOK_URL not configured.")
        return

    summary = card_data.get("executive_summary", "")
    deps = card_data.get("deployments", [])

    headers = ["Change No", "App", "Desc", "Env", "St's", "Owner"]
    rows = []
    for d in deps:
        c_no = str(d.get('change_no', 'N/A'))[:15]
        app = str(d.get('application', 'N/A'))[:8]
        desc = str(d.get('short_desc', 'N/A'))[:20]
        env = str(d.get('environment', 'N/A'))[:6]
        emoji = d.get('status_emoji', '⏳')
        owner = str(d.get('implementer', 'N/A')).split(" ")[0][:10]
        rows.append([c_no, app, desc, env, emoji, owner])

    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            display_length = len(cell) - 1 if any(e in cell for e in ['✅','⚙️','⏳','🚨','🚧']) else len(cell)
            col_widths[i] = max(col_widths[i], display_length)

    header_line = "| " + " | ".join(h.ljust(w) for h, w in zip(headers, col_widths)) + " |"
    separator_line = "|" + "|".join("-" * (w + 2) for w in col_widths) + "|"
    table_lines = [header_line, separator_line]

    for row in rows:
        row_line = "|"
        for i, cell in enumerate(row):
            if i == 4:
                row_line += f" " + (" " * (col_widths[i] - 1)) + f"{cell}|"
            else:
                row_line += f" {cell.ljust(col_widths[i])} |"
        table_lines.append(row_line)

    table_content = "\n".join(table_lines)
    today_date = datetime.now(IST).strftime('%d/%m/%Y')

    # Live Radar: Actionable Insights & Friction Points
    comments_section = ""
    for d in deps:
        key = d.get('change_no', 'N/A')
        emoji = d.get('status_emoji', '')
        c = d.get("comments", "").strip()
        if emoji != "✅" and c and c.lower() not in ["none", "-", "n/a", ""]:
            comments_section += f"• **<{JIRA_SERVER}/browse/{key}|{key}>**: {c}\n"

    msg = f"*Hi All, Update - Planned Changes for Today ({today_date}) (ReleaseRadar)*\n\n"
    if summary:
        msg += f"🤖 *AI Summary:* {summary}\n\n"
    msg += f"```\n{table_content}\n```\n"
    msg += "*(Legend: ✅ Done | ⚙️ In Progress | ⏳ Pending | 🚨 Failed | 🚧 Backed Out/Rescheduled)*\n"

    if comments_section:
        msg += f"\n*⚡ Live Radar: Actionable Insights & Friction Points*\n{comments_section}"

    resp = requests.post(WEBHOOK_URL, headers={"Content-Type": "application/json"}, data=json.dumps({"text": msg}))
    if resp.status_code == 200:
        print("✅ Google Chat update broadcasted successfully!")
    else:
        print(f"❌ Chat post failed: {resp.status_code}, {resp.text}", file=sys.stderr)

def main():
    print(f"\n📢 [Chat Broadcast Bot] Starting 2-Hour Status Broadcast ({datetime.now(IST).strftime('%H:%M:%S IST')})...")
    deployments = fetch_active_deployments()
    if not deployments:
        print("No active deployments found in Sheet.")
        return
    card_data = run_gemini_analysis(deployments)
    if card_data:
        broadcast_to_gchat(card_data)

if __name__ == "__main__":
    main()
