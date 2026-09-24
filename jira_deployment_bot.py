import os
import sys
import json
from datetime import datetime, timedelta, timezone
import gspread
import requests
from jira import JIRA
from oauth2client.service_account import ServiceAccountCredentials
import vertexai
from vertexai.generative_models import GenerativeModel

# --- CONFIGURATION ---
JIRA_SERVER = os.getenv("JIRA_SERVER", "https://osttra.atlassian.net")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
JQL_QUERY = os.getenv("JQL_QUERY", 'worktype = Change AND assignee IN ("Gaurav Raghav", "Chandan Kumar", "Himanshu Mittal", "Kamaldeep Singh", "Rahul Dev Jha") AND "Planned start" >= startOfDay() AND "Planned start" <= endOfDay()')

GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "prj-s-ai-bot-4rl8")
GCP_LOCATION = os.getenv("GCP_LOCATION", "us-central1")
GOOGLE_SHEET_FILE_ID = os.getenv("GOOGLE_SHEET_FILE_ID", "1qITm4Ny1Kf_JZoKsrCLpbDHlbmET49OaqN1aNsI7JHo")
GOOGLE_WORKSHEET_NAME = os.getenv("GOOGLE_WORKSHEET_NAME", "Daily Changes")
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "./gcp_keys.json")

GOOGLE_CHAT_WEBHOOK_URL = os.getenv("WEBHOOK_URL")

IST = timezone(timedelta(hours=5, minutes=30))

class JiraService:
    def __init__(self, server, email, token):
        self.client = None
        if not email or not token:
            print("❌ ERROR: JIRA_EMAIL or JIRA_API_TOKEN is not set.", file=sys.stderr)
            sys.exit(1)
        try:
            self.client = JIRA(server=server, basic_auth=(email, token))
            print("✅ Successfully connected to Jira.")
        except Exception as e:
            print(f"❌ ERROR: Could not connect to Jira. {e}", file=sys.stderr)
            sys.exit(1)

    def get_planned_changes(self):
        if not self.client: return []
        try:
            print(f"🔍 Querying Jira...")
            issues = self.client.search_issues(JQL_QUERY, maxResults=50, fields=['summary', 'status', 'assignee', 'description', 'comment'])
            print(f" Jira found {len(issues)} planned ticket(s).")
            return issues
        except Exception as e:
            print(f"❌ ERROR: Failed to query Jira. {e}", file=sys.stderr)
            return []

class GoogleSheetService:
    def __init__(self, credentials_path, sheet_id, worksheet_name):
        self.worksheet = None
        try:
            scope = ["https://spreadsheets.google.com/feeds", 'https://www.googleapis.com/auth/spreadsheets', "https://www.googleapis.com/auth/drive.file", "https://www.googleapis.com/auth/drive"]
            creds = ServiceAccountCredentials.from_json_keyfile_name(credentials_path, scope)
            gc = gspread.authorize(creds)
            self.worksheet = gc.open_by_key(sheet_id).worksheet(worksheet_name)
            print("✅ Successfully connected to Google Sheets.")
        except Exception as e:
            print(f"❌ ERROR: Could not connect to Google Sheets. {e}", file=sys.stderr)
            sys.exit(1)

    def get_sheet_data_and_find_insertion_point(self):
        all_values = self.worksheet.get_all_values()
        today_str = datetime.now(IST).strftime("%d/%m/%Y")
        today_block_start_row, next_block_start_row = -1, -1
        for i, row in enumerate(all_values):
            if row and row[0] and today_str in row[0]:
                if today_block_start_row == -1: today_block_start_row = i + 1
            elif today_block_start_row != -1 and row and row[0] and ('/' in row[0] or '-' in row[0]):
                next_block_start_row = i + 1; break
        insertion_index = next_block_start_row if next_block_start_row != -1 else len(all_values) + 1
        return all_values, today_block_start_row, insertion_index

    def create_today_block_and_insert(self, rows_to_add):
        print("No block for today found. Creating a new one...")
        self.worksheet.append_row([])
        self.worksheet.append_row([datetime.now(IST).strftime("%d/%m/%Y")])
        self.worksheet.append_row(['Date', 'CR', 'Env', 'Application', 'Description', 'Implementer', 'Start Time', 'Status'])
        self.worksheet.append_rows(rows_to_add)

    def insert_rows_at_index(self, rows_to_add, index):
        self.worksheet.insert_rows(rows_to_add, row=index)

class GeminiService:
    def __init__(self, project_id, location):
        vertexai.init(project=project_id, location=location)
        self.model = GenerativeModel("gemini-1.5-flash-001")
        print("✅ Vertex AI (Gemini) service initialized.")

    def analyze_jira_data(self, jira_issues):
        # This function remains the same as provided before
        # ... (pasting it here for completeness)
        deployments_for_prompt = []
        for issue in jira_issues:
            recent_comments = [c.body for c in (issue.fields.comment.comments or [])[-3:]]
            deployments_for_prompt.append({
                "ticket_key": issue.key, "summary": issue.fields.summary, "status": issue.fields.status.name,
                "assignee": issue.fields.assignee.displayName if issue.fields.assignee else 'Unassigned',
                "description": issue.fields.description, "recent_comments": recent_comments
            })

        prompt = f"""
        Analyze this raw deployment data from Jira. Raw JSON Data: {json.dumps(deployments_for_prompt, indent=2)}
        You are an intelligent Release Manager. Perform these tasks:
        1. Write a 1-sentence "executive_summary" of today's deployment health.
        2. For each ticket, analyze the description and recent_comments to create a "comments" field summarizing any blockers or notable updates. If none, leave it as an empty string.
        3. Create a "short_desc" (MAX 3-5 words).
        4. Deduce the "application" and "environment".
        5. For "status", use the exact Jira status.
        6. For "status_emoji", use ✅ (Done/Completed), ⚙️ (In Progress/Started), ⏳ (Pending/To Do), 🚧 (Backed Out/Failed/Cancelled).
        
        OUTPUT FORMAT REQUIREMENTS: Return ONLY a valid JSON object. Do not use markdown.
        {{
        "executive_summary": "...",
        "deployments": [
            {{
            "change_no": "...", "application": "...", "short_desc": "...", "environment": "...",
            "implementer": "...", "status": "...", "status_emoji": "...", "comments": "..."
            }}
        ]
        }}
        """
        try:
            response = self.model.generate_content(prompt)
            cleaned_text = response.text.strip().replace("```json", "").replace("```", "")
            return json.loads(cleaned_text)
        except Exception as e:
            print(f"❌ ERROR: Failed to get or parse Gemini response. {e}", file=sys.stderr)
            return None

def send_to_gchat(card_data, is_first_update=True, is_wrap_up=False):
    # This function is from your original jira_deployment_bot.py. 
    # It's better to keep it in the same file to avoid import issues.
    # ... (code for send_to_gchat from your script)
    if not card_data or "deployments" not in card_data: return False
    print("\nSending formatted message to Chat...")
    summary = card_data.get("executive_summary", "")
    deployments = card_data["deployments"]
    headers = ["Change No", "App", "Desc", "Env", "St's", "Owner"]
    rows = []
    for dep in deployments:
        change_no = str(dep.get('change_no', 'N/A'))[:15]
        app = str(dep.get('application', 'N/A'))[:8]
        desc = str(dep.get('short_desc', 'N/A'))[:20]
        env = str(dep.get('environment', 'N/A'))[:6]
        status_emoji = dep.get('status_emoji', '⏳')
        owner_full = str(dep.get('implementer', 'N/A'))
        owner = owner_full.split(" ")[0][:10] if " " in owner_full else owner_full[:10]
        rows.append([change_no, app, desc, env, status_emoji, owner])
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            display_length = len(cell) if not any(emoji in cell for emoji in ['✅','⚙️','⏳','🚧']) else len(cell) - 1
            col_widths[i] = max(col_widths[i], display_length)
    header_line = "| " + " | ".join(h.ljust(w) for h, w in zip(headers, col_widths)) + " |"
    separator_line = "|" + "|".join("-" * (w + 2) for w in col_widths) + "|"
    table_lines = [header_line, separator_line]
    for row in rows:
        row_line = "|"
        for i, cell in enumerate(row):
            if i == 4: row_line += f" " + (" " * (col_widths[i] - 1)) + f"{cell}|"
            else: row_line += f" {cell.ljust(col_widths[i])} |"
        table_lines.append(row_line)
    table_content = "\n".join(table_lines)
    comments_section = ""
    for dep in deployments:
        key, status, c = dep.get('change_no', 'N/A'), dep.get('status', 'Unknown'), dep.get("comments", "").strip()
        is_done = ("done" in status.lower() or "completed" in status.lower() or dep.get("status_emoji") == "✅")
        if not is_done and c and c.lower() not in ["none", "-", "n/a", "null", ""]:
            jira_link = f"<{JIRA_SERVER}/browse/{key}|{key}>"
            comments_section += f"• **{jira_link}**: {c}\n"
    today_date = datetime.now(IST).strftime('%d/%m/%Y')
    if is_wrap_up: message_text = f"*🎉 Wrap-up Update - Planned Changes for Today ({today_date}) (JIRA Radar)*\n\n"
    elif is_first_update: message_text = f"*Hi All, Planned Changes for Today ({today_date}) (JIRA Radar)*\n\n"
    else: message_text = f"*Hi All, Update - Planned Changes for Today ({today_date}) (JIRA Radar)*\n\n"
    if summary: message_text += f"🤖 *AI Summary:* {summary}\n\n"
    message_text += f"```\n{table_content}\n```\n*(Legend: ✅ Done | ⚙️ In Progress | ⏳ Pending | 🚧 Backed Out/Failed/Rescheduled)*\n"
    if comments_section: message_text += f"\n*⚡ Live Radar: Actionable Insights & Friction Points*\n{comments_section}"
    payload = {"text": message_text}
    response = requests.post(GOOGLE_CHAT_WEBHOOK_URL, headers={"Content-Type": "application/json"}, data=json.dumps(payload))
    if response.status_code == 200:
        print("✅ TABLE message sent successfully to Chat Space!"); return True
    else:
        print(f"❌ Failed to send message: {response.status_code}, {response.text}"); return False


def main():
    print("--- Starting Integrated ReleaseRadar Bot ---")
    jira = JiraService(JIRA_SERVER, JIRA_EMAIL, JIRA_API_TOKEN)
    sheets = GoogleSheetService(GOOGLE_CREDENTIALS_PATH, GOOGLE_SHEET_FILE_ID, GOOGLE_WORKSHEET_NAME)
    gemini = GeminiService(GCP_PROJECT_ID, GCP_LOCATION)
    jira_tickets = jira.get_planned_changes()
    if not jira_tickets: print("No planned changes in Jira. Exiting."); return
    all_sheet_values, today_block_row, insertion_index = sheets.get_sheet_data_and_find_insertion_point()
    existing_keys = {row[1].strip() for row in all_sheet_values if len(row) > 1 and row[1]}
    new_tickets = [t for t in jira_tickets if t.key not in existing_keys]
    if new_tickets:
        rows_to_add = []
        for ticket in new_tickets:
            rows_to_add.append([
                datetime.now(IST).strftime("%d/%m/%Y"), ticket.key,
                'PROD' if 'prod' in ticket.fields.summary.lower() else 'UAT', '',
                ticket.fields.summary, ticket.fields.assignee.displayName if ticket.fields.assignee else 'Unassigned',
                '', ticket.fields.status.name
            ])
        if today_block_row == -1: sheets.create_today_block_and_insert(rows_to_add)
        else: print(f"Inserting {len(rows_to_add)} new row(s)..."); sheets.insert_rows_at_index(rows_to_add, insertion_index)
    else: print("✅ Sheet is already up-to-date.")
    gemini_analysis = gemini.analyze_jira_data(jira_tickets)
    if gemini_analysis and GOOGLE_CHAT_WEBHOOK_URL: send_to_gchat(gemini_analysis)
    else: print("Skipping Chat notification.")

if __name__ == "__main__":
    main()
