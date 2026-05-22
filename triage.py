#!/usr/bin/env python3
"""
Morning Gmail Triage
Reads unread Gmail threads from the last 24 h, drafts replies for the top 3
(prioritised by client domain), and posts a briefing to Slack #morning-standup.

First-time setup: python triage.py --auth
"""
import argparse
import base64
import json
import os
import re
import sys
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

ROOT = Path(__file__).parent
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
MODEL = "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Credentials / service helpers
# ---------------------------------------------------------------------------

def get_gmail_service():
    creds = None
    token_path = ROOT / "token.json"
    creds_path = ROOT / "credentials.json"

    if not creds_path.exists():
        sys.exit(
            "credentials.json not found. Download OAuth 2.0 Desktop credentials "
            "from Google Cloud Console and place them in the project root."
        )

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as fh:
            fh.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def get_own_domain(service: object) -> str:
    profile = service.users().getProfile(userId="me").execute()
    email = profile.get("emailAddress", "")
    return email.split("@")[-1].lower() if "@" in email else ""


# ---------------------------------------------------------------------------
# clients.txt
# ---------------------------------------------------------------------------

def load_client_domains() -> set[str]:
    path = ROOT / "clients.txt"
    if not path.exists():
        return set()
    with open(path) as fh:
        return {line.strip().lower() for line in fh if line.strip()}


# ---------------------------------------------------------------------------
# Message / thread helpers
# ---------------------------------------------------------------------------

def _header(message: dict, name: str) -> str:
    headers = message.get("payload", {}).get("headers", [])
    return next((h["value"] for h in headers if h["name"].lower() == name.lower()), "")


def sender_domain(message: dict) -> str:
    from_val = _header(message, "from")
    match = re.search(r"[\w.+\-]+@([\w.\-]+)", from_val)
    return match.group(1).lower() if match else ""


def subject(message: dict) -> str:
    return _header(message, "subject") or "(no subject)"


def body_text(message: dict) -> str:
    def _extract(part: dict) -> str:
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        for sub in part.get("parts", []):
            result = _extract(sub)
            if result:
                return result
        return ""

    return _extract(message.get("payload", {}))[:3000]


# ---------------------------------------------------------------------------
# Fetch + prioritise
# ---------------------------------------------------------------------------

def fetch_unread_threads(service, own_domain: str) -> list[dict]:
    """Return one representative message per unread thread from the last 24 h."""
    result = (
        service.users()
        .threads()
        .list(userId="me", q="is:unread newer_than:1d", maxResults=50)
        .execute()
    )
    thread_refs = result.get("threads", [])
    messages = []

    for ref in thread_refs:
        thread = (
            service.users()
            .threads()
            .get(userId="me", id=ref["id"], format="full")
            .execute()
        )
        thread_messages = thread.get("messages", [])
        if not thread_messages:
            continue
        latest = thread_messages[-1]
        domain = sender_domain(latest)
        if not domain or domain == own_domain:
            continue
        messages.append(latest)

    return messages


def prioritise(messages: list[dict], client_domains: set[str]) -> list[dict]:
    """Sort by client-domain first, then by recency. Return top 3."""

    def _key(msg: dict):
        is_client = 0 if sender_domain(msg) in client_domains else 1
        recency = -int(msg.get("internalDate", 0))
        return (is_client, recency)

    return sorted(messages, key=_key)[:3]


# ---------------------------------------------------------------------------
# AI: draft reply + summary
# ---------------------------------------------------------------------------

def generate_reply_and_summary(ai: anthropic.Anthropic, message: dict) -> tuple[str, str]:
    prompt = f"""You are drafting a reply on behalf of a professional.

Email details:
From: {_header(message, 'from')}
Subject: {subject(message)}
Body:
{body_text(message)}

Return a JSON object with exactly two keys:
- "reply": a professional reply, under 100 words, ready to send
- "summary": a single sentence summarising the email

Respond with valid JSON only, no markdown fences."""

    response = ai.messages.create(
        model=MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    # Strip accidental markdown fences if present
    raw = re.sub(r"^```[a-z]*\n?", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\n?```$", "", raw)
    data = json.loads(raw)
    return data["reply"], data["summary"]


# ---------------------------------------------------------------------------
# Gmail draft
# ---------------------------------------------------------------------------

def create_draft(service, message: dict, reply_text: str) -> str:
    """Create a Gmail draft reply and return its subject line."""
    thread_id = message["threadId"]
    msg_id = message["id"]
    to = _header(message, "from")
    subj = subject(message)
    reply_subject = subj if subj.lower().startswith("re:") else f"Re: {subj}"

    mime = MIMEText(reply_text)
    mime["To"] = to
    mime["Subject"] = reply_subject
    mime["In-Reply-To"] = msg_id
    mime["References"] = msg_id

    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    service.users().drafts().create(
        userId="me",
        body={"message": {"raw": raw, "threadId": thread_id}},
    ).execute()

    return reply_subject


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def post_slack_briefing(slack: WebClient, briefings: list[tuple[str, str, str, str]]):
    """
    briefings: list of (from_header, email_subject, one_sentence_summary, draft_subject)
    """
    today = datetime.now().strftime("%A, %B %-d")
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Morning Email Briefing — {today}"},
        }
    ]

    for i, (from_header, email_subject, summary, draft_subject) in enumerate(briefings, 1):
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{i}. {email_subject}*\n"
                        f"*From:* {from_header}\n"
                        f"*Summary:* {summary}\n"
                        f"*Draft reply subject:* _{draft_subject}_"
                    ),
                },
            }
        )

    fallback = f"Morning Email Briefing — {today}: {len(briefings)} thread(s) triaged."

    try:
        slack.chat_postMessage(
            channel="#morning-standup",
            text=fallback,
            blocks=blocks,
        )
    except SlackApiError as exc:
        print(f"Slack error: {exc.response['error']}", file=sys.stderr)
        raise


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    required = ["ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        sys.exit(f"Missing environment variables: {', '.join(missing)}")

    print("Connecting to Gmail…")
    gmail = get_gmail_service()
    own_domain = get_own_domain(gmail)
    print(f"Authenticated as …@{own_domain}")

    client_domains = load_client_domains()
    print(f"Client domains loaded: {client_domains or '(none)'}")

    print("Fetching unread threads from the last 24 h…")
    messages = fetch_unread_threads(gmail, own_domain)
    print(f"  {len(messages)} eligible thread(s) found")

    top3 = prioritise(messages, client_domains)
    print(f"  {len(top3)} thread(s) selected for triage")

    if not top3:
        print("Nothing to triage. Exiting.")
        return

    ai = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    slack = WebClient(token=os.environ["SLACK_BOT_TOKEN"])

    briefings = []
    for msg in top3:
        subj = subject(msg)
        from_hdr = _header(msg, "from")
        print(f"  Processing: {subj!r} from {from_hdr!r}")

        reply_text, summary = generate_reply_and_summary(ai, msg)
        draft_subject = create_draft(gmail, msg, reply_text)
        briefings.append((from_hdr, subj, summary, draft_subject))

    print("Posting briefing to Slack #morning-standup…")
    post_slack_briefing(slack, briefings)
    print(f"Done. {len(briefings)} thread(s) triaged.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Morning Gmail Triage")
    parser.add_argument(
        "--auth",
        action="store_true",
        help="Run Gmail OAuth flow only (generates token.json), then exit.",
    )
    args = parser.parse_args()

    if args.auth:
        svc = get_gmail_service()
        profile = svc.users().getProfile(userId="me").execute()
        print(f"Authenticated as {profile['emailAddress']}. token.json saved.")
    else:
        run()
