import os
import sys
import json
import subprocess
from datetime import datetime, timedelta, timezone
from jira import JIRA

import google.auth
from googleapiclient.discovery import build
import vertexai
from vertexai.generative_models import GenerativeModel

# ==============================================================================
# CONFIGURATION
# ==============================================================================
JIRA_SERVER = os.getenv("JIRA_SERVER", "https://osttra.atlassian.net")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")

# Strict JQL: Pulls tickets planned for today onwards or active this week, excluding past closed tickets
JQL_QUERY = os.getenv(
    "JQL_QUERY",
    """
    (worktype = Change OR issuetype = Change)
    AND assignee IN ("Gaurav Raghav", "Chandan Kumar", "Kamaldeep Singh", "Rahul Dev Jha")
    AND (
        ("Planned start" >= startOfDay("-1d") AND "Planned start" <= endOfWeek("+2d"))
        OR (status IN ("In Progress", "Work in progress", "Implementing") AND updated >= startOfDay("-1d"))
    )
    ORDER BY "Planned start" ASC
    """
)

GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "prj-s-ai-bot-4rl8")
GCP_LOCATION = os.getenv("GCP_LOCATION", "us-central1")

DAILY_SHEET_ID = os.getenv("GOOGLE_SHEET_FILE_ID", "1NwJd8uzGIcDTxE0XJ_VYbqJfQnjuqxB4wh5UR97SIlk")
DAILY_TAB_NAME = os.getenv("GOOGLE_WORKSHEET_NAME", "Daily Changes")
DRIVE_FOLDER_ID = os.getenv("WEEKEND_DRIVE_FOLDER_ID", "122nLOsrDs6kV9t34gG6b6CXg4FxAwLhy")

STATE_CACHE_FILE = "sheet_last_status_cache.json"
IST = timezone(timedelta(hours=5, minutes=30))

# ==============================================================================
# 1. JIRA SERVICE
# ==============================================================================
class JiraClient:
    def __init__(self):
        if not JIRA_EMAIL or not JIRA_API_TOKEN:
            print("❌ JIRA_EMAIL or JIRA_API_TOKEN missing!", file=sys.stderr)
            sys.exit(1)
        self.client = JIRA(server=JIRA_SERVER, basic_auth=(JIRA_EMAIL, JIRA_API_TOKEN))

    def fetch_relevant_changes(self):
        try:
            print("🔍 Fetching relevant change tickets from Jira...")
            return self.client.search_issues(JQL_QUERY, maxResults=50, fields=['*all'])
        except Exception as e:
            print(f"❌ Jira query error: {e}", file=sys.stderr)
            return []

# ==============================================================================
# 2. VERTEX AI (GEMINI 2.5 FLASH) SMART ROUTER & FORMATTER
# ==============================================================================
class SmartAIRouter:
    def __init__(self, project_id, location):
        vertexai.init(project=project_id, location=location)
        self.model = GenerativeModel("gemini-2.5-flash")
        print("✅ Vertex AI (Gemini 2.5 Flash) Smart Decision Engine initialized.")

    def process_and_route_tickets(self, jira_issues, today_date_str):
        """
        Gemini analyzes raw tickets and produces:
        - Strict filtering of old/irrelevant tickets
        - Correct routing: 'daily' vs 'weekend'
        - Specific date and day section ('Saturday' / 'Sunday')
        - Application name matching Sheet dropdowns
        - Clickable hyperlink formula
        """
        serialized = []
        for issue in jira_issues:
            raw = issue.raw.get('fields', {})
            serialized.append({
                "key": issue.key,
                "summary": issue.fields.summary,
                "status": issue.fields.status.name,
                "assignee": issue.fields.assignee.displayName if issue.fields.assignee else "TOC",
                "planned_start": raw.get("customfield_10015") or raw.get("plannedstart") or str(getattr(issue.fields, "created", "")),
                "affected_services": raw.get("services") or raw.get("affectedServices") or raw.get("components") or "",
                "description": (issue.fields.description or "")[:300]
            })

        prompt = f"""
        You are an intelligent Release Operations AI for Osttra.
        Analyze these deployment change tickets from Jira.
        Current Reference Date (IST): {today_date_str}

        Task:
        1. FILTER OUT any old tickets that were already completed/closed on previous days unless actively being modified today.
        2. Determine the exact scheduled deployment date for each ticket in DD/MM/YYYY format.
        3. CLASSIFY target destination:
           - "daily": For Monday to Friday deployments.
           - "weekend": For Saturday or Sunday deployments.
        4. If "weekend", specify "weekend_section": "Saturday" or "Sunday".
        5. Map "application" to the cleanest short name (e.g. "TradeSTP", "IC SA", "DealHub", "LimitHub", "MW", "OTM", "Harmony", "SocGen", "Cashflow", "AAS").
        6. Determine "env": "PROD", "UAT", or "PROD DR".
        7. Format "cr_link": '=HYPERLINK("https://osttra.atlassian.net/browse/' + key + '", "' + key + '")'

        Raw Data:
        {json.dumps(serialized, indent=2)}

        OUTPUT STRICT JSON ONLY (No markdown):
        {{
            "tickets": [
                {{
                    "key": "...",
                    "target": "daily" or "weekend",
                    "date_str": "DD/MM/YYYY",
                    "weekend_section": "Saturday" or "Sunday" or null,
                    "env": "...",
                    "type": "Upload" or "Normal" or "SC",
                    "application": "...",
                    "description": "...",
                    "implementor": "...",
                    "start_time": "10:00:00",
                    "status": "..."
                }}
            ]
        }}
        """
        try:
            resp = self.model.generate_content(prompt)
            clean = resp.text.strip().replace("```json", "").replace("```", "")
            data = json.loads(clean)
            return data.get("tickets", [])
        except Exception as e:
            print(f"❌ Smart Router AI Error: {e}", file=sys.stderr)
            return []

# ==============================================================================
# 3. GOOGLE SHEETS & DRIVE ENGINE (PERFECT FORMATTING)
# ==============================================================================
class GoogleSheetEngine:
    def __init__(self):
        scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        self.creds, _ = google.auth.default(scopes=scopes)
        self.sheets_api = build('sheets', 'v4', credentials=self.creds)
        self.drive_api = build('drive', 'v3', credentials=self.creds)

    def get_sheet_id_num(self, spreadsheet_id, tab_name):
        meta = self.sheets_api.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        for s in meta.get('sheets', []):
            if s.get("properties", {}).get("title") == tab_name:
                return s.get("properties", {}).get("sheetId")
        return 0

    def resolve_weekend_sheet_id(self, ref_date):
        """Finds the 'DD-MM-YYYY & DD-MM-YYYY' spreadsheet in Drive."""
        weekday = ref_date.weekday()
        sat = ref_date if weekday == 5 else (ref_date - timedelta(days=1) if weekday == 6 else ref_date + timedelta(days=(5 - weekday)))
        sun = sat + timedelta(days=1)
        filename = f"{sat.strftime('%d-%m-%Y')} & {sun.strftime('%d-%m-%Y')}"

        q = f"name contains '{filename}' and mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false"
        res = self.drive_api.files().list(q=q, fields="files(id, name)").execute()
        files = res.get('files', [])
        if files:
            return files[0]['id'], files[0]['name']
        return None, None

    def read_sheet(self, sheet_id, tab_name):
        try:
            res = self.sheets_api.spreadsheets().values().get(
                spreadsheetId=sheet_id, range=f"'{tab_name}'!A:L"
            ).execute()
            return res.get('values', [])
        except Exception:
            return []

    def update_cell(self, sheet_id, tab_name, cell, value):
        self.sheets_api.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"'{tab_name}'!{cell}",
            valueInputOption='USER_ENTERED',
            body={'values': [[value]]}
        ).execute()

    def append_rows(self, sheet_id, tab_name, rows):
        self.sheets_api.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"'{tab_name}'!A:A",
            valueInputOption='USER_ENTERED',
            insertDataOption='INSERT_ROWS',
            body={'values': rows}
        ).execute()

    def insert_row_at(self, sheet_id, tab_name, row_data, row_idx_1_based):
        s_id_num = self.get_sheet_id_num(sheet_id, tab_name)
        body = {
            "requests": [
                {
                    "insertDimension": {
                        "range": {
                            "sheetId": s_id_num,
                            "dimension": "ROWS",
                            "startIndex": row_idx_1_based - 1,
                            "endIndex": row_idx_1_based
                        },
                        "inheritFromBefore": False
                    }
                }
            ]
        }
        self.sheets_api.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body=body).execute()
        self.sheets_api.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"'{tab_name}'!A{row_idx_1_based}",
            valueInputOption='USER_ENTERED',
            body={'values': [row_data]}
        ).execute()

    def create_clean_daily_block(self, sheet_id, tab_name, date_str, rows_to_add, start_row_1_based):
        """Creates a professional date banner + navy header with borders and no dropdowns."""
        s_id_num = self.get_sheet_id_num(sheet_id, tab_name)
        block_rows = [
            [], # Blank spacer
            [date_str] + [""] * 8, # Banner
            ['Date', 'CR', 'Env', 'Type', 'Application', 'Description', 'Implementor', 'Start Time', 'Status']
        ] + rows_to_add

        self.append_rows(sheet_id, tab_name, block_rows)

        banner_row_1 = start_row_1_based + 2
        header_row_1 = start_row_1_based + 3
        data_start_row_1 = start_row_1_based + 4
        data_end_row_1 = data_start_row_1 + len(rows_to_add) - 1

        requests = [
            # 1. Merge Banner
            {
                "mergeCells": {
                    "range": {
                        "sheetId": s_id_num,
                        "startRowIndex": banner_row_1 - 1,
                        "endRowIndex": banner_row_1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 9
                    },
                    "mergeType": "MERGE_ALL"
                }
            },
            # 2. Style Banner (Light Blue fill, Bold text)
            {
                "repeatCell": {
                    "range": {
                        "sheetId": s_id_num,
                        "startRowIndex": banner_row_1 - 1,
                        "endRowIndex": banner_row_1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 9
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": {"red": 0.44, "green": 0.71, "blue": 0.98},
                            "textFormat": {"bold": True, "foregroundColor": {"red": 0.0, "green": 0.0, "blue": 0.0}, "fontSize": 10},
                            "horizontalAlignment": "LEFT",
                            "verticalAlignment": "MIDDLE"
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
                }
            },
            # 3. Clear dropdowns from banner
            {
                "setDataValidation": {
                    "range": {
                        "sheetId": s_id_num,
                        "startRowIndex": banner_row_1 - 1,
                        "endRowIndex": banner_row_1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 9
                    },
                    "rule": None
                }
            },
            # 4. Style Column Header Row (Dark Navy #0b5394, Bold White text)
            {
                "repeatCell": {
                    "range": {
                        "sheetId": s_id_num,
                        "startRowIndex": header_row_1 - 1,
                        "endRowIndex": header_row_1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 9
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": {"red": 0.04, "green": 0.33, "blue": 0.58},
                            "textFormat": {"bold": True, "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}, "fontSize": 9},
                            "horizontalAlignment": "CENTER",
                            "verticalAlignment": "MIDDLE"
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
                }
            },
            # 5. Clear dropdowns from header row
            {
                "setDataValidation": {
                    "range": {
                        "sheetId": s_id_num,
                        "startRowIndex": header_row_1 - 1,
                        "endRowIndex": header_row_1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 9
                    },
                    "rule": None
                }
            }
        ]
        self.sheets_api.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": requests}).execute()
        self.apply_grid_borders(sheet_id, s_id_num, data_start_row_1, data_end_row_1)

    def apply_grid_borders(self, sheet_id, s_id_num, start_row_1, end_row_1):
        border_spec = {"style": "SOLID", "width": 1, "color": {"red": 0.85, "green": 0.85, "blue": 0.85}}
        body = {
            "requests": [
                {
                    "updateBorders": {
                        "range": {
                            "sheetId": s_id_num,
                            "startRowIndex": start_row_1 - 1,
                            "endRowIndex": end_row_1,
                            "startColumnIndex": 0,
                            "endColumnIndex": 9
                        },
                        "top": border_spec, "bottom": border_spec,
                        "left": border_spec, "right": border_spec,
                        "innerHorizontal": border_spec, "innerVertical": border_spec
                    }
                }
            ]
        }
        self.sheets_api.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body=body).execute()

# ==============================================================================
# 4. MAIN ORCHESTRATION CYCLE
# ==============================================================================
def main():
    now_ist = datetime.now(IST)
    today_date_str = now_ist.strftime("%d/%m/%Y")
    print(f"\n=======================================================")
    print(f"  AI-POWERED JIRA SYNC TRACKER | {now_ist.strftime('%Y-%m-%d %H:%M:%S IST')}")
    print(f"=======================================================")

    jira = JiraClient()
    ai_router = SmartAIRouter(GCP_PROJECT_ID, GCP_LOCATION)
    engine = GoogleSheetEngine()

    # 1. Fetch raw candidates from Jira
    jira_issues = jira.fetch_relevant_changes()
    if not jira_issues:
        print("No active change candidates found in Jira. Exiting.")
        return

    # 2. Let Gemini evaluate, filter out old/closed noise, and route
    smart_tickets = ai_router.process_and_route_tickets(jira_issues, today_date_str)
    print(f"🧠 Gemini validated and routed {len(smart_tickets)} active deployment ticket(s).")

    # 3. Read both sheets to inspect existing tickets and protect manual edits
    daily_vals = engine.read_sheet(DAILY_SHEET_ID, DAILY_TAB_NAME)
    weekend_sheet_id, weekend_name = engine.resolve_weekend_sheet_id(now_ist)
    weekend_vals = engine.read_sheet(weekend_sheet_id, "Sheet1") if weekend_sheet_id else []

    existing_tracker = {}

    # Parse Daily
    daily_block_start = {}  # {date_str: row_idx}
    for i, row in enumerate(daily_vals):
        if not row or not row[0]: continue
        c0 = str(row[0]).strip()
        if '/' in c0 or '-' in c0:
            daily_block_start[c0] = i + 1
        if len(row) > 1 and row[1]:
            raw_k = str(row[1]).strip()
            k = raw_k.split('", "')[-1].replace('")', '').strip() if '", "' in raw_k else raw_k
            if k not in ["CR", "Change"]:
                existing_tracker[k] = {"type": "daily", "row": i + 1, "status": row[8].strip() if len(row) > 8 else ""}

    # Parse Weekend
    weekend_sunday_row = None
    if weekend_vals:
        for i, row in enumerate(weekend_vals):
            if not row or not row[0]: continue
            c0 = str(row[0]).strip()
            if c0 == "Sunday": weekend_sunday_row = i + 1
            k = c0.split('", "')[-1].replace('")', '').strip() if '", "' in c0 else c0
            if k not in ["Change", "CR", "Saturday", "Sunday"]:
                existing_tracker[k] = {"type": "weekend", "row": i + 1, "status": row[7].strip() if len(row) > 7 else ""}

    # 4. Ingest Smart Tickets into Their Respective Sheets
    for item in smart_tickets:
        k = item["key"]
        j_stat = item.get("status", "Pending")
        target = item.get("target", "daily")
        date_str = item.get("date_str", today_date_str)
        cr_hyperlink = f'=HYPERLINK("{JIRA_SERVER}/browse/{k}", "{k}")'

        # Rule: Ticket already exists -> Check for Jira completion sync, but never overwrite manual Done
        if k in existing_tracker:
            info = existing_tracker[k]
            s_stat = str(info["status"]).strip().lower()
            if j_stat.lower() in ["completed", "closed", "resolved", "done"] and s_stat not in ["done", "completed"]:
                print(f"🔄 Bi-directional Sync: Updating sheet status for {k} to 'Done'...")
                if info["type"] == "daily":
                    engine.update_cell(DAILY_SHEET_ID, DAILY_TAB_NAME, f"I{info['row']}", "Done")
                elif weekend_sheet_id:
                    engine.update_cell(weekend_sheet_id, "Sheet1", f"H{info['row']}", "Done")
                existing_tracker[k]["status"] = "Done"
            continue

        # Ingest New Ticket: Weekend vs Daily
        if target == "weekend" and weekend_sheet_id:
            w_section = item.get("weekend_section", "Saturday")
            print(f"🗓️ Ingesting {k} into Weekend Sheet under {w_section}...")
            weekend_row = [
                cr_hyperlink, item.get("env", "PROD"), item.get("type", "Normal"),
                item.get("description", ""), item.get("application", ""),
                "TOC", "TOC", "Pending", "Pending", item.get("implementor", "TOC"),
                item.get("start_time", "10:00:00"), ""
            ]
            if w_section == "Saturday" and weekend_sunday_row:
                engine.insert_row_at(weekend_sheet_id, "Sheet1", weekend_row, weekend_sunday_row)
                weekend_sunday_row += 1
            else:
                engine.append_rows(weekend_sheet_id, "Sheet1", [weekend_row])
            existing_tracker[k] = {"type": "weekend", "status": "Pending"}

        else:
            print(f"📅 Ingesting {k} into Daily Changes Sheet ({date_str})...")
            daily_row = [
                date_str, cr_hyperlink, item.get("env", "PROD"),
                item.get("type", "Upload"), item.get("application", ""),
                item.get("description", ""), item.get("implementor", "TOC"),
                item.get("start_time", "10:00:00"), j_stat
            ]
            # If date block exists, append to bottom of sheet or block
            if date_str in daily_block_start:
                engine.append_rows(DAILY_SHEET_ID, DAILY_TAB_NAME, [daily_row])
            else:
                print(f"Creating brand-new styled date block for {date_str}...")
                engine.create_clean_daily_block(DAILY_SHEET_ID, DAILY_TAB_NAME, date_str, [daily_row], len(daily_vals))
                daily_block_start[date_str] = len(daily_vals) + 2

            existing_tracker[k] = {"type": "daily", "status": j_stat}

    # 5. Status Transition Tracking & Working Hours Gate
    prev_cache = {}
    if os.path.exists(STATE_CACHE_FILE):
        try:
            with open(STATE_CACHE_FILE, "r") as f: prev_cache = json.load(f)
        except Exception: prev_cache = {}

    status_changed = False
    for k, v in existing_tracker.items():
        curr_s = v["status"].strip().lower()
        prev_s = prev_cache.get(k, "").strip().lower()
        if curr_s and prev_s and curr_s != prev_s:
            print(f"⚡ Status Transition: {k} went from '{prev_s}' -> '{curr_s}'")
            status_changed = True

    with open(STATE_CACHE_FILE, "w") as f:
        json.dump({k: v["status"] for k, v in existing_tracker.items()}, f)

    # Working Window Protection: 9:00 AM to 9:00 PM IST
    is_working_hours = (9 <= now_ist.hour < 21)

    if status_changed:
        if is_working_hours:
            print("🚀 Working hours active (09:00 - 21:00 IST). Triggering Chat broadcast immediately...")
            subprocess.run(["python", "chat_broadcast_bot.py"], check=False)
        else:
            print(f"🌙 Outside working window ({now_ist.strftime('%H:%M IST')}). Chat broadcast suppressed.")
    else:
        print("✅ No status transitions. Broadcast deferred to regular 2-hour schedule.")

if __name__ == "__main__":
    main()
