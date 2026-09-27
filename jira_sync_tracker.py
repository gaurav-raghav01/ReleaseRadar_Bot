import os
import sys
import json
import subprocess
from datetime import datetime, timedelta, timezone
from jira import JIRA

import google.auth
from googleapiclient.discovery import build

# ==============================================================================
# CONFIGURATION
# ==============================================================================
JIRA_SERVER = os.getenv("JIRA_SERVER", "https://osttra.atlassian.net")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")

# Expanded JQL to capture all planned tickets for the current week & upcoming weekend
JQL_QUERY = os.getenv(
    "JQL_QUERY",
    """
    (worktype = Change OR issuetype = Change)
    AND assignee IN ("Gaurav Raghav", "Chandan Kumar", "Kamaldeep Singh", "Rahul Dev Jha")
    AND "Planned start" >= startOfWeek() AND "Planned start" <= endOfWeek("+2d")
    ORDER BY "Planned start" ASC
    """
)

DAILY_SHEET_ID = os.getenv("GOOGLE_SHEET_FILE_ID", "1NwJd8uzGIcDTxE0XJ_VYbqJfQnjuqxB4wh5UR97SIlk")
DAILY_TAB_NAME = os.getenv("GOOGLE_WORKSHEET_NAME", "Daily Changes")
DRIVE_FOLDER_ID = os.getenv("WEEKEND_DRIVE_FOLDER_ID", "122nLOsrDs6kV9t34gG6b6CXg4FxAwLhy")

STATE_CACHE_FILE = "sheet_last_status_cache.json"
IST = timezone(timedelta(hours=5, minutes=30))

KNOWN_APPS = [
    "IC SA", "TradeSTP", "DealHub", "LimitHub", "MW", "OTM", "TN", "Transformers",
    "STPaaS", "FXBA", "Traiana", "Adapter", "CCPC", "JSW", "H5/CL", "CRDS", "H3",
    "DNM", "SSO", "CSM", "CFD", "ODC", "EQ", "Cashflow", "LH", "CreditLink",
    "SocGen", "SCB", "ETD", "Baton", "AAS", "DMS", "SIGMA", "HSBC TRM", "IC LH UI",
    "Alpine", "CITI TRM", "Apigee", "Jigsaw", "DB TRM", "BOFA TRM", "CFS", "JPTRM",
    "NextGen Citi", "UBS", "ZKB", "BNPP", "3C", "WFM", "H4", "CLFX"
]

# ==============================================================================
# JIRA SERVICE
# ==============================================================================
class JiraClient:
    def __init__(self):
        if not JIRA_EMAIL or not JIRA_API_TOKEN:
            print("❌ JIRA_EMAIL or JIRA_API_TOKEN is missing!", file=sys.stderr)
            sys.exit(1)
        self.client = JIRA(server=JIRA_SERVER, basic_auth=(JIRA_EMAIL, JIRA_API_TOKEN))

    def fetch_changes(self):
        try:
            return self.client.search_issues(JQL_QUERY, maxResults=50, fields=['*all'])
        except Exception as e:
            print(f"❌ Jira query error: {e}")
            return []

    def get_affected_service(self, issue):
        raw = issue.raw.get('fields', {})
        for attr in ['services', 'affectedServices', 'components']:
            val = raw.get(attr)
            if val and isinstance(val, list):
                for item in val:
                    name = item.get('name') if isinstance(item, dict) else str(item)
                    for app in KNOWN_APPS:
                        if app.lower() == str(name).lower(): return app
                    return name

        for k, v in raw.items():
            if isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        name = item.get('name') or item.get('value')
                        for app in KNOWN_APPS:
                            if name and app.lower() == str(name).lower(): return app
            elif isinstance(v, dict):
                name = v.get('name') or v.get('value')
                for app in KNOWN_APPS:
                    if name and app.lower() == str(name).lower(): return app

        text = (issue.fields.summary or "") + " " + (issue.fields.description or "")
        for app in KNOWN_APPS:
            if f" {app.lower()} " in f" {text.lower()} " or app.lower() in text.lower():
                return app
        return ""

# ==============================================================================
# GOOGLE SHEETS & DRIVE RESOLVER
# ==============================================================================
class SheetManager:
    def __init__(self):
        scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        self.creds, _ = google.auth.default(scopes=scopes)
        self.sheets_api = build('sheets', 'v4', credentials=self.creds)
        self.drive_api = build('drive', 'v3', credentials=self.creds)

    def get_weekend_sheet_info(self, ref_date):
        """Finds or constructs the weekend filename for a given Saturday/Sunday date."""
        weekday = ref_date.weekday()
        sat = ref_date if weekday == 5 else (ref_date - timedelta(days=1) if weekday == 6 else ref_date + timedelta(days=(5 - weekday)))
        sun = sat + timedelta(days=1)
        filename = f"{sat.strftime('%d-%m-%Y')} & {sun.strftime('%d-%m-%Y')}"

        q = f"name contains '{filename}' and mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false"
        res = self.drive_api.files().list(q=q, fields="files(id, name)").execute()
        files = res.get('files', [])
        if files:
            return files[0]['id'], "Sheet1", sat.strftime("%A")
        return None, None, None

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
        meta = self.sheets_api.spreadsheets().get(spreadsheetId=sheet_id).execute()
        s_id_num = 0
        for s in meta.get('sheets', []):
            if s.get("properties", {}).get("title") == tab_name:
                s_id_num = s.get("properties", {}).get("sheetId")
                break

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

# ==============================================================================
# MAIN SYNC CYCLE
# ==============================================================================
def main():
    now_ist = datetime.now(IST)
    print(f"\n=======================================================")
    print(f"  JIRA SYNC TRACKER | {now_ist.strftime('%Y-%m-%d %H:%M:%S IST')}")
    print(f"=======================================================")

    jira = JiraClient()
    sheet_mgr = SheetManager()

    # 1. Fetch tickets from Jira
    jira_issues = jira.fetch_changes()
    print(f"🔍 Fetched {len(jira_issues)} change ticket(s) from Jira.")

    # 2. Read Daily Sheet & Weekend Sheet
    daily_values = sheet_mgr.read_sheet(DAILY_SHEET_ID, DAILY_TAB_NAME)
    weekend_sheet_id, weekend_tab, _ = sheet_mgr.get_weekend_sheet_info(now_ist)
    weekend_values = sheet_mgr.read_sheet(weekend_sheet_id, weekend_tab) if weekend_sheet_id else []

    # Map existing tickets: {ticket_key: {"sheet_type": "daily"|"weekend", "row": row_num, "status": current_status}}
    existing_tracker = {}

    # Parse Daily Sheet
    for i, row in enumerate(daily_values):
        if len(row) > 1 and row[1]:
            raw_k = str(row[1]).strip()
            key = raw_k.split('", "')[-1].replace('")', '').strip() if '", "' in raw_k else raw_k
            if key not in ["CR", "Change"]:
                stat = row[8].strip() if len(row) > 8 else ""
                existing_tracker[key] = {"type": "daily", "row": i + 1, "status": stat}

    # Parse Weekend Sheet
    weekend_sunday_row = None
    if weekend_values:
        for i, row in enumerate(weekend_values):
            if not row or not row[0]: continue
            c0 = str(row[0]).strip()
            if c0 == "Sunday": weekend_sunday_row = i + 1
            key = c0.split('", "')[-1].replace('")', '').strip() if '", "' in c0 else c0
            if key not in ["Change", "CR", "Saturday", "Sunday"]:
                stat = row[7].strip() if len(row) > 7 else ""
                existing_tracker[key] = {"type": "weekend", "row": i + 1, "status": stat}

    # 3. Process Each Jira Ticket: Route to Daily or Weekend Sheet
    for issue in jira_issues:
        key = issue.key
        summary = issue.fields.summary
        env = "PROD" if "prod" in summary.lower() else "UAT"
        app = jira.get_affected_service(issue)
        assignee = issue.fields.assignee.displayName if issue.fields.assignee else "TOC"
        jira_hyperlink = f'=HYPERLINK("{JIRA_SERVER}/browse/{key}", "{key}")'
        j_status = issue.fields.status.name

        # Parse planned start date
        p_start_str = getattr(issue.fields, "customfield_10015", None) or getattr(issue.fields, "plannedstart", None)
        try:
            planned_date = datetime.strptime(str(issue.fields.created)[:10], "%Y-%m-%d")
            # If issue has planned start field:
            for f_val in issue.raw.get('fields', {}).values():
                if isinstance(f_val, str) and len(f_val) >= 10 and f_val[:4].isdigit() and f_val[4] == '-':
                    try:
                        planned_date = datetime.strptime(f_val[:10], "%Y-%m-%d")
                        break
                    except Exception:
                        pass
        except Exception:
            planned_date = now_ist

        is_weekend_ticket = planned_date.weekday() >= 5
        ticket_day_name = planned_date.strftime("%A")

        # RULE: If ticket already exists in sheet, check for bi-directional completion sync
        if key in existing_tracker:
            info = existing_tracker[key]
            sheet_stat = info["status"].lower()
            # If Jira is completed/closed, sync it into the sheet if not already marked Done
            if j_status.lower() in ["completed", "closed", "resolved", "done"] and sheet_stat not in ["done", "completed"]:
                print(f"🔄 Syncing Jira completion into sheet for {key} (Row {info['row']})...")
                if info["type"] == "daily":
                    sheet_mgr.update_cell(DAILY_SHEET_ID, DAILY_TAB_NAME, f"I{info['row']}", "Done")
                elif weekend_sheet_id:
                    sheet_mgr.update_cell(weekend_sheet_id, weekend_tab, f"H{info['row']}", "Done")
                existing_tracker[key]["status"] = "Done"
            continue

        # RULE: Add new ticket to appropriate sheet
        if is_weekend_ticket and weekend_sheet_id:
            print(f"🗓️ Routing {key} to Weekend Sheet under {ticket_day_name}...")
            weekend_row = [
                jira_hyperlink, env, "Normal", summary, app,
                "TOC", "TOC", "Pending", "Pending", assignee, "10:00:00", ""
            ]
            if ticket_day_name == "Saturday" and weekend_sunday_row:
                sheet_mgr.insert_row_at(weekend_sheet_id, weekend_tab, weekend_row, weekend_sunday_row)
                weekend_sunday_row += 1
            else:
                sheet_mgr.append_rows(weekend_sheet_id, weekend_tab, [weekend_row])
            existing_tracker[key] = {"type": "weekend", "status": "Pending"}
        else:
            print(f"📅 Routing {key} to Daily Changes Sheet...")
            weekday_row = [
                now_ist.strftime("%d/%m/%Y"), jira_hyperlink, env, "Upload", app,
                summary, assignee, "10:00:00", j_status
            ]
            sheet_mgr.append_rows(DAILY_SHEET_ID, DAILY_TAB_NAME, [weekday_row])
            existing_tracker[key] = {"type": "daily", "status": j_status}

    # 4. Check for Status Changes (Parity & Change Detection)
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
            print(f"⚡ Status Transition Detected on {k}: '{prev_s}' -> '{curr_s}'")
            status_changed = True

    # Save cache
    with open(STATE_CACHE_FILE, "w") as f:
        json.dump({k: v["status"] for k, v in existing_tracker.items()}, f)

    # 5. Business Hours Protection Rule: 9:00 AM IST to 9:00 PM IST
    is_in_working_hours = (9 <= now_ist.hour < 21)

    if status_changed:
        if is_in_working_hours:
            print("🚀 Inside working hours (09:00-21:00 IST). Triggering Chat Broadcast Bot immediately...")
            subprocess.run(["python", "chat_broadcast_bot.py"], check=False)
        else:
            print(f"🌙 Status changed, but current time ({now_ist.strftime('%H:%M IST')}) is outside working window (09:00-21:00 IST). Notification suppressed.")
    else:
        print("✅ No critical status transitions detected. Chat broadcast deferred.")

if __name__ == "__main__":
    main()
