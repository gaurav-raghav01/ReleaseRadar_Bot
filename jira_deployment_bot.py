import os
import sys
import json
from datetime import datetime, timedelta, timezone
import requests
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

JQL_QUERY = os.getenv(
    "JQL_QUERY",
    """
    (worktype = Change OR issuetype = Change)
    AND assignee IN ("Gaurav Raghav", "Chandan Kumar", "Kamaldeep Singh", "Rahul Dev Jha")
    AND "Planned start" >= startOfDay() AND "Planned start" <= endOfDay()
    ORDER BY "Planned start" ASC
    """
)

GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "prj-s-ai-bot-4rl8")
GCP_LOCATION = os.getenv("GCP_LOCATION", "us-central1")

GOOGLE_SHEET_FILE_ID = os.getenv("GOOGLE_SHEET_FILE_ID", "1NwJd8uzGIcDTxE0XJ_VYbqJfQnjuqxB4wh5UR97SIlk")
GOOGLE_WORKSHEET_NAME = os.getenv("GOOGLE_WORKSHEET_NAME", "Daily Changes")

WEBHOOK_URL = os.getenv("WEBHOOK_URL")
IST = timezone(timedelta(hours=5, minutes=30))

DONE_STATUSES = {"done", "completed", "closed", "resolved", "implemented successfully", "success"}
IN_PROGRESS_STATUSES = {"in progress", "work in progress", "implementing", "started", "awaiting implementation"}

# Known dropdown options in your Google Sheet (from input_file_22.png)
KNOWN_APPS = [
    "IC SA", "TradeSTP", "DealHub", "LimitHub", "MW", "OTM", "TN", "Transformers",
    "STPaaS", "FXBA", "Traiana", "Adapter", "CCPC", "JSW", "H5/CL", "CRDS", "H3",
    "DNM", "SSO", "CSM", "CFD", "ODC", "EQ", "Cashflow", "LH", "CreditLink",
    "SocGen", "SCB", "ETD", "Baton", "AAS", "DMS", "SIGMA", "HSBC TRM", "IC LH UI",
    "Alpine", "CITI TRM", "Apigee", "Jigsaw", "DB TRM", "BOFA TRM", "CFS", "JPTRM",
    "NextGen Citi", "UBS", "ZKB", "BNPP", "3C", "WFM", "H4", "CLFX"
]

# ==============================================================================
# JIRA SERVICE (WITH DYNAMIC AFFECTED SERVICES EXTRACTION)
# ==============================================================================
class JiraService:
    def __init__(self, server, email, token):
        self.client = None
        if not email or not token:
            print("❌ ERROR: JIRA_EMAIL or JIRA_API_TOKEN is missing!", file=sys.stderr)
            sys.exit(1)
        try:
            self.client = JIRA(server=server, basic_auth=(email, token))
            print("✅ Successfully connected to Jira.")
        except Exception as e:
            print(f"❌ ERROR: Could not connect to Jira: {e}", file=sys.stderr)
            sys.exit(1)

    def extract_affected_service(self, issue):
        """
        Extracts the 'Affected services' field from Jira issue data.
        Falls back to summary inspection matching your sheet's dropdowns.
        """
        raw_fields = issue.raw.get('fields', {})

        # 1. Direct standard field check
        for attr in ['services', 'affectedServices', 'components']:
            val = raw_fields.get(attr)
            if val and isinstance(val, list):
                for item in val:
                    name = item.get('name') if isinstance(item, dict) else str(item)
                    if name:
                        for app in KNOWN_APPS:
                            if app.lower() == name.lower():
                                return app
                        return name

        # 2. Iterate all custom fields to find the 'Affected services' object
        for k, v in raw_fields.items():
            if not v:
                continue
            # Look inside list of dicts (standard for service management assets)
            if isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        name = item.get('name') or item.get('value')
                        if name:
                            for app in KNOWN_APPS:
                                if app.lower() == str(name).lower():
                                    return app
            # Look inside single dict
            elif isinstance(v, dict):
                name = v.get('name') or v.get('value')
                if name:
                    for app in KNOWN_APPS:
                        if app.lower() == str(name).lower():
                            return app

        # 3. Fallback: Deduce from Summary or Description
        text = (issue.fields.summary or "") + " " + (issue.fields.description or "")
        for app in KNOWN_APPS:
            if f" {app.lower()} " in f" {text.lower()} " or app.lower() in text.lower():
                return app

        return ""

    def get_planned_changes(self):
        try:
            print("🔍 Querying Jira for today's planned changes...")
            issues = self.client.search_issues(
                JQL_QUERY,
                maxResults=50,
                fields=['*all']  # Fetch all custom fields so Affected Services is included
            )
            print(f" Jira found {len(issues)} planned ticket(s).")
            return issues
        except Exception as e:
            print(f"❌ ERROR: Failed to query Jira: {e}", file=sys.stderr)
            return []

# ==============================================================================
# GOOGLE SHEETS SERVICE WITH CLEAN FORMATTING & HYPERLINKING
# ==============================================================================
class GoogleSheetService:
    def __init__(self, spreadsheet_id, sheet_name):
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name
        try:
            print("Authenticating to Google Sheets using ADC...")
            scopes = [
                'https://www.googleapis.com/auth/spreadsheets',
                'https://www.googleapis.com/auth/drive'
            ]
            self.credentials, _ = google.auth.default(scopes=scopes)
            self.service = build('sheets', 'v4', credentials=self.credentials)
            self.sheet_id_num = self._get_sheet_id_num()
            print("✅ Successfully connected to Google Sheets API.")
        except Exception as e:
            print(f"❌ ERROR: Failed to build Sheets API client: {e}", file=sys.stderr)
            sys.exit(1)

    def _get_sheet_id_num(self):
        metadata = self.service.spreadsheets().get(spreadsheetId=self.spreadsheet_id).execute()
        for s in metadata.get('sheets', []):
            if s.get("properties", {}).get("title") == self.sheet_name:
                return s.get("properties", {}).get("sheetId")
        return 0

    def get_all_values(self):
        range_name = f"'{self.sheet_name}'!A:Z"
        res = self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id, range=range_name
        ).execute()
        return res.get('values', [])

    def append_rows(self, rows_data):
        range_name = f"'{self.sheet_name}'!A:A"
        res = self.service.spreadsheets().values().append(
            spreadsheetId=self.spreadsheet_id,
            range=range_name,
            valueInputOption='USER_ENTERED',
            insertDataOption='INSERT_ROWS',
            body={'values': rows_data}
        ).execute()
        return res

    def insert_rows_at(self, rows_data, start_row_index_1_based):
        start_0 = start_row_index_1_based - 1
        num_rows = len(rows_data)

        body = {
            "requests": [
                {
                    "insertDimension": {
                        "range": {
                            "sheetId": self.sheet_id_num,
                            "dimension": "ROWS",
                            "startIndex": start_0,
                            "endIndex": start_0 + num_rows
                        },
                        "inheritFromBefore": False
                    }
                }
            ]
        }
        self.service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id, body=body
        ).execute()

        self.service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{self.sheet_name}'!A{start_row_index_1_based}",
            valueInputOption='USER_ENTERED',
            body={'values': rows_data}
        ).execute()

    def parse_sheet_for_today(self, all_values):
        today_str = datetime.now(IST).strftime("%d/%m/%Y")
        today_str_alt = datetime.now(IST).strftime("%d/%m/%y")

        today_block_start = -1
        next_block_start = -1
        existing_today_tickets = {}

        for idx, row in enumerate(all_values):
            if not row or not row[0]:
                continue
            cell = str(row[0]).strip()

            if today_str in cell or today_str_alt in cell:
                if today_block_start == -1:
                    today_block_start = idx + 1
            elif today_block_start != -1 and ('/' in cell or '-' in cell):
                next_block_start = idx + 1
                break

            if today_block_start != -1 and len(row) > 1 and row[1]:
                # Strip out formula if hyperlinked, e.g. =HYPERLINK("...", "INTU-313")
                raw_cell = row[1].strip()
                t_key = raw_cell
                if '", "' in raw_cell:
                    t_key = raw_cell.split('", "')[-1].replace('")', '').strip()
                elif '",' in raw_cell:
                    t_key = raw_cell.split('",')[-1].replace(')', '').replace('"', '').strip()

                if t_key not in ["CR", "Change"]:
                    existing_today_tickets[t_key] = {
                        "row": idx + 1,
                        "env": row[2].strip() if len(row) > 2 else "",
                        "type": row[3].strip() if len(row) > 3 else "",
                        "app": row[4].strip() if len(row) > 4 else "",
                        "desc": row[5].strip() if len(row) > 5 else "",
                        "owner": row[6].strip() if len(row) > 6 else "",
                        "status": row[8].strip() if len(row) > 8 else ""
                    }

        insertion_index = next_block_start if next_block_start != -1 else len(all_values) + 1
        return today_block_start, insertion_index, existing_today_tickets

    def create_clean_today_block(self, rows_to_add, start_row_1_based):
        today_str = datetime.now(IST).strftime("%d/%m/%Y")
        all_block_rows = [
            [],
            [today_str] + [""] * 8,
            ['Date', 'CR', 'Env', 'Type', 'Application', 'Description', 'Implementor', 'Start Time', 'Status']
        ] + rows_to_add

        self.append_rows(all_block_rows)

        banner_row_1 = start_row_1_based + 2
        header_row_1 = start_row_1_based + 3
        data_start_row_1 = start_row_1_based + 4
        data_end_row_1 = data_start_row_1 + len(rows_to_add) - 1

        requests_list = [
            # A. MERGE Banner Col A-I
            {
                "mergeCells": {
                    "range": {
                        "sheetId": self.sheet_id_num,
                        "startRowIndex": banner_row_1 - 1,
                        "endRowIndex": banner_row_1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 9
                    },
                    "mergeType": "MERGE_ALL"
                }
            },
            # B. Style Banner (Blue fill, White bold text)
            {
                "repeatCell": {
                    "range": {
                        "sheetId": self.sheet_id_num,
                        "startRowIndex": banner_row_1 - 1,
                        "endRowIndex": banner_row_1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 9
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": {"red": 0.29, "green": 0.53, "blue": 0.91},
                            "textFormat": {"bold": True, "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}, "fontSize": 10},
                            "horizontalAlignment": "LEFT",
                            "verticalAlignment": "MIDDLE"
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
                }
            },
            # C. REMOVE DROPDOWNS from Banner Row
            {
                "setDataValidation": {
                    "range": {
                        "sheetId": self.sheet_id_num,
                        "startRowIndex": banner_row_1 - 1,
                        "endRowIndex": banner_row_1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 9
                    },
                    "rule": None
                }
            },
            # D. Style Column Header Row (Dark Navy #0b5394, Bold White)
            {
                "repeatCell": {
                    "range": {
                        "sheetId": self.sheet_id_num,
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
            # E. REMOVE DROPDOWNS from Header Row
            {
                "setDataValidation": {
                    "range": {
                        "sheetId": self.sheet_id_num,
                        "startRowIndex": header_row_1 - 1,
                        "endRowIndex": header_row_1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 9
                    },
                    "rule": None
                }
            }
        ]

        self.service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id, body={"requests": requests_list}
        ).execute()

        self.format_borders(data_start_row_1, data_end_row_1)
        print("🎨 Cleaned block: Dropdowns removed from Banner & Headers. Formatted correctly!")

    def format_borders(self, start_row_1, end_row_1):
        border_spec = {
            "style": "SOLID",
            "width": 1,
            "color": {"red": 0.85, "green": 0.85, "blue": 0.85}
        }
        body = {
            "requests": [
                {
                    "updateBorders": {
                        "range": {
                            "sheetId": self.sheet_id_num,
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
        self.service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id, body=body
        ).execute()

# ==============================================================================
# STATUS PARITY LOGIC
# ==============================================================================
def resolve_parity(sheet_data, jira_ticket):
    s_status = (sheet_data.get("status") if sheet_data else "").strip().lower()
    j_status = (jira_ticket.fields.status.name if jira_ticket else "").strip().lower()

    if s_status in DONE_STATUSES or j_status in DONE_STATUSES:
        return "Done", "✅"
    elif s_status in IN_PROGRESS_STATUSES or j_status in IN_PROGRESS_STATUSES:
        return "In Progress", "⚙️"
    elif "fail" in s_status or "fail" in j_status or "back" in s_status or "back" in j_status:
        return "Failed/Backed Out", "🚧"
    else:
        return "Pending", "⏳"

# ==============================================================================
# VERTEX AI / GEMINI SERVICE
# ==============================================================================
class GeminiService:
    def __init__(self, project_id, location):
        vertexai.init(project=project_id, location=location)
        self.model = GenerativeModel("gemini-2.5-flash")
        print("✅ Vertex AI (Gemini 2.5 Flash) initialized.")

    def analyze_unified_deployments(self, unified_list):
        prompt = f"""
        Analyze this raw deployment data from our Deployment Tracker (Jira + manual Sheet entries).
        Data:
        {json.dumps(unified_list, indent=2)}

        You are an intelligent Release Manager. Do the following:
        1. Write a 1-sentence "executive_summary" summarizing today's overall deployment health.
        2. For "comments", analyze description and recent comments for issues/blockers. If none, leave "".
        3. Create a "short_desc" (MAX 3 to 5 words, strictly under 20 characters).
        4. Deduce the "application" and "environment".
        5. For "status", strictly use the provided "effective_status".
        6. For "status_emoji", strictly use the provided "effective_emoji".

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
            resp = self.model.generate_content(prompt)
            clean_json = resp.text.strip().replace("```json", "").replace("```", "")
            return json.loads(clean_json)
        except Exception as e:
            print(f"❌ Failed to parse Gemini response: {e}")
            return None

# ==============================================================================
# GOOGLE CHAT NOTIFIER
# ==============================================================================
def send_to_gchat(card_data):
    if not card_data or not WEBHOOK_URL:
        print("⚠️ Skipping Chat: No data or WEBHOOK_URL not configured.")
        return

    summary = card_data.get("executive_summary", "")
    deployments = card_data.get("deployments", [])

    headers = ["Change No", "App", "Desc", "Env", "St's", "Owner"]
    rows = []
    for dep in deployments:
        c_no = str(dep.get('change_no', 'N/A'))[:15]
        app = str(dep.get('application', 'N/A'))[:8]
        desc = str(dep.get('short_desc', 'N/A'))[:20]
        env = str(dep.get('environment', 'N/A'))[:6]
        emoji = dep.get('status_emoji', '⏳')
        owner = str(dep.get('implementer', 'N/A')).split(" ")[0][:10]
        rows.append([c_no, app, desc, env, emoji, owner])

    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            display_length = len(cell) - 1 if any(e in cell for e in ['✅','⚙️','⏳','🚧']) else len(cell)
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

    comments_section = ""
    for dep in deployments:
        key = dep.get('change_no', 'N/A')
        emoji = dep.get('status_emoji', '')
        c = dep.get("comments", "").strip()
        is_done = emoji == "✅" or "done" in str(dep.get('status', '')).lower()
        if not is_done and c and c.lower() not in ["none", "-", "n/a", "null", ""]:
            comments_section += f"• **<{JIRA_SERVER}/browse/{key}|{key}>**: {c}\n"

    today_date = datetime.now(IST).strftime('%d/%m/%Y')
    msg = f"*Hi All, Planned Changes for Today ({today_date}) (ReleaseRadar)*\n\n"
    if summary:
        msg += f"🤖 *AI Summary:* {summary}\n\n"
    msg += f"```\n{table_content}\n```\n"
    msg += "*(Legend: ✅ Done | ⚙️ In Progress | ⏳ Pending | 🚧 Backed Out/Failed)*\n"

    if comments_section:
        msg += f"\n*⚡ Live Radar: Actionable Insights*\n{comments_section}"

    resp = requests.post(WEBHOOK_URL, headers={"Content-Type": "application/json"}, data=json.dumps({"text": msg}))
    if resp.status_code == 200:
        print("✅ Google Chat update broadcasted successfully!")
    else:
        print(f"❌ Chat post failed: {resp.status_code}, {resp.text}")

# ==============================================================================
# MAIN SYNC CYCLE
# ==============================================================================
def main():
    print(f"\n--- Starting Integrated ReleaseRadar Bot ({datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}) ---")

    jira = JiraService(JIRA_SERVER, JIRA_EMAIL, JIRA_API_TOKEN)
    sheets = GoogleSheetService(GOOGLE_SHEET_FILE_ID, GOOGLE_WORKSHEET_NAME)
    gemini = GeminiService(GCP_PROJECT_ID, GCP_LOCATION)

    jira_tickets = jira.get_planned_changes()

    # 1. Parse existing sheet to preserve manual entries and formatting
    all_values = sheets.get_all_values()
    today_block_row, insertion_idx, existing_sheet_tickets = sheets.parse_sheet_for_today(all_values)

    new_tickets_to_add = []
    jira_dict = {}
    for t in jira_tickets:
        jira_dict[t.key] = t
        if t.key not in existing_sheet_tickets:
            new_tickets_to_add.append(t)

    print(f"[Sync] {len(existing_sheet_tickets)} tickets already in sheet (PERSISTED). {len(new_tickets_to_add)} new Jira ticket(s) to add.")

    # 2. Add new tickets with HYPERLINK and EXTRACTED AFFECTED SERVICE
    if new_tickets_to_add:
        rows_to_add = []
        for t in new_tickets_to_add:
            summary = t.fields.summary
            env = "PROD" if "prod" in summary.lower() else "UAT"
            assignee = t.fields.assignee.displayName if t.fields.assignee else "Unassigned"

            # 🔗 Clickable Jira Hyperlink Formula
            jira_link_formula = f'=HYPERLINK("{JIRA_SERVER}/browse/{t.key}", "{t.key}")'

            # 🎯 Extract Affected Service (e.g. "IC SA")
            app_service = jira.extract_affected_service(t)

            rows_to_add.append([
                datetime.now(IST).strftime("%d/%m/%Y"), # Col A: Date
                jira_link_formula,                      # Col B: CR (Clickable Link)
                env,                                    # Col C: Env
                "Upload",                               # Col D: Type
                app_service,                            # Col E: Application (From Affected services)
                summary,                                # Col F: Description
                assignee,                               # Col G: Implementor
                "10:00:00",                             # Col H: Start Time
                t.fields.status.name                    # Col I: Status
            ])

        if today_block_row == -1:
            print("Creating styled today block with hyperlinked tickets & clean headers...")
            sheets.create_clean_today_block(rows_to_add, len(all_values))
        else:
            print(f"Inserting {len(rows_to_add)} new row(s) at line {insertion_idx}...")
            sheets.insert_rows_at(rows_to_add, insertion_idx)
    else:
        print("✅ No new tickets to insert. All existing manual entries and statuses are untouched.")

    # 3. Prepare Unified List for Gemini Analysis & Chat
    unified_list = []
    all_active_keys = set(existing_sheet_tickets.keys()) | set(jira_dict.keys())

    for key in all_active_keys:
        s_data = existing_sheet_tickets.get(key)
        j_data = jira_dict.get(key)

        effective_status, effective_emoji = resolve_parity(s_data, j_data)

        desc = s_data.get("desc") if (s_data and s_data.get("desc")) else (j_data.fields.summary if j_data else key)
        owner = s_data.get("owner") if (s_data and s_data.get("owner")) else (j_data.fields.assignee.displayName if j_data and j_data.fields.assignee else "TOC")
        env = s_data.get("env") if (s_data and s_data.get("env")) else ("PROD" if "prod" in desc.lower() else "UAT")
        app = s_data.get("app") if (s_data and s_data.get("app")) else (jira.extract_affected_service(j_data) if j_data else "")

        comments = []
        if j_data and hasattr(j_data.fields, 'comment') and j_data.fields.comment:
            comments = [c.body for c in (j_data.fields.comment.comments or [])[-3:]]

        unified_list.append({
            "ticket_key": key,
            "summary": desc,
            "application": app,
            "environment": env,
            "assignee": owner,
            "effective_status": effective_status,
            "effective_emoji": effective_emoji,
            "recent_comments": comments
        })

    # 4. Gemini Processing & Chat Broadcast
    analysis = gemini.analyze_unified_deployments(unified_list)
    if analysis:
        send_to_gchat(analysis)

if __name__ == "__main__":
    main()
