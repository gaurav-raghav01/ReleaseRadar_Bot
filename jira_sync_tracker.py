import os
import sys
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from jira import JIRA
import requests
from requests.auth import HTTPBasicAuth

import google.auth
from googleapiclient.discovery import build
import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig

# ==============================================================================
# CONFIGURATION
# ==============================================================================
JIRA_SERVER = os.getenv("JIRA_SERVER", "https://osttra.atlassian.net")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")

# Strict JQL bounded from today to next 10 days
JQL_QUERY = os.getenv(
    "JQL_QUERY",
    """
    (worktype = Change OR issuetype = Change)
    AND (
        assignee IN ("Gaurav Raghav", "Chandan Kumar", "Kamaldeep Singh", "Rahul Dev Jha", "Parthiv Upreti")
        OR project IN (TRMPLATFORM, LIMITHUB, HAR, HARFOUR, HARTHREE, CFS, INTU, ETD, EQ, CONNDEV, CCPC, MSERV, DAAS, AAS, DMGS)
    )
    AND status NOT IN (Completed, Canceled, Done, "Backed out", Declined, Rejected, Closed)
    AND (
        ("Planned start" >= startOfDay() AND "Planned start" <= startOfDay("+10d"))
        OR (status IN ("In Progress", "Work in progress", "Implementing") AND updated >= startOfDay("-1d"))
    )
    ORDER BY "Planned start" ASC
    """
)

GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "prj-s-ai-bot-4rl8")
GCP_LOCATION = os.getenv("GCP_LOCATION", "us-central1")

DAILY_SHEET_ID = os.getenv("GOOGLE_SHEET_FILE_ID", "1NwJd8uzGIcDTxE0XJ_VYbqJfQnjuqxB4wh5UR97SIlk")
DAILY_TAB_NAME = os.getenv("GOOGLE_WORKSHEET_NAME", "Daily Changes")

STATE_CACHE_FILE = "sheet_last_status_cache.json"
IST = timezone(timedelta(hours=5, minutes=30))

# ==============================================================================
# 1. JIRA SERVICE (WITH ROBUST REST API FIELD DISCOVERY)
# ==============================================================================
class JiraClient:
    def __init__(self):
        if not JIRA_EMAIL or not JIRA_API_TOKEN:
            print("❌ JIRA_EMAIL or JIRA_API_TOKEN is missing!", file=sys.stderr)
            sys.exit(1)
        self.auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_API_TOKEN)
        self.client = JIRA(server=JIRA_SERVER, basic_auth=(JIRA_EMAIL, JIRA_API_TOKEN))
        self.planned_start_field_id = self._discover_field_id("Planned start")
        print(f"🎯 Verified 'Planned start' Field ID: {self.planned_start_field_id}")

    def _discover_field_id(self, target_name):
        """Discovers the exact custom field key using direct REST API JSON."""
        try:
            url = f"{JIRA_SERVER}/rest/api/3/field"
            resp = requests.get(url, auth=self.auth, headers={"Accept": "application/json"}, timeout=10)
            if resp.status_code == 200:
                fields = resp.json()
                for f in fields:
                    if str(f.get('name', '')).strip().lower() == target_name.lower():
                        return f.get('id')
        except Exception as e:
            print(f"⚠️ Field discovery warning: {e}")
        return "customfield_11400" # Common default for planned start

    def fetch_candidates(self):
        try:
            print("🔍 Querying Jira for candidate changes...")
            return self.client.search_issues(JQL_QUERY, maxResults=60, fields=['*all'])
        except Exception as e:
            print(f"❌ Jira query error: {e}", file=sys.stderr)
            return []

    def get_planned_start_iso(self, issue):
        raw = issue.raw.get('fields', {})

        # 1. Check discovered field ID
        if self.planned_start_field_id and raw.get(self.planned_start_field_id):
            return str(raw[self.planned_start_field_id])

        # 2. Check common custom field IDs
        for key in ["customfield_11400", "customfield_10015", "customfield_10020", "customfield_10600", "plannedstart"]:
            val = raw.get(key)
            if val and isinstance(val, str) and len(val) >= 10:
                return val

        # 3. Search for any field value with matching date format
        for k, v in raw.items():
            if k in ["created", "updated", "resolutiondate", "lastViewed"]:
                continue
            if isinstance(v, str) and len(v) >= 10 and v.startswith("2026-") and v[4] == '-' and v[7] == '-':
                return v

        return None

# ==============================================================================
# 2. DATE PARSER (CONVERTS JIRA TIMESTAMP TO IST)
# ==============================================================================
def parse_to_ist(iso_string):
    if not iso_string:
        return None
    clean = iso_string.split('.')[0]
    for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]:
        try:
            dt = datetime.strptime(clean[:len(fmt) + 2], fmt)
            return dt.replace(tzinfo=IST)
        except Exception:
            pass
    try:
        dt = datetime.strptime(iso_string[:10], "%Y-%m-%d")
        return dt.replace(tzinfo=IST)
    except Exception:
        return None

# ==============================================================================
# 3. VERTEX AI (GEMINI 2.5 FLASH) TOC THINKER
# ==============================================================================
class SeniorTOCReviewer:
    def __init__(self, project_id, location):
        vertexai.init(project=project_id, location=location)
        self.model = GenerativeModel("gemini-2.5-flash")
        print("⚡ Vertex AI (Gemini 2.5 Flash) Senior Reviewer initialized.")

    def evaluate_and_clean_tickets(self, raw_candidates):
        system_instruction = """
        You are a Senior TOC Deployment Engineer at Osttra.
        Evaluate each candidate change ticket:
        
        RULES:
        1. EXCLUDE RAVI GOYAL:
           - Any ticket assigned to 'Ravi Goyal' or reported by 'Ravi Goyal' MUST be set to "is_toc_deployment": false.
        2. EXCLUDE PURE DBA TASKS:
           - Exclude tasks strictly owned by DBAs (e.g. database segregation, schema drops, tablespace migrations without TOC application deployment).
        3. KEEP TOC APPLICATION RELEASES:
           - Keep all software releases, patch drops, transforms, JVM updates, and go-lives across Osttra applications (TradeSTP, LimitHub, ETD, EQ, CFS, HAR, Harmony, SocGen, IC SA, DealHub, CCPC, MW, AAS, ODC, Cashflow, CL).
        4. CLEAN DESCRIPTION:
           - Strictly create a single-line summary (MAX 100 characters). Remove multi-line runbook URLs or tables.
        5. CLEAN APPLICATION:
           - Return clean short name: ("TradeSTP", "LimitHub", "ETD", "EQ", "CFS", "HAR", "Harmony", "SocGen", "IC SA", "DealHub", "CCPC", "MW", "AAS", "ODC", "Cashflow", "CL", "TS").
        6. ENV: "PROD", "UAT", "DR", or "PROD DR".
        7. TYPE: "Upload", "Normal", or "SC".

        OUTPUT STRICT JSON ONLY:
        {
            "reviewed_tickets": [
                {
                    "key": "...",
                    "is_toc_deployment": true or false,
                    "application": "...",
                    "clean_description": "...",
                    "env": "...",
                    "type": "...",
                    "implementor": "...",
                    "status": "..."
                }
            ]
        }
        """
        try:
            resp = self.model.generate_content(
                f"{system_instruction}\n\nCandidates:\n{json.dumps(raw_candidates, indent=2)}",
                generation_config=GenerationConfig(
                    response_mime_type="application/json",
                    temperature=0.0
                )
            )
            data = json.loads(resp.text.strip())
            return data.get("reviewed_tickets", [])
        except Exception as e:
            print(f"❌ AI Reviewer Error: {e}", file=sys.stderr)
            return []

# ==============================================================================
# 4. GOOGLE SHEETS & DRIVE ENGINE
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
        weekday = ref_date.weekday()
        sat = ref_date if weekday == 5 else (ref_date - timedelta(days=1) if weekday == 6 else ref_date + timedelta(days=(5 - weekday)))
        sun = sat + timedelta(days=1)
        filename = f"{sat.strftime('%d-%m-%Y')} & {sun.strftime('%d-%m-%Y')}"

        q = f"name contains '{filename}' and mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false"
        res = self.drive_api.files().list(q=q, fields="files(id, name)").execute()
        files = res.get('files', [])
        return (files[0]['id'], files[0]['name']) if files else (None, None)

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

    def create_vibrant_date_block(self, sheet_id, tab_name, date_str, rows_to_add, start_row_1_based):
        s_id_num = self.get_sheet_id_num(sheet_id, tab_name)
        block_rows = [
            [], # Empty spacer
            [date_str] + [""] * 8, # Row A: Date Banner
            ['Date', 'CR', 'Env', 'Type', 'Application', 'Description', 'Implementor', 'Start Time', 'Status'] # Row B: Header Row
        ] + rows_to_add

        self.append_rows(sheet_id, tab_name, block_rows)

        banner_row_1 = start_row_1_based + 2
        header_row_1 = start_row_1_based + 3
        data_start_row_1 = start_row_1_based + 4
        data_end_row_1 = data_start_row_1 + len(rows_to_add) - 1

        requests = [
            # 1. Merge Banner across Col A (0) to Col I (9)
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
            # 2. Azure Blue Banner (#1a73e8, Bold White Text)
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
                            "backgroundColor": {"red": 0.10, "green": 0.45, "blue": 0.91},
                            "textFormat": {"bold": True, "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}, "fontSize": 10},
                            "horizontalAlignment": "LEFT",
                            "verticalAlignment": "MIDDLE"
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
                }
            },
            # 3. Clear Data Validation (No Dropdowns) from Banner
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
            # 4. Deep Slate Navy Column Headers (#0d233a, Bold White Text)
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
                            "backgroundColor": {"red": 0.05, "green": 0.14, "blue": 0.23},
                            "textFormat": {"bold": True, "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}, "fontSize": 9},
                            "horizontalAlignment": "CENTER",
                            "verticalAlignment": "MIDDLE"
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
                }
            },
            # 5. Clear Data Validation from Column Header Row
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
        self.apply_clean_borders(sheet_id, s_id_num, data_start_row_1, data_end_row_1)

    def apply_clean_borders(self, sheet_id, s_id_num, start_row_1, end_row_1):
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
# 5. MAIN EXECUTION CYCLE
# ==============================================================================
def main():
    now_ist = datetime.now(IST)
    print(f"\n=======================================================")
    print(f"  AI-POWERED TOC RELEASE TRACKER | {now_ist.strftime('%Y-%m-%d %H:%M:%S IST')}")
    print(f"=======================================================")

    jira = JiraClient()
    ai_reviewer = SeniorTOCReviewer(GCP_PROJECT_ID, GCP_LOCATION)
    engine = GoogleSheetEngine()

    # 1. Fetch Candidates from Jira
    jira_issues = jira.fetch_candidates()
    if not jira_issues:
        print("No active candidates found in Jira. Exiting.")
        return

    # 2. Extract EXACT Planned Start Dates
    issue_date_map = {}
    serialized_candidates = []

    for issue in jira_issues:
        assignee_name = issue.fields.assignee.displayName if issue.fields.assignee else "Unassigned"

        # Explicitly ignore Ravi Goyal
        if "ravi goyal" in assignee_name.lower():
            print(f"🚫 Skipping {issue.key}: Assigned to Ravi Goyal.")
            continue

        raw_iso = jira.get_planned_start_iso(issue)
        planned_dt = parse_to_ist(raw_iso)

        if not planned_dt:
            print(f"⚠️ Skipping {issue.key}: No valid Planned Start date.")
            continue

        issue_date_map[issue.key] = planned_dt
        serialized_candidates.append({
            "key": issue.key,
            "summary": issue.fields.summary,
            "status": issue.fields.status.name,
            "assignee": assignee_name,
            "planned_date": planned_dt.strftime("%d/%m/%Y"),
            "description": (issue.fields.description or "")[:250]
        })

    # 3. Run AI Review
    reviewed_list = ai_reviewer.evaluate_and_clean_tickets(serialized_candidates)
    reviewed_dict = {t["key"]: t for t in reviewed_list if t.get("is_toc_deployment", True)}
    print(f"🧠 TOC Review Approved: {len(reviewed_dict)} deployment changes.")

    # 4. Read Existing Sheets to Protect Manual Edits
    daily_vals = engine.read_sheet(DAILY_SHEET_ID, DAILY_TAB_NAME)
    weekend_sheet_id, weekend_name = engine.resolve_weekend_sheet_id(now_ist)
    weekend_vals = engine.read_sheet(weekend_sheet_id, "Sheet1") if weekend_sheet_id else []

    existing_tracker = {}
    existing_daily_dates = set()

    # Parse Daily
    for i, row in enumerate(daily_vals):
        if not row or not row[0]: continue
        c0 = str(row[0]).strip()
        if '/' in c0 or '-' in c0:
            existing_daily_dates.add(c0)
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

    # 5. Group Tickets by Date and Destination
    daily_grouped_new = {}
    weekend_new_tickets = []

    for issue in jira_issues:
        key = issue.key
        if key not in reviewed_dict or key not in issue_date_map:
            continue

        t_info = reviewed_dict[key]
        planned_dt = issue_date_map[key]
        date_str = planned_dt.strftime("%d/%m/%Y")
        weekday_idx = planned_dt.weekday() # 5 = Saturday, 6 = Sunday
        is_weekend = weekday_idx >= 5
        j_stat = issue.fields.status.name

        cr_hyperlink = f'=HYPERLINK("{JIRA_SERVER}/browse/{key}", "{key}")'
        clean_desc = t_info.get("clean_description") or issue.fields.summary
        clean_desc = re.sub(r'[\r\n]+', ' ', clean_desc).strip()
        app_name = t_info.get("application", "")
        env = t_info.get("env", "PROD")
        type_val = t_info.get("type", "Normal")
        impl = t_info.get("implementor") or (issue.fields.assignee.displayName if issue.fields.assignee else "TOC")

        # Bi-directional sync if ticket already exists
        if key in existing_tracker:
            info = existing_tracker[key]
            s_stat = str(info["status"]).strip().lower()
            if j_stat.lower() in ["completed", "closed", "resolved", "done"] and s_stat not in ["done", "completed"]:
                print(f"🔄 Bi-directional Sync: Updating sheet status for {key} to 'Done'...")
                if info["type"] == "daily":
                    engine.update_cell(DAILY_SHEET_ID, DAILY_TAB_NAME, f"I{info['row']}", "Done")
                elif weekend_sheet_id:
                    engine.update_cell(weekend_sheet_id, "Sheet1", f"H{info['row']}", "Done")
                existing_tracker[key]["status"] = "Done"
            continue

        # Ingest New Ticket: Weekend vs Daily
        if is_weekend and weekend_sheet_id:
            w_section = "Saturday" if weekday_idx == 5 else "Sunday"
            print(f"🗓️ Ingesting {key} -> Weekend Sheet under {w_section} (Planned: {date_str})...")
            weekend_row = [
                cr_hyperlink, env, type_val, clean_desc, app_name,
                "TOC", "TOC", "Pending", "Pending", impl, "10:00:00", ""
            ]
            weekend_new_tickets.append((w_section, weekend_row))
            existing_tracker[key] = {"type": "weekend", "status": "Pending"}
        else:
            print(f"📅 Ingesting {key} -> Daily Changes Sheet ({date_str})...")
            daily_row = [
                date_str, cr_hyperlink, env, type_val, app_name,
                clean_desc, impl, "10:00:00", j_stat
            ]
            if date_str not in daily_grouped_new:
                daily_grouped_new[date_str] = []
            daily_grouped_new[date_str].append(daily_row)
            existing_tracker[key] = {"type": "daily", "status": j_stat}

    # 6. Write Grouped Daily Blocks
    current_daily_len = len(daily_vals)
    # Sort dates chronologically
    sorted_dates = sorted(daily_grouped_new.keys(), key=lambda d: datetime.strptime(d, "%d/%m/%Y"))

    for date_str in sorted_dates:
        rows = daily_grouped_new[date_str]
        if date_str in existing_daily_dates:
            print(f"📝 Appending {len(rows)} row(s) to existing block for {date_str}...")
            engine.append_rows(DAILY_SHEET_ID, DAILY_TAB_NAME, rows)
            current_daily_len += len(rows)
        else:
            print(f"🎨 Creating vibrant date block for {date_str} ({len(rows)} tickets)...")
            engine.create_vibrant_date_block(DAILY_SHEET_ID, DAILY_TAB_NAME, date_str, rows, current_daily_len)
            current_daily_len += len(rows) + 3
            existing_daily_dates.add(date_str)

    # 7. Write Weekend Tickets
    for w_section, row_data in weekend_new_tickets:
        if w_section == "Saturday" and weekend_sunday_row:
            engine.insert_row_at(weekend_sheet_id, "Sheet1", row_data, weekend_sunday_row)
            weekend_sunday_row += 1
        else:
            engine.append_rows(weekend_sheet_id, "Sheet1", [row_data])
        print(f"🗓️ Inserted weekend change into {w_section}.")

    # 8. Status Transition Detection & Working Hours Gate (9:00 AM - 9:00 PM IST)
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
            print(f"⚡ Status Transition: {k} changed from '{prev_s}' -> '{curr_s}'")
            status_changed = True

    with open(STATE_CACHE_FILE, "w") as f:
        json.dump({k: v["status"] for k, v in existing_tracker.items()}, f)

    is_working_hours = (9 <= now_ist.hour < 21)
    if status_changed:
        if is_working_hours:
            print("🚀 Inside working hours (09:00 - 21:00 IST). Triggering Chat broadcast immediately...")
            subprocess.run(["python", "chat_broadcast_bot.py"], check=False)
        else:
            print(f"🌙 Outside working window ({now_ist.strftime('%H:%M IST')}). Chat broadcast suppressed.")
    else:
        print("✅ No status transitions. Broadcast deferred to regular 2-hour schedule.")

if __name__ == "__main__":
    main()
