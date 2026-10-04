import os
import sys
import json
import csv
import re
from io import StringIO
from datetime import datetime, timedelta, timezone

import google.auth
from googleapiclient.discovery import build
import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig
import requests
from jira import JIRA

# ==============================================================================
# CONFIGURATION
# ==============================================================================
PROJECT_ID = os.getenv("GCP_PROJECT_ID", "prj-s-ai-bot-4rl8")
LOCATION = os.getenv("GCP_LOCATION", "us-central1")
IST = timezone(timedelta(hours=5, minutes=30))

WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
SHEET_FILE_ID = os.getenv("GOOGLE_SHEET_FILE_ID", "1qITm4Ny1Kf_JZoKsrCLpbDHlbmET49OaqN1aNsI7JHo")

JIRA_SERVER = os.getenv("JIRA_SERVER", "https://osttra.atlassian.net")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")

STATE_FILE = "last_run_state.txt"       # Tracks if wrap-up was sent
FIRST_RUN_FILE = "first_run_state.txt" # Tracks first morning broadcast

DONE_STATUSES = {"done", "completed", "closed", "resolved", "implemented", "success"}
IN_PROGRESS_STATUSES = {"in progress", "work in progress", "implementing", "started", "review"}

# ==============================================================================
# 1. JIRA DEEP CONTEXT SERVICE
# ==============================================================================
class JiraContextService:
    def __init__(self):
        self.client = None
        if JIRA_EMAIL and JIRA_API_TOKEN:
            try:
                self.client = JIRA(server=JIRA_SERVER, basic_auth=(JIRA_EMAIL, JIRA_API_TOKEN))
                print("✅ Connected to Jira REST API for deep context extraction.")
            except Exception as e:
                print(f"⚠️ Jira connection warning: {e}. Falling back to sheet-only context.")

    def fetch_ticket_deep_context(self, ticket_key):
        """Fetches raw ticket data, summary, status, and the latest comments."""
        if not self.client:
            return None
        try:
            issue = self.client.issue(ticket_key, fields=['summary', 'description', 'status', 'assignee', 'comment', 'priority'])
            comments = []
            if hasattr(issue.fields, 'comment') and issue.fields.comment:
                for c in (issue.fields.comment.comments or [])[-4:]:
                    author = getattr(c.author, 'displayName', 'Team')
                    body_clean = re.sub(r'[\r\n]+', ' ', c.body).strip()
                    comments.append(f"[{author}]: {body_clean[:220]}")

            return {
                "key": issue.key,
                "summary": issue.fields.summary,
                "jira_status": issue.fields.status.name,
                "jira_assignee": issue.fields.assignee.displayName if issue.fields.assignee else "TOC",
                "comments": comments,
                "description": (issue.fields.description or "")[:350]
            }
        except Exception as e:
            print(f"⚠️ Could not fetch Jira context for {ticket_key}: {e}")
            return None

# ==============================================================================
# 2. SHEET EXTRACTION ENGINE (SHEET IS SOURCE OF TRUTH)
# ==============================================================================
def extract_ticket_key(raw_cell):
    """Extracts raw key like 'TRMPLATFORM-10064' from plain text or =HYPERLINK formulas."""
    cell = str(raw_cell).strip()
    if '", "' in cell:
        return cell.split('", "')[-1].replace('")', '').strip()
    elif '",' in cell:
        return cell.split('",')[-1].replace(')', '').replace('"', '').strip()
    return cell

def get_today_deployments_from_sheet():
    print("🔍 Authenticating via ADC and querying active deployment sheet...")
    scopes = [
        'https://www.googleapis.com/auth/drive.readonly',
        'https://www.googleapis.com/auth/cloud-platform'
    ]
    credentials, _ = google.auth.default(scopes=scopes)

    try:
        drive_service = build('drive', 'v3', credentials=credentials)
    except Exception as e:
        print(f"❌ CRITICAL ERROR: Failed to connect to Drive API: {e}", file=sys.stderr)
        sys.exit(1)

    now_ist = datetime.now(IST)
    is_weekend = now_ist.weekday() >= 5
    target_file_id = SHEET_FILE_ID

    if is_weekend:
        if now_ist.weekday() == 5:
            sat_date = now_ist
            sun_date = now_ist + timedelta(days=1)
        else:
            sat_date = now_ist - timedelta(days=1)
            sun_date = now_ist

        weekend_filename = f"{sat_date.strftime('%d-%m-%Y')} & {sun_date.strftime('%d-%m-%Y')}"
        print(f"🗓️ Weekend active! Resolving sheet: '{weekend_filename}'")
        q = f"name contains '{weekend_filename}' and mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false"
        res = drive_service.files().list(q=q, fields="files(id, name)").execute()
        files = res.get('files', [])
        if not files:
            print(f"⚠️ Weekend tracker file '{weekend_filename}' not found. Exiting.")
            return []
        target_file_id = files[0]['id']
        print(f"✅ Found weekend tracker: {files[0]['name']}")
    else:
        print(f"📅 Daily operations active. Reading daily changes sheet...")

    try:
        resp = drive_service.files().export_media(fileId=target_file_id, mimeType='text/csv').execute()
        reader = csv.reader(StringIO(resp.decode('utf-8')))
        values = list(reader)
    except Exception as e:
        print(f"❌ Failed to download sheet CSV: {e}", file=sys.stderr)
        return []

    if not values:
        return []

    parsed_deployments = []

    if is_weekend:
        today_day_name = now_ist.strftime("%A")
        current_block = None

        for row in values:
            if not row or not any(row): continue
            clean = [str(c).strip() for c in row if str(c).strip()]
            if not clean: continue

            if clean[0] in ["Saturday", "Sunday"]:
                current_block = clean[0]
                continue

            if current_block == today_day_name and len(row) > 7:
                k = extract_ticket_key(row[0])
                if k and k not in ["Change", "CR", "Saturday", "Sunday"]:
                    parsed_deployments.append({
                        "key": k,
                        "env": row[1].strip() if len(row) > 1 else "PROD",
                        "type": row[2].strip() if len(row) > 2 else "Normal",
                        "desc": row[3].strip() if len(row) > 3 else "",
                        "app": row[4].strip() if len(row) > 4 else "",
                        "owner": row[9].strip() if len(row) > 9 and row[9] else (row[5].strip() if len(row) > 5 else "TOC"),
                        "sheet_status": row[7].strip() if len(row) > 7 else "Pending",
                        "sheet_comments": row[11].strip() if len(row) > 11 else ""
                    })
    else:
        possible_dates = [
            now_ist.strftime("%d/%m/%Y"), now_ist.strftime("%d/%m/%y"),
            now_ist.strftime("%Y-%m-%d"), f"{now_ist.day}/{now_ist.month}/{now_ist.year}"
        ]
        today_active = False

        for row in values:
            if not row or not any(row): continue
            c0 = str(row[0]).strip()
            if any(d in c0 for d in possible_dates):
                today_active = True
                continue
            elif today_active and ('/' in c0 or '-' in c0) and not any(d in c0 for d in possible_dates):
                break

            if today_active and len(row) > 1 and row[1]:
                k = extract_ticket_key(row[1])
                if k and k not in ["CR", "Change"]:
                    parsed_deployments.append({
                        "key": k,
                        "env": row[2].strip() if len(row) > 2 else "PROD",
                        "type": row[3].strip() if len(row) > 3 else "Normal",
                        "app": row[4].strip() if len(row) > 4 else "",
                        "desc": row[5].strip() if len(row) > 5 else "",
                        "owner": row[6].strip() if len(row) > 6 else "TOC",
                        "sheet_status": row[8].strip() if len(row) > 8 else "Pending",
                        "sheet_comments": ""
                    })

    print(f"📊 Extracted {len(parsed_deployments)} planned deployment(s) for today from active sheet.")
    return parsed_deployments

# ==============================================================================
# 3. STATUS PARITY RESOLUTION
# ==============================================================================
def resolve_effective_status(sheet_status, jira_status):
    s_stat = (sheet_status or "").strip().lower()
    j_stat = (jira_status or "").strip().lower()

    if s_stat in DONE_STATUSES or j_stat in DONE_STATUSES:
        return "Done", "✅"
    elif s_stat in IN_PROGRESS_STATUSES or j_stat in IN_PROGRESS_STATUSES:
        return "In Progress", "⚙️"
    elif "fail" in s_stat or "fail" in j_stat:
        return "Failed", "🚨"
    elif any(w in s_stat or w in j_stat for w in ["back", "reschedule", "cancel"]):
        return "Rescheduled", "🚧"
    else:
        return "Pending", "⏳"

# ==============================================================================
# 4. VERTEX AI (GEMINI 2.5 FLASH) INTELLIGENT AUTO-RADAR
# ==============================================================================
def analyze_with_gemini_2_5(enriched_deployments):
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    model = GenerativeModel("gemini-2.5-flash")

    prompt = f"""
    You are the Senior TOC Deployment Lead at Osttra.
    Analyze today's deployment schedule and current operational health.
    
    Data:
    {json.dumps(enriched_deployments, indent=2)}

    TASKS:
    1. Write a 1-sentence "executive_summary" summarizing today's deployment progress.
    2. For each deployment:
       - Keep "change_no" exact.
       - Create a concise "short_desc" (MAX 3 to 5 words, strictly under 20 chars).
       - Maintain the given "effective_status" and "effective_emoji".
       - Determine clean "application" and "environment".
    
    3. Generate "live_radar_items" (STRICT AUTO MODE):
       - Act as an intelligent engineer analyzing the deployment's status, description, and recent comments.
       - ONLY include tickets that warrant real attention or have meaningful operational context:
         * Tasks currently In Progress, Pending, Failed, or Rescheduled.
         * Tasks with noteworthy blockers, technical nuances, or client handoffs discussed in the comments (e.g. SIFMA DR reschedule, FpML mapping issue, client testing acknowledgement).
       - DO NOT include tickets that completed routinely without any issues or special notes.
       - For selected tickets, provide a sharp, professional 1-sentence summary of the latest progress or status.

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
                "status_emoji": "..."
            }}
        ],
        "live_radar": [
            {{
                "change_no": "...",
                "app": "...",
                "insight": "..."
            }}
        ]
    }}
    """

    try:
        resp = model.generate_content(
            prompt,
            generation_config=GenerationConfig(
                response_mime_type="application/json",
                temperature=0.1
            )
        )
        return json.loads(resp.text.strip())
    except Exception as e:
        print(f"❌ Gemini analysis error: {e}", file=sys.stderr)
        return None

# ==============================================================================
# 5. GOOGLE CHAT BROADCAST WITH HYPERLINKS & CLEAN LEGEND
# ==============================================================================
def broadcast_to_gchat(card_data, is_first_update=True, is_wrap_up=False):
    if not card_data or not WEBHOOK_URL:
        print("⚠️ Broadcast skipped: No data or WEBHOOK_URL not configured.")
        return False

    summary = card_data.get("executive_summary", "")
    deployments = card_data.get("deployments", [])
    radar_items = card_data.get("live_radar", [])

    # Build ASCII Table (allowing up to 17 characters for Change No)
    headers = ["Change No", "App", "Desc", "Env", "St's", "Owner"]
    rows = []
    for dep in deployments:
        c_no = str(dep.get('change_no', 'N/A'))[:17]  # Extended to fit full TRMPLATFORM-XXXXX
        app = str(dep.get('application', 'N/A'))[:8]
        desc = str(dep.get('short_desc', 'N/A'))[:20]
        env = str(dep.get('environment', 'N/A'))[:6]
        emoji = dep.get('status_emoji', '⏳')
        owner = str(dep.get('implementer', 'N/A')).split(' ')[0][:10]
        rows.append([c_no, app, desc, env, emoji, owner])

    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            disp_len = len(cell) - 1 if any(e in cell for e in ['✅','⚙️','⏳','🚨','🚧']) else len(cell)
            col_widths[i] = max(col_widths[i], disp_len)

    header_line = "| " + " | ".join(h.ljust(w) for h, w in zip(headers, col_widths)) + " |"
    separator_line = "|" + "|".join("-" * (w + 2) for w in col_widths) + "|"
    table_lines = [header_line, separator_line]

    for row in rows:
        r_str = "|"
        for i, cell in enumerate(row):
            if i == 4:
                r_str += f" " + (" " * (col_widths[i] - 1)) + f"{cell}|"
            else:
                r_str += f" {cell.ljust(col_widths[i])} |"
        table_lines.append(r_str)

    table_content = "\n".join(table_lines)
    today_date = datetime.now(IST).strftime('%d/%m/%Y')

    # Header title
    if is_wrap_up:
        msg = f"*🎉 Wrap-up Update - Planned Changes for Today ({today_date}) (ReleaseRadar)*\n\n"
    elif is_first_update:
        msg = f"*Hi All, Planned Changes for Today ({today_date}) (ReleaseRadar)*\n\n"
        if summary:
            msg += f"🤖 *AI Summary:* {summary}\n\n"
    else:
        msg = f"*Hi All, Update - Planned Changes for Today ({today_date}) (ReleaseRadar)*\n\n"
        if summary:
            msg += f"🤖 *AI Summary:* {summary}\n\n"

    msg += f"```\n{table_content}\n```\n"
    msg += "*(Legend: ✅ Done | ⚙️ In Progress | ⏳ Pending | 🚨 Failed | 🚧 Rescheduled)*\n"

    # Clickable Live Radar Feed (Auto Mode)
    if radar_items:
        msg += "\n*⚡ Live Radar: Actionable Insights & Progress*\n"
        for item in radar_items:
            k = item.get("change_no")
            app_str = f" ({item.get('app')})" if item.get('app') else ""
            insight = item.get("insight")
            msg += f"• *<{JIRA_SERVER}/browse/{k}|{k}>*{app_str}: {insight}\n"

    resp = requests.post(WEBHOOK_URL, headers={"Content-Type": "application/json"}, data=json.dumps({"text": msg}))
    if resp.status_code == 200:
        print("✅ Live Radar table broadcasted to Google Chat successfully!")
        return True
    else:
        print(f"❌ Chat post failed: {resp.status_code}, {resp.text}")
        return False

# ==============================================================================
# 6. MAIN ENGINE
# ==============================================================================
def main():
    now_ist = datetime.now(IST)
    today_str = now_ist.strftime('%Y-%m-%d')
    print(f"\n=======================================================")
    print(f"  RELEASERADAR LIVE FEED BOT | {now_ist.strftime('%Y-%m-%d %H:%M:%S IST')}")
    print(f"=======================================================")

    # 1. Extract today's tickets from Sheet (Source of Truth)
    sheet_tickets = get_today_deployments_from_sheet()
    if not sheet_tickets:
        print("No deployments found for today in the active sheet. Exiting.")
        return

    # 2. Enrich with Jira deep context
    jira_service = JiraContextService()
    enriched = []
    all_done = True

    for t in sheet_tickets:
        key = t["key"]
        jira_ctx = jira_service.fetch_ticket_deep_context(key)
        j_status = jira_ctx["jira_status"] if jira_ctx else ""
        
        eff_status, eff_emoji = resolve_effective_status(t["sheet_status"], j_status)
        if eff_status != "Done":
            all_done = False

        enriched.append({
            "key": key,
            "sheet_app": t["app"],
            "sheet_env": t["env"],
            "sheet_type": t["type"],
            "sheet_desc": t["desc"],
            "sheet_owner": t["owner"],
            "sheet_status": t["sheet_status"],
            "effective_status": eff_status,
            "effective_emoji": eff_emoji,
            "jira_summary": jira_ctx["summary"] if jira_ctx else t["desc"],
            "jira_comments": jira_ctx["comments"] if jira_ctx else [t.get("sheet_comments", "")],
            "jira_description": jira_ctx["description"] if jira_ctx else ""
        })

    # 3. Handle First-run & Wrap-up State
    for fname in [FIRST_RUN_FILE, STATE_FILE]:
        if not os.path.exists(fname): open(fname, 'w').close()

    is_first_run = True
    with open(FIRST_RUN_FILE, "r") as f:
        if f.read().strip() == today_str:
            is_first_run = False

    is_deadline = now_ist.hour >= 21
    is_wrap_up = all_done or is_deadline

    if is_wrap_up:
        with open(STATE_FILE, "r") as f:
            if f.read().strip() == today_str:
                print("✅ Wrap-up update was already completed today. Exiting.")
                return

    # 4. Generate AI Summary & Live Radar
    analysis = analyze_with_gemini_2_5(enriched)
    if not analysis:
        return

    # 5. Send Broadcast
    success = broadcast_to_gchat(analysis, is_first_update=is_first_run, is_wrap_up=is_wrap_up)

    if success:
        if is_first_run:
            with open(FIRST_RUN_FILE, "w") as f: f.write(today_str)
        if is_wrap_up:
            with open(STATE_FILE, "w") as f: f.write(today_str)
        else:
            with open(STATE_FILE, "w") as f: f.write("")

if __name__ == "__main__":
    main()
