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
# These will be set by the GitHub Actions secrets and environment variables
JIRA_SERVER = os.getenv("JIRA_SERVER", "https://osttra.atlassian.net")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
JQL_QUERY = os.getenv("JQL_QUERY", 'worktype = Change AND assignee IN ("Gaurav Raghav", "Chandan Kumar", "Himanshu Mittal", "Kamaldeep Singh", "Rahul Dev Jha") AND "Planned start" >= startOfDay() AND "Planned start" <= endOfDay()')

# Google Cloud & Sheets
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "prj-s-ai-bot-4rl8")
GCP_LOCATION = os.getenv("GCP_LOCATION", "us-central1")
GOOGLE_SHEET_FILE_ID = os.getenv("GOOGLE_SHEET_FILE_ID", "1NwJd8uzGIcDTxE0XJ_VYbqJfQnjuqxB4wh5UR97SIlk") # Daily Sheet ID
GOOGLE_WORKSHEET_NAME = os.getenv("GOOGLE_WORKSHEET_NAME", "Daily Changes") # Your tab name
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "./gcp_keys.json")

# Google Chat
GOOGLE_CHAT_WEBHOOK_URL = os.getenv("WEBHOOK_URL")

IST = timezone(timedelta(hours=5, minutes=30))

class JiraService:
    def __init__(self, server, email, token):
        self.client = None
        try:
            self.client = JIRA(server=server, basic_auth=(email, token))
            print("✅ Successfully connected to Jira.")
        except Exception as e:
            print(f"❌ ERROR: Could not connect to Jira. {e}", file=sys.stderr)
            sys.exit(1)

    def get_planned_changes(self):
        if not self.client: return []
        try:
            print(f"🔍 Querying Jira for today's planned changes...")
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
            print(f"❌ ERROR: Could not connect to Google Sheets. Check permissions and names. {e}", file=sys.stderr)
            sys.exit(1)

    def get_sheet_data_and_find_insertion_point(self):
        all_values = self.worksheet.get_all_values()
        today_str_long = datetime.now(IST).strftime("%d/%m/%Y")
        today_str_short = datetime.now(IST).strftime("%d/%m/%y")

        today_block_start_row = -1
        next_block_start_row = -1

        for i, row in enumerate(all_values):
            if not row or not row[0]: continue
            cell_content = row[0].strip()
            
            # Find the start of today's block
            if today_str_long in cell_content or today_str_short in cell_content:
                if today_block_start_row == -1: # Found the first occurrence
                    today_block_start_row = i + 1
            # Find the start of the next block
            elif today_block_start_row != -1 and cell_content and ('/' in cell_content or '-' in cell_content):
                 next_block_start_row = i + 1
                 break
        
        insertion_index = next_block_start_row if next_block_start_row != -1 else len(all_values) + 1
        return all_values, today_block_start_row, insertion_index

    def create_today_block_and_insert(self, rows_to_add):
        print(f"No block found for today. Creating a new one...")
        today_header = [datetime.now(IST).strftime("%d/%m/%Y")]
        column_headers = ['Date', 'CR', 'Env', 'Application', 'Description', 'Implementer', 'Start Time', 'Status']
        
        self.worksheet.append_row([]) # Spacer row
        self.worksheet.append_row(today_header)
        self.worksheet.append_row(column_headers)
        self.worksheet.append_rows(rows_to_add)

    def insert_rows_at_index(self, rows_to_add, index):
        self.worksheet.insert_rows(rows_to_add, row=index)

class GeminiService:
    def __init__(self, project_id, location):
        vertexai.init(project=project_id, location=location)
        self.model = GenerativeModel("gemini-1.5-flash-001") # Using Gemini 1.5 Flash
        print("✅ Vertex AI (Gemini) service initialized.")

    def analyze_jira_data(self, jira_issues):
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
            # Clean the response text to ensure it's valid JSON
            cleaned_text = response.text.strip().replace("```json", "").replace("```", "")
            return json.loads(cleaned_text)
        except Exception as e:
            print(f"❌ ERROR: Failed to get or parse Gemini response. {e}", file=sys.stderr)
            return None

def main():
    print("--- Starting Integrated ReleaseRadar Bot ---")
    
    # 1. Initialize Services
    jira = JiraService(JIRA_SERVER, JIRA_EMAIL, JIRA_API_TOKEN)
    sheets = GoogleSheetService(GOOGLE_CREDENTIALS_PATH, GOOGLE_SHEET_FILE_ID, GOOGLE_WORKSHEET_NAME)
    gemini = GeminiService(GCP_PROJECT_ID, GCP_LOCATION)
    
    # 2. Get tickets from Jira
    jira_tickets = jira.get_planned_changes()
    if not jira_tickets:
        print("No planned changes found in Jira for today. Exiting.")
        return

    # 3. Find where to insert data in the Google Sheet
    all_sheet_values, today_block_row, insertion_index = sheets.get_sheet_data_and_find_insertion_point()
    
    # 4. De-duplicate: Find tickets from Jira that are NOT already in the sheet
    existing_keys = {row[1].strip() for row in all_sheet_values if len(row) > 1 and row[1]}
    new_tickets = [t for t in jira_tickets if t.key not in existing_keys]

    # 5. Prepare and insert/append new rows if any
    rows_to_add = []
    if new_tickets:
        for ticket in new_tickets:
            # This is the data that gets written to the sheet
            row_data = [
                datetime.now(IST).strftime("%d/%m/%Y"), ticket.key,
                'PROD' if 'prod' in ticket.fields.summary.lower() else 'UAT',
                '', # Application
                ticket.fields.summary,
                ticket.fields.assignee.displayName if ticket.fields.assignee else 'Unassigned',
                '', # Start Time
                ticket.fields.status.name
            ]
            rows_to_add.append(row_data)

        if today_block_row == -1:
            sheets.create_today_block_and_insert(rows_to_add)
        else:
            print(f"Inserting {len(rows_to_add)} new row(s) at sheet row {insertion_index}...")
            sheets.insert_rows_at_index(rows_to_add, insertion_index)
    else:
        print("✅ Google Sheet is already up-to-date with Jira tickets.")

    # 6. Use Gemini to analyze the complete list of today's tickets for chat reporting
    gemini_analysis = gemini.analyze_jira_data(jira_tickets)

    # 7. Send the update to Google Chat
    if gemini_analysis and GOOGLE_CHAT_WEBHOOK_URL:
        # Use the logic from your original jira_deployment_bot to send chat
        from jira_deployment_bot import send_to_gchat 
        send_to_gchat(gemini_analysis, is_first_update=True, is_wrap_up=False)
    else:
        print("Skipping Google Chat notification (no data from Gemini or webhook URL not set).")

if __name__ == "__main__":
    main()

