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

# Precise JQL matching your team for today
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
IN_PROGRESS_STATUSES = {"in progress", "work in progress", "implementing", "started"}

# ==============================================================================
# JIRA SERVICE
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

    def get_planned_changes(self):
        try:
            print("🔍 Querying Jira for today's planned changes...")
            issues = self.client.search_issues(
                JQL_QUERY,
                maxResults=50,
                fields=['summary', 'status', 'assignee', 'description', 'comment']
            )
            print(f" Jira found {len(issues)} planned ticket(s).")
            return issues
        except Exception as e:
            print(f"❌ ERROR: Failed to query Jira: {e}", file=sys.stderr)
            return []

# ==============================================================================
# GOOGLE SHEETS SERVICE WITH FORMATTING SUPPORT
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

    def update_range(self, cell_range, values):
        self.service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{self.sheet_name}'!{cell_range}",
            valueInputOption='USER_ENTERED',
            body={'values': values}
        ).execute()

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
        
        # 1. Insert blank rows
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

        # 2. Write values
        self.update_range(f"A{start_row_index_1_based}", rows_data)
        
        # 3. Apply cell styling (borders and dropdown styling)
        self.format_data_rows(start_row_index_1_based, start_row_index_1_based + num_rows - 1)

    def find_insertion_point_and_statuses(self, all_values):
        today_str = datetime.now(IST).strftime("%d/%m/%Y")
        today_str_alt = datetime.now(IST).strftime("%d/%m/%y")

        today_block_start = -1
        next_block_start = -1
        sheet_ticket_statuses = {}  # {ticket_key: status_str}

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

            # If inside today's block, track ticket statuses from Column B (CR) & Column I (Status)
            if today_block_start != -1 and len(row) > 1 and row[1]:
                t_key = row[1].strip()
                t_status = row[8].strip() if len(row) > 8 else ""
                sheet_ticket_statuses[t_key] = t_status

        insertion_index = next_block_start if next_block_start != -1 else len(all_values) + 1
        return today_block_start, insertion_index, sheet_ticket_statuses

    def create_styled_today_block(self, rows_to_add, start_row_1_based):
        """
        Creates the complete colorful date banner, headers, and rows with matching borders & colors.
        """
        today_str = datetime.now(IST).strftime("%d/%m/%Y")
        all_block_rows = [
            [], # Empty spacer
            [today_str] + [""] * 8, # Date banner row
            ['Date', 'CR', 'Env', 'Type', 'Application', 'Description', 'Implementor', 'Start Time', 'Status']
        ] + rows_to_add

        # 1. Append values
        self.append_rows(all_block_rows)

        banner_row_1 = start_row_1_based + 1
        header_row_1 = start_row_1_based + 2
        data_start_row_1 = start_row_1_based + 3
        data_end_row_1 = data_start_row_1 + len(rows_to_add) - 1

        # 2. Batch format request: Merge banner, apply colors, borders, font weights
        requests_list = [
            # A. Merge Date Banner (Col A to I)
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
            # B. Format Date Banner (Light Blue fill, Bold Text, Centered)
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
                            "backgroundColor": {"red": 0.44, "green": 0.71, "blue": 0.98}, # #70b5f9
                            "textFormat": {"bold": True, "foregroundColor": {"red": 0.0, "green": 0.0, "blue": 0.0}, "fontSize": 10},
                            "horizontalAlignment": "CENTER"
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"
                }
            },
            # C. Format Column Headers (Dark Navy fill, White Bold Text)
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
                            "backgroundColor": {"red": 0.09, "green": 0.45, "blue": 0.91}, # #1773e8
                            "textFormat": {"bold": True, "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}, "fontSize": 9},
                            "horizontalAlignment": "CENTER"
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"
                }
            }
        ]

        # Apply borders and formats
        self.service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"requests": requests_list}
        ).execute()

        # Apply borders to data rows
        self.format_data_rows(data_start_row_1, data_end_row_1)
        print("🎨 Successfully styled today's block with matching colors and headers!")

    def format_data_rows(self, start_row_1, end_row_1):
        """Applies grid borders and alignments to data rows."""
        border_spec = {
            "style": "SOLID",
            "width": 1,
            "color": {"red": 0.8, "green": 0.8, "blue": 0.8}
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
def resolve_parity_status(jira_status, sheet_status):
    """
    Parity Rule:
    - If either Sheet OR Jira says Done/Completed -> Done (✅)
    - Else if either is In Progress -> In Progress (⚙️)
    - Else -> Pending (⏳)
    """
    j_stat = (jira_status or "").strip().lower()
    s_stat = (sheet_status or "").strip().lower()

    if s_stat in DONE_STATUSES or j_stat in DONE_STATUSES:
        return "Done", "✅"
    elif s_stat in IN_PROGRESS_STATUSES or j_stat in IN_PROGRESS_STATUSES:
        return "In Progress", "⚙️"
    elif "fail" in s_stat or "fail" in j_stat or "back" in s_stat or "back" in j_stat:
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

    def analyze_jira_data(self, jira_issues, sheet_statuses):
        deployments = []
        for issue in jira_issues:
            fields = issue.fields
            comments = [c.body for c in (fields.comment.comments or [])[-3:]]
            assignee = fields.assignee.displayName if fields.assignee else "Unassigned"
            
            # Apply Status Parity Rule
            sheet_stat = sheet_statuses.get(issue.key, "")
            effective_status, effective_emoji = resolve_parity_status(fields.status.name, sheet_stat)

            deployments.append({
                "ticket_key": issue.key,
                "summary": fields.summary,
                "jira_status": fields.status.name,
                "sheet_status": sheet_stat,
                "effective_status": effective_status,
                "effective_emoji": effective_emoji,
                "assignee": assignee,
                "description": fields.description or "",
                "recent_comments": comments
            })

        prompt = f"""
        Analyze this raw deployment data fetched directly from Jira with live sheet status overrides.
        Data:
        {json.dumps(deployments, indent=2)}

        You are an intelligent Release Manager. Do the following:
        1. Write a 1-sentence "executive_summary" summarizing today's overall deployment health.
        2. For "comments", analyze description and recent_comments for issues/blockers. If none, leave "".
        3. Create a "short_desc" (MAX 3 to 5 words, strictly under 20 characters).
        4. Deduce the "application" and "environment".
        5. Respect the given "effective_status" and "effective_emoji" in the input!

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
        print("✅ Google Chat update broadcasted successfully with parity!")
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
    if not jira_tickets:
        print("No planned changes found in Jira for today. Exiting.")
        return

    # 1. Read sheet to inspect existing entries and current Sheet statuses
    all_values = sheets.get_all_values()
    today_block_row, insertion_idx, sheet_statuses = sheets.find_insertion_point_and_statuses(all_values)

    existing_keys = set(sheet_statuses.keys())
    for row in all_values:
        for cell in row:
            for issue in jira_tickets:
                if issue.key in str(cell):
                    existing_keys.add(issue.key)

    new_tickets = [t for t in jira_tickets if t.key not in existing_keys]
    print(f"[Sync] {len(existing_keys)} tickets in sheet. {len(new_tickets)} new ticket(s) to add.")

    # 2. Append/Insert new tickets with styling if needed
    if new_tickets:
        rows_to_add = []
        for t in new_tickets:
            summary = t.fields.summary
            env = "PROD" if "prod" in summary.lower() else "UAT"
            assignee = t.fields.assignee.displayName if t.fields.assignee else "Unassigned"
            rows_to_add.append([
                datetime.now(IST).strftime("%d/%m/%Y"), # Date
                t.key,                                   # CR
                env,                                     # Env
                "Upload",                                # Type
                "",                                      # Application
                summary,                                 # Description
                assignee,                                # Implementor
                "10:00:00",                              # Start Time
                t.fields.status.name                     # Status
            ])

        if today_block_row == -1:
            print("Today's block does not exist. Creating styled block with headers...")
            current_row_count = len(all_values)
            sheets.create_styled_today_block(rows_to_add, current_row_count)
        else:
            print(f"Inserting into today's active block at row {insertion_idx}...")
            sheets.insert_rows_at(rows_to_add, insertion_idx)
    else:
        print("✅ Sheet is already up-to-date with Jira tickets.")

    # 3. Analyze with Gemini using the Sheet-Priority Parity Rule
    analysis = gemini.analyze_jira_data(jira_tickets, sheet_statuses)
    if analysis:
        send_to_gchat(analysis)

if __name__ == "__main__":
    main()
