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
    '(worktype = Change OR issuetype = Change) AND assignee IN ("Gaurav Raghav", "Chandan Kumar", "Kamaldeep Singh", "Rahul Dev Jha", 712020:b0badaa4-1a98-499f-9a5c-893ddaa5ccbe) AND "Planned start" >= startOfDay() AND "Planned start" <= endOfDay() ORDER BY "Planned start" ASC'
)

GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "prj-s-ai-bot-4rl8")
GCP_LOCATION = os.getenv("GCP_LOCATION", "us-central1")

# Testing Sheet
GOOGLE_SHEET_FILE_ID = os.getenv("GOOGLE_SHEET_FILE_ID", "1NwJd8uzGIcDTxE0XJ_VYbqJfQnjuqxB4wh5UR97SIlk")
GOOGLE_WORKSHEET_NAME = os.getenv("GOOGLE_WORKSHEET_NAME", "Daily Changes")

WEBHOOK_URL = os.getenv("WEBHOOK_URL")
IST = timezone(timedelta(hours=5, minutes=30))

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
# GOOGLE SHEETS API SERVICE (Using native Google API Client via ADC)
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
            print("✅ Successfully connected to Google Sheets API.")
        except Exception as e:
            print(f"❌ ERROR: Failed to build Sheets API client: {e}", file=sys.stderr)
            sys.exit(1)

    def get_all_values(self):
        """Reads all rows from the worksheet."""
        range_name = f"'{self.sheet_name}'!A:Z"
        result = self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=range_name
        ).execute()
        return result.get('values', [])

    def append_rows(self, rows_data):
        """Appends rows to the worksheet."""
        range_name = f"'{self.sheet_name}'!A:A"
        body = {'values': rows_data}
        self.service.spreadsheets().values().append(
            spreadsheetId=self.spreadsheet_id,
            range=range_name,
            valueInputOption='USER_ENTERED',
            insertDataOption='INSERT_ROWS',
            body=body
        ).execute()
        print(f"📝 Appended {len(rows_data)} rows to Google Sheet.")

    def insert_rows_at(self, rows_data, start_row_index_1_based):
        """
        Inserts rows at a specific line inside the sheet using batchUpdate,
        then populates values.
        """
        # 1. Get the sheetId of the worksheet tab
        sheet_metadata = self.service.spreadsheets().get(spreadsheetId=self.spreadsheet_id).execute()
        sheets = sheet_metadata.get('sheets', '')
        sheet_id_num = None
        for s in sheets:
            if s.get("properties", {}).get("title") == self.sheet_name:
                sheet_id_num = s.get("properties", {}).get("sheetId")
                break
        
        if sheet_id_num is None:
            # Fallback to append if sheetId lookup fails
            self.append_rows(rows_data)
            return

        # 2. Insert blank rows at index (0-based)
        insert_index_0_based = start_row_index_1_based - 1
        requests_body = {
            "requests": [
                {
                    "insertDimension": {
                        "range": {
                            "sheetId": sheet_id_num,
                            "dimension": "ROWS",
                            "startIndex": insert_index_0_based,
                            "endIndex": insert_index_0_based + len(rows_data)
                        },
                        "inheritFromBefore": False
                    }
                }
            ]
        }
        self.service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body=requests_body
        ).execute()

        # 3. Paste the data into the newly created rows
        update_range = f"'{self.sheet_name}'!A{start_row_index_1_based}"
        self.service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=update_range,
            valueInputOption='USER_ENTERED',
            body={'values': rows_data}
        ).execute()
        print(f"📝 Inserted {len(rows_data)} rows at line {start_row_index_1_based}.")

    def find_insertion_point(self, all_values):
        """
        Finds today's block or determines the insertion line.
        """
        today_str = datetime.now(IST).strftime("%d/%m/%Y")
        today_str_alt = datetime.now(IST).strftime("%d/%m/%y")

        today_block_start = -1
        next_block_start = -1

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

        insertion_index = next_block_start if next_block_start != -1 else len(all_values) + 1
        return today_block_start, insertion_index

# ==============================================================================
# VERTEX AI / GEMINI SERVICE
# ==============================================================================
class GeminiService:
    def __init__(self, project_id, location):
        vertexai.init(project=project_id, location=location)
        self.model = GenerativeModel("gemini-2.5-flash")
        print("✅ Vertex AI (Gemini 2.5 Flash) initialized.")

    def analyze_jira_data(self, jira_issues):
        deployments = []
        for issue in jira_issues:
            fields = issue.fields
            comments = [c.body for c in (fields.comment.comments or [])[-3:]]
            assignee = fields.assignee.displayName if fields.assignee else "Unassigned"
            deployments.append({
                "ticket_key": issue.key,
                "summary": fields.summary,
                "status": fields.status.name,
                "assignee": assignee,
                "description": fields.description or "",
                "recent_comments": comments
            })

        prompt = f"""
        Analyze this raw deployment data fetched directly from Jira.
        Data:
        {json.dumps(deployments, indent=2)}

        You are an intelligent Release Manager. Do the following:
        1. Write a 1-sentence "executive_summary" summarizing today's overall deployment health.
        2. For "comments", analyze description and recent_comments for issues/blockers. If none, leave "".
        3. Create a "short_desc" (MAX 3 to 5 words, strictly under 20 characters).
        4. Deduce the "application" and "environment".
        5. For "status", output the exact current status string.
        6. For "status_emoji", use ✅ (Done/Completed), ⚙️ (In Progress/Started), ⏳ (Pending/To Do), 🚧 (Backed Out/Failed/Rescheduled).

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
        status = dep.get('status', 'Unknown')
        c = dep.get("comments", "").strip()
        is_done = "done" in status.lower() or "completed" in status.lower() or dep.get("status_emoji") == "✅"
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
# MAIN ENGINE
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

    # 1. Read sheet to inspect existing entries
    all_values = sheets.get_all_values()
    existing_keys = set()
    for row in all_values:
        for cell in row:
            for issue in jira_tickets:
                if issue.key in str(cell):
                    existing_keys.add(issue.key)

    new_tickets = [t for t in jira_tickets if t.key not in existing_keys]
    print(f"[De-duplication] {len(existing_keys)} tickets already in sheet. {len(new_tickets)} new ticket(s) to add.")

    # 2. Append/Insert new tickets if any
    if new_tickets:
        today_block_row, insertion_idx = sheets.find_insertion_point(all_values)
        rows_to_add = []
        for t in new_tickets:
            summary = t.fields.summary
            env = "PROD" if "prod" in summary.lower() else "UAT"
            assignee = t.fields.assignee.displayName if t.fields.assignee else "Unassigned"
            # Format row matching your Daily Changes sheet columns
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
            print("Today's block doesn't exist yet. Creating new date header block...")
            header_block = [
                [],
                [datetime.now(IST).strftime("%d/%m/%Y")],
                ['Date', 'CR', 'Env', 'Type', 'Application', 'Description', 'Implementor', 'Start Time', 'Status']
            ] + rows_to_add
            sheets.append_rows(header_block)
        else:
            print(f"Inserting into today's active block at row {insertion_idx}...")
            sheets.insert_rows_at(rows_to_add, insertion_idx)
    else:
        print("✅ Sheet is already up-to-date with Jira tickets.")

    # 3. Analyze with Gemini & send live radar broadcast to Chat
    analysis = gemini.analyze_jira_data(jira_tickets)
    if analysis:
        send_to_gchat(analysis)

if __name__ == "__main__":
    main()
