import os
import sys
import json
from datetime import datetime, timedelta, timezone
import requests
from requests.auth import HTTPBasicAuth

import vertexai
from vertexai.generative_models import GenerativeModel

# --- CONFIGURATION ---
PROJECT_ID = "prj-c-platformtools-fn8z" 
LOCATION = "us-central1"  
IST = timezone(timedelta(hours=5, minutes=30))

# SECURE CREDENTIALS via GitHub Actions Environment Variables
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
JIRA_URL = os.environ.get("JIRA_URL", "https://osttra.atlassian.net") 
JIRA_EMAIL = os.environ.get("JIRA_EMAIL")  
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN")

# STATE FILES
STATE_FILE = "jira_last_run_state.txt" 
FIRST_RUN_FILE = "jira_first_run_state.txt" 

def get_today_deployments():
    """Fetches today's planned Change tickets assigned to the team using Jira REST API v3."""
    print("Authenticating and fetching tickets from Jira...")
    
    if not JIRA_API_TOKEN or not JIRA_EMAIL:
        print("❌ CRITICAL ERROR: Jira credentials not found in environment variables!")
        sys.exit(1)
        
    jql_query = """
    worktype = Change 
    AND assignee IN ("Gaurav Raghav", "Chandan Kumar", "Himanshu Mittal", "Kamaldeep Singh", "Rahul Dev Jha")
    AND "Planned start" >= startOfDay() AND "Planned start" <= endOfDay()
    """
    
    # FIXED: Added JIRA_URL variable to the path
    url = f"{JIRA_URL}/rest/api/3/search/jql" 
    auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_API_TOKEN)
    
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    
    payload = {
        'jql': jql_query,
        'maxResults': 50,
        'fields': ['summary', 'status', 'assignee', 'description', 'environment', 'comment'] 
    }
    
    response = None
    try:
        response = requests.post(url, headers=headers, json=payload, auth=auth)
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as e:
        # FIXED: Added exception details to the print statement
        print(f"\n❌ CRITICAL ERROR: Failed to fetch data from Jira. Details: {e}")
        if response is not None:
            print(response.text)
        sys.exit(1)

    issues = data.get('issues', [])
    if not issues:
        return []

    today_deployments = []
    for issue in issues:
        fields = issue.get('fields', {})
        assignee_dict = fields.get('assignee') or {}
        status_dict = fields.get('status') or {}
        
        # Extract the latest comments to give Gemini context on current blockers/status
        comment_data = fields.get('comment', {})
        comments_list = comment_data.get('comments', [])
        recent_comments = [c.get('body') for c in comments_list[-3:]]
        
        dep_info = {
            "ticket_key": issue.get('key'),
            "summary": fields.get('summary'),
            "status": status_dict.get('name', 'Unknown'),
            "assignee": assignee_dict.get('displayName', 'Unassigned'),
            "description": fields.get('description', ''),
            "recent_comments": recent_comments 
        }
        today_deployments.append(dep_info)
        
    return today_deployments

def are_all_tasks_completed(deployments):
    """Checks if there are active tasks remaining based on Jira statuses."""
    if not deployments:
        return True 
        
    completed_statuses = ['done', 'completed', 'implemented successfully', 'closed', 'resolved', 'cancelled']
    
    for dep in deployments:
        status = str(dep.get("status", "")).lower()
        if status not in completed_statuses:
            return False 
    return True 

def generate_data_with_gemini(deployments):
    """Uses Gemini to extract structured JSON and perform AI Analysis from Jira data."""
    print("Extracting data and analyzing updates with Gemini (Vertex AI)...")
    
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    model = GenerativeModel("gemini-2.5-flash") 
    
    prompt = f"""
    Analyze this raw deployment data fetched directly from our Jira system.
    Raw Jira JSON Data:
    {json.dumps(deployments, indent=2)}
    
    You are an intelligent Release Manager. Do the following:
    1. Extract the basic details for each deployment using the Jira summary, description, and status.
    2. Write a 1-sentence "executive_summary" summarizing today's overall deployment health.
    3. For "comments", analyze BOTH the `description` AND the `recent_comments`. Summarize any specific issues, blockers, failure reasons, or rollback notes. If there is nothing notable, leave it as "". DO NOT make up generic comments.
    4. Create a "short_desc", which must be a condensed summary of the task (MAXIMUM 3 to 5 words, strictly under 20 characters).
    5. Deduce the "application" and "environment" dynamically.
    6. For "status", strictly output the EXACT state the ticket is currently in based on the raw JSON (e.g., "In Progress", "Backed Out", "Completed", "Pending", "Cancelled").
    
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
    "comments": "Rollback steps verified. Blocked by DB team..."
    }}
    ]
    }}
    Note: For status_emoji use ✅ (Done/Completed), ⚙️ (In Progress/Started), ⏳ (Pending/To Do), 🚧 (Rescheduled/Failed/Backed Out/Cancelled).
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
    """Builds an ASCII table for Google Chat."""
    if not card_data or "deployments" not in card_data:
        return False

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
        is_done = ("done" in status.lower() or "completed" in status.lower() or dep.get("status_emoji") == "✅")
        
        # Only show comments/blockers for incomplete tasks
        if not is_done and c and c.lower() not in ["none", "-", "n/a", "null", ""]:
            # FIXED: Properly formatting Jira hyperlinking and comment injection
            jira_link = f"<{JIRA_URL}/browse/{key}|{key}>"
            comments_section += f"• **{jira_link}**: {c}\n"

    # FIXED TIMEZONE HERE
    today_date = datetime.now(IST).strftime('%d/%m/%Y')
    
    # FIXED: Added today_date variable to headers
    if is_wrap_up:
        message_text = f"*🎉 Wrap-up Update - Planned Changes for Today ({today_date}) (JIRA Radar)*\n\n"
    elif is_first_update:
        message_text = f"*Hi All, Planned Changes for Today ({today_date}) (JIRA Radar)*\n\n"
    else:
        message_text = f"*Hi All, Update - Planned Changes for Today ({today_date}) (JIRA Radar)*\n\n"
        
    if summary:
        message_text += f"🤖 *AI Summary:* {summary}\n\n"
        
    # FIXED: Added table_content to the code block
    message_text += f"```\n{table_content}\n```\n"
    message_text += "*(Legend: ✅ Done | ⚙️ In Progress | ⏳ Pending | 🚧 Backed Out/Failed/Rescheduled)*\n"

    # FIXED: Now injects the 'comments_section' string!
    if comments_section:
        message_text += f"\n*⚡ Live Radar: Actionable Insights & Friction Points*\n{comments_section}"

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
            print("No deployments found for today in Jira. Exiting.")
            sys.exit(0)

        now_ist = datetime.now(IST)
        is_finished = are_all_tasks_completed(today_deployments)
        is_deadline_reached = now_ist.hour >= 21

        today_str = now_ist.strftime('%Y-%m-%d')

        # SAFELY INITIALIZE STATE FILES
        if not os.path.exists(FIRST_RUN_FILE): open(FIRST_RUN_FILE, 'w').close()
        if not os.path.exists(STATE_FILE): open(STATE_FILE, 'w').close()

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
                print("🕒 21:00 IST reached with pending tasks. Forcing final wrap-up summary...")
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
        # FIXED: Catching the actual exception message
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    main()