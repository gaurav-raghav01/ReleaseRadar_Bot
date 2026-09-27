import os
import sys
import json
import subprocess
from datetime import datetime, timedelta, timezone
from jira import JIRA

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

# High-recall JQL: Captures your team + all Traiana / TOC release projects over the next 10 days
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

TOC_TEAM_MEMBERS = ["Gaurav Raghav", "Chandan Kumar", "Kamaldeep Singh", "Rahul Dev Jha", "Parthiv Upreti"]

# ==============================================================================
# 1. JIRA SERVICE
# ==============================================================================
class JiraClient:
    def __init__(self):
        if not JIRA_EMAIL or not JIRA_API_TOKEN:
            print("❌ JIRA_EMAIL or JIRA_API_TOKEN missing!", file=sys.stderr)
            sys.exit(1)
        self.client = JIRA(server=JIRA_SERVER, basic_auth=(JIRA_EMAIL, JIRA_API_TOKEN))

    def fetch_candidates(self):
        try:
            print("🔍 Fetching active and upcoming candidate changes from Jira...")
            return self.client.search_issues(JQL_QUERY, maxResults=60, fields=['*all'])
        except Exception as e:
            print(f"❌ Jira query error: {e}", file=sys.stderr)
            return []

# ==============================================================================
# 2. VERTEX AI (GEMINI 2.5 FLASH) INTELLIGENT TOC THINKER
# ==============================================================================
class SeniorTOCOperationsAI:
    def __init__(self, project_id, location):
        vertexai.init(project=project_id, location=location)
        # Using Gemini 2.5 Flash for high reasoning quality and speed
        self.model = GenerativeModel("gemini-2.5-flash")
        print("⚡ Vertex AI (Gemini 2.5 Flash) Senior TOC Thinker initialized.")

    def evaluate_and_route_tickets(self, jira_issues, today_date_str):
        serialized = []
        for issue in jira_issues:
            raw = issue.raw.get('fields', {})
            p_start = (
                raw.get("customfield_10015") or 
                raw.get("plannedstart") or 
                raw.get("customfield_11400") or 
                str(getattr(issue.fields, "created", ""))
            )
            assignee = issue.fields.assignee.displayName if issue.fields.assignee else "Unassigned"
            summary = issue.fields.summary or ""
            desc = (issue.fields.description or "")[:350]
            services = raw.get("services") or raw.get("affectedServices") or raw.get("components") or ""

            serialized.append({
                "key": issue.key,
                "project": issue.fields.project.key,
                "summary": summary,
                "status": issue.fields.status.name,
                "assignee": assignee,
                "reporter": issue.fields.reporter.displayName if issue.fields.reporter else "",
                "planned_start": p_start,
                "affected_services": services,
                "description": desc
            })

        system_instruction = f"""
        You are a Senior Technology Operations Centre (TOC) Deployment Engineer at Osttra.
        Your team consists of: Gaurav Raghav, Chandan Kumar, Kamaldeep Singh, Rahul Dev Jha, Parthiv Upreti.
        Today's Reference Date: {today_date_str} (IST)

        Evaluate each Jira ticket and act as a critical human filter:
        
        RULE 1: IS THIS A REAL TOC APPLICATION DEPLOYMENT?
        - KEEP: Software release, application deployment, patch release, hotfix, transform update, config standardization, JVM tuning, go-live, or microservice release across our application stack (TradeSTP, LimitHub, ETD, EQ, CFS, HAR/CL, Harmony, SocGen, IC-SA, DealHub, CCPC, MW, AAS, ODC, Cashflow).
        - KEEP EVEN IF NOT ASSIGNED TO OUR TEAM YET: If a ticket is currently assigned to a Scrum Master, QA, or requester (e.g. Surbhi Bansal, Monika Saini, Ankit Agrawal, Rajendra Prasad, Ravi Goyal), but it represents an application release or deployment task that TOC executes, KEEP IT! Map implementor to the assignee or 'TOC'.
        - DISCARD / EXCLUDE: 
          * Pure Database administration tasks owned strictly by DBAs (e.g., 'TRMPLATFORM-10064 JPTRM database segregation in Production' or DB migrations where DBA is implementor and TOC has no application task).
          * Infrastructure network cabling, AWS secrets generation, or non-deployment Jira tickets.

        RULE 2: EXTRACTION & ROUTING
        - Extract the exact scheduled date in 'DD/MM/YYYY' format from 'planned_start' or summary.
        - Destination Routing:
          * "daily": Monday through Friday releases -> go to Daily Changes sheet.
          * "weekend": Saturday or Sunday releases -> go to Weekend sheet.
        - If "weekend", specify "weekend_section": "Saturday" or "Sunday".
        - Map "application" to a clean dropdown value:
          ("TradeSTP", "LimitHub", "ETD", "EQ", "CFS", "HAR", "Harmony", "SocGen", "IC SA", "DealHub", "CCPC", "MW", "AAS", "ODC", "Cashflow", "CL").
        - Determine "env": "PROD", "UAT", "DR", or "PROD DR".
        - Format "type": "Upload", "Normal", or "SC".

        OUTPUT STRICT JSON ONLY:
        {{
            "accepted_tickets": [
                {{
                    "key": "...",
                    "target": "daily" or "weekend",
                    "date_str": "DD/MM/YYYY",
                    "weekend_section": "Saturday" or "Sunday" or null,
                    "env": "...",
                    "type": "...",
                    "application": "...",
                    "description": "...",
                    "implementor": "...",
                    "start_time": "10:00:00",
                    "status": "...",
                    "reason_accepted": "..."
                }}
            ],
            "excluded_tickets": [
                {{
                    "key": "...",
                    "reason_excluded": "e.g. DBA owned task / Not TOC deployment"
                }}
            ]
        }}
        """

        prompt = f"Raw Tickets:\n{json.dumps(serialized, indent=2)}"

        try:
            resp = self.model.generate_content(
                f"{system_instruction}\n\n{prompt}",
                generation_config=GenerationConfig(
                    response_mime_type="application/json",
                    temperature=0.1
                )
            )
            data = json.loads(resp.text.strip())
            accepted = data.get("accepted_tickets", [])
            excluded = data.get("excluded_tickets", [])
            print(f"🎯 AI Decision: Accepted {len(accepted)} TOC deployment changes | Filtered out {len(excluded)} non-TOC items.")
            for ex in excluded:
                print(f"   🚫 Discarded {ex.get('key')}: {ex.get('reason_excluded')}")
            return accepted
        except Exception as e:
            print(f"❌ AI Thinker Error: {e}", file=sys.stderr)
            return []

# ==============================================================================
# 3. GOOGLE SHEETS & DRIVE ENGINE
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
        """Creates a vibrant, polished date block with styled headers and crisp gridlines."""
        s_id_num = self.get_sheet_id_num(sheet_id, tab_name)
        block_rows = [
            [], # Empty spacer row
            [date_str] + [""] * 8, # Row A: Date Banner
            ['Date', 'CR', 'Env', 'Type', 'Application', 'Description', 'Implementor', 'Start Time', 'Status'] # Row B: Header Row
        ] + rows_to_add

        self.append_rows(sheet_id, tab_name, block_rows)

        banner_row_1 = start_row_1_based + 2
        header_row_1 = start_row_1_based + 3
        data_start_row_1 = start_row_1_based + 4
        data_end_row_1 = data_start_row_1 + len(rows_to_add) - 1

        requests = [
            # 1. Merge Date Banner
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
# 4. MAIN ORCHESTRATION CYCLE
# ==============================================================================
def main():
    now_ist = datetime.now(IST)
    today_date_str = now_ist.strftime("%d/%m/%Y")
    print(f"\n=======================================================")
    print(f"  AI-POWERED TOC RELEASE TRACKER | {now_ist.strftime('%Y-%m-%d %H:%M:%S IST')}")
    print(f"=======================================================")

    jira = JiraClient()
    ai_thinker = SeniorTOCOperationsAI(GCP_PROJECT_ID, GCP_LOCATION)
    engine = GoogleSheetEngine()

    # 1. Fetch raw candidates
    jira_issues = jira.fetch_candidates()
    if not jira_issues:
        print("No active candidates in Jira. Exiting.")
        return

    # 2. Run Gemini 2.5 Flash Thinking Engine to evaluate & filter
    smart_tickets = ai_thinker.evaluate_and_route_tickets(jira_issues, today_date_str)
    print(f"🧠 Accepted {len(smart_tickets)} legitimate TOC release changes.")

    # 3. Read sheets and map existing state
    daily_vals = engine.read_sheet(DAILY_SHEET_ID, DAILY_TAB_NAME)
    weekend_sheet_id, weekend_name = engine.resolve_weekend_sheet_id(now_ist)
    weekend_vals = engine.read_sheet(weekend_sheet_id, "Sheet1") if weekend_sheet_id else []

    existing_tracker = {}

    # Parse Daily
    daily_block_start = {}
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

    # 4. Ingest Accepted Tickets
    for item in smart_tickets:
        k = item["key"]
        j_stat = item.get("status", "Pending")
        target = item.get("target", "daily")
        date_str = item.get("date_str", today_date_str)
        cr_hyperlink = f'=HYPERLINK("{JIRA_SERVER}/browse/{k}", "{k}")'

        # Rule: Ticket already exists -> Check bi-directional completion sync, never overwrite manual edits
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
            print(f"🗓️ Ingesting {k} into Weekend Sheet ({w_section})...")
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
            if date_str in daily_block_start:
                engine.append_rows(DAILY_SHEET_ID, DAILY_TAB_NAME, [daily_row])
            else:
                print(f"Creating polished vibrant date block for {date_str}...")
                engine.create_vibrant_date_block(DAILY_SHEET_ID, DAILY_TAB_NAME, date_str, [daily_row], len(daily_vals))
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
