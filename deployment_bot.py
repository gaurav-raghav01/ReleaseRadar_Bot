import os
import sys
import json
import csv
from io import StringIO
from datetime import datetime, timedelta, timezone
import google.auth
from googleapiclient.discovery import build
import vertexai
from vertexai.generative_models import GenerativeModel
import requests

# --- CONFIGURATION ---
PROJECT_ID = "prj-c-platformtools-fn8z" 
LOCATION = "us-central1"  
IST = timezone(timedelta(hours=5, minutes=30))

# Load Webhook from environment variable for security
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

SHEET_FILE_ID = "1qITm4Ny1Kf_JZoKsrCLpbDHlbmET49OaqN1aNsI7JHo" # Daily Sheet ID
STATE_FILE = "last_run_state.txt" # if the final wrap-up was sent
FIRST_RUN_FILE = "first_run_state.txt" # if the first update was sent today

def get_today_deployments():
    print("Authenticating via ADC and fetching Sheet data...")
    scopes = [
        'https://www.googleapis.com/auth/drive.readonly',
        'https://www.googleapis.com/auth/cloud-platform'
    ]
    credentials, _ = google.auth.default(scopes=scopes)
    
    try:
        drive_service = build('drive', 'v3', credentials=credentials)
    except Exception as e:
        print(f"\n❌ CRITICAL ERROR: Failed to authenticate with Drive API. {e}")
        sys.exit(1)

    # STRICTLY USE IST FOR GH ACTIONS COMPATIBILITY
    today = datetime.now(IST) 
    is_weekend = today.weekday() >= 5 # 5 = Saturday, 6 = Sunday

    target_file_id = SHEET_FILE_ID #daily sheet

    if is_weekend:
        if today.weekday() == 5: #saturday
            sat_date = today
            sun_date = today + timedelta(days=1)
        else: # Today is Sunday
            sat_date = today - timedelta(days=1)
            sun_date = today

        weekend_filename = f"{sat_date.strftime('%d-%m-%Y')} & {sun_date.strftime('%d-%m-%Y')}"
        print(f"It's the weekend! Searching Drive for file: {weekend_filename}")

        query = f"name contains '{weekend_filename}' and trashed = false"
        results = drive_service.files().list(q=query, fields="files(id, name)").execute()
        items = results.get('files', [])

        if not items:
            print(f"❌ Could not find a file named '{weekend_filename}' in your Drive. Exiting.")
            return []
        
        target_file_id = items[0]['id']
        print(f"✅ Found weekend file: {items[0]['name']}")
    else:
        print("Fetching standard Daily Changes sheet...")

    #download file
    try:
        request = drive_service.files().export_media(fileId=target_file_id, mimeType='text/csv')
        response = request.execute()
    except Exception as e:
        print(f"\n❌ CRITICAL ERROR: Failed to download sheet via Drive API. Details: {e}")
        sys.exit(1)

    csv_data = StringIO(response.decode('utf-8'))
    reader = csv.reader(csv_data)
    values = list(reader)

    if not values:
        return []

    today_deployments = []

    #parse the data csv
    if is_weekend:
        today_day_name = today.strftime("%A") 
        current_day_block = None

        for row in values:
            clean_row = [str(item).strip() for item in row if str(item).strip() != ""]
            if not clean_row or clean_row[0] in ["Change", "Date"]: 
                continue

            if len(clean_row) == 1 and clean_row[0] in ["Saturday", "Sunday"]:
                current_day_block = clean_row[0]
                continue
            
            if current_day_block == today_day_name:
                today_deployments.append(clean_row)
    else:
        possible_dates = [
            today.strftime("%d/%m/%Y"), today.strftime("%d/%m/%y"), today.strftime("%m/%d/%Y"),
            today.strftime("%Y-%m-%d"), f"{today.day}/{today.month}/{today.year}", today.strftime("%d-%b-%Y")
        ]
        
        for row in values:
            clean_row = [str(item).strip() for item in row if str(item).strip() != ""]
            is_today = any(date_str in cell for cell in clean_row for date_str in possible_dates)
            if is_today:
                today_deployments.append(clean_row)
            
    return today_deployments


def are_all_tasks_completed(deployments):
    if not deployments:
        return True 
    active_statuses = ['pending', 'in progress', 'scheduled', 'started']
    for row in deployments:
        for cell in row:
            if str(cell).lower() in active_statuses:
                return False 
    return True 


def generate_data_with_gemini(deployments):
    print("Extracting data and analyzing updates with Gemini (Vertex AI)...")
    
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    model = GenerativeModel("gemini-2.5-flash") 
    
    data_str = ""
    for row in deployments:
        data_str += " | ".join(row) + "\n"

    prompt = f"""
    Analyze this raw deployment data.
    Raw Data:
    {data_str}
    
    You are an intelligent Release Manager. Do the following:
    1. Extract the basic details for each deployment.
    2. Write a 1-sentence "executive_summary" summarizing today's overall deployment health.
    3. For "comments", ONLY extract explicit issues, blockers, or actual notes from the raw data. If there is no real comment, leave it as an empty string "". DO NOT make up generic comments.
    4. Create a "short_desc", which must be a condensed summary of the task description (MAXIMUM 3 to 5 words, strictly under 20 characters).
    
    OUTPUT FORMAT REQUIREMENTS:
    Return strictly a valid JSON object. Do NOT wrap it in ```json markdown blocks.
    The JSON must match this exact structure:
    {{
        "executive_summary": "A 1-sentence AI summary of today's deployments",
        "deployments": [
        {{
            "change_no": "TRMPLATFORM-9498",
            "application": "SocGen",
            "short_desc": "Release 1.17.0",
            "environment": "DR",
            "implementer": "Kamaldeep",
            "status": "In Progress",
            "status_emoji": "⚙️",
            "comments": "Dress Deployment got successfully..."
        }}
        ]
    }}
    Note: For status_emoji use ✅ (Done), ⚙️ (In Progress), ⏳ (Pending), 🚧 (Rescheduled/Failed/Cancelled).
    """

    response = model.generate_content(prompt)
    response_text = response.text.strip()
    
    if response_text.startswith("```json"): response_text = response_text[7:]
    if response_text.startswith("```"): response_text = response_text[3:]
    if response_text.endswith("```"): response_text = response_text[:-3]
        
    try:
        return json.loads(response_text.strip())
    except json.JSONDecodeError as e:
        print("❌ Failed to parse Gemini output into JSON.")
        print(response_text)
        return None


def send_to_gchat(card_data, is_first_update=True, is_wrap_up=False):
    if not card_data or "deployments" not in card_data:
        return False
    if not WEBHOOK_URL:
        print("❌ WEBHOOK_URL is not set!")
        return False

    print("\nSending formatted table message to Chat...")
    
    summary = card_data.get("executive_summary", "")
    deployments = card_data["deployments"]

    headers = ["Change No", "App", "Desc", "Env", "St's", "Owner"]
    rows = []
    
    for dep in deployments:
        change_no = str(dep.get('change_no', 'N/A'))[:13] 
        app = str(dep.get('application', 'N/A'))[:8]    
        desc = str(dep.get('short_desc', 'N/A'))[:20]   
        env = str(dep.get('environment', 'N/A'))[:6]    
        status_emoji = dep.get('status_emoji', '⏳')
        owner = str(dep.get('implementer', 'N/A'))[:10]  
        
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
            if i == 4:
                row_line += f" {cell}" + (" " * (col_widths[i] - 1)) + "|"
            else:
                row_line += f" {cell.ljust(col_widths[i])} |"
        table_lines.append(row_line)

    table_content = "\n".join(table_lines)

    comments_section = ""
    for dep in deployments:
        c = dep.get("comments", "").strip()
        status = dep.get("status", "").strip().lower()
        status_emoji = dep.get("status_emoji", "")
        
        is_done = ("done" in status or "completed" in status or status_emoji == "✅")
        
        if not is_done:
            if c and c.lower() not in ["none", "-", "n/a", "null", ""]:
                comments_section += f"• *{dep.get('change_no', 'Task')}*: {c}\n"

    today_date = datetime.now(IST).strftime('%d/%m/%Y')
    
    if is_wrap_up:
        message_text = f"*🎉 Wrap-up Update - Planned Changes for Today ({today_date})*\n\n"
    elif is_first_update:
        message_text = f"*Hi All, Planned Changes for Today ({today_date})*\n\n"
        if summary:
            message_text += f"🤖 *AI Summary:* {summary}\n\n"
    else:
        message_text = f"*Hi All, Update - Planned Changes for Today ({today_date})*\n\n"
        
    message_text += f"```\n{table_content}\n```\n"
    message_text += "*(Legend: ✅ Done | ⚙️ In Progress | ⏳ Pending | 🚧 Rescheduled/Failed)*\n"

    if comments_section:
        message_text += f"\n*💬 Comments & Updates (Active Tasks):*\n{comments_section}"

    payload = {"text": message_text}
    headers_req = {"Content-Type": "application/json"}
    
    response = requests.post(WEBHOOK_URL, headers=headers_req, data=json.dumps(payload))
    if response.status_code == 200:
        print("✅ TABLE message sent successfully to Chat Space!")
        return True
    else:
        print(f"❌ Failed to send message: {response.status_code}, {response.text}")
        return False


def main():
    try:
        today_deployments = get_today_deployments()
        
        if not today_deployments:
            print("No deployments found for today. Exiting.")
            sys.exit(0)

        now_ist = datetime.now(IST)
        is_finished = are_all_tasks_completed(today_deployments)
        is_deadline_reached = now_ist.hour >= 21

        today_str = now_ist.strftime('%Y-%m-%d')

        # Create files if they do not exist
        if not os.path.exists(FIRST_RUN_FILE):
            open(FIRST_RUN_FILE, 'w').close()
        if not os.path.exists(STATE_FILE):
            open(STATE_FILE, 'w').close()

        is_first_run = True
        with open(FIRST_RUN_FILE, "r") as f:
            if f.read().strip() == today_str:
                is_first_run = False

        if is_finished or is_deadline_reached:
            with open(STATE_FILE, "r") as f:
                if f.read().strip() == today_str:
                    print("✅ Wrap-up update was already completed today. Exiting.")
                    sys.exit(0)
            
            if is_deadline_reached and not is_finished:
                print("🕒 23:00 IST reached with pending tasks. Forcing final wrap-up summary...")
            else:
                print("🎉 All deployments completed! Sending final wrap-up summary...")
                
            card_data = generate_data_with_gemini(today_deployments)
            if card_data:
                success = send_to_gchat(card_data, is_first_update=is_first_run, is_wrap_up=True)
                
                if success:
                    with open(STATE_FILE, "w") as f:
                        f.write(today_str)
                    if is_first_run:
                        with open(FIRST_RUN_FILE, "w") as f:
                            f.write(today_str)
                            
        else:
            with open(STATE_FILE, "w") as f:
                f.write("") # Clear out the final state so it knows it isn't wrapped up yet

            card_data = generate_data_with_gemini(today_deployments)
            if card_data:
                success = send_to_gchat(card_data, is_first_update=is_first_run, is_wrap_up=False)
                
                if success and is_first_run:
                    with open(FIRST_RUN_FILE, "w") as f:
                        f.write(today_str)
        
    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    main()