#!/usr/bin/env python3
"""Morning Gmail triage: reads unread threads, drafts replies, posts Slack briefing."""

import base64
import email.utils
import os
from datetime import datetime, timedelta, timezone
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

REPO_ROOT = Path(__file__).parent
CLIENTS_FILE = REPO_ROOT / "clients.txt"
TOKEN_FILE = REPO_ROOT / "token.json"
CREDENTIALS_FILE = REPO_ROOT / "credentials.json"

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]

SLACK_CHANNEL = "#morning-standup"


def load_client_domains() -> list[str]:
    """Load and normalise client domains from clients.txt."""
    if not CLIENTS_FILE.exists():
        return []
    return [
        line.strip().lower()
        for line in CLIENTS_FILE.read_text().splitlines()
        if line.strip()
    ]


def get_gmail_service():
    """Authenticate with Gmail OAuth2 and return an authorised service object."""
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _sender_domain(from_header: str) -> str:
    _, addr = email.utils.parseaddr(from_header)
    return addr.split("@")[1].lower() if "@" in addr else ""


def _extract_body(payload: dict) -> str:
    """Recursively extract the first plain-text part from a Gmail message payload."""
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        body = _extract_body(part)
        if body:
            return body
    return ""


def fetch_unread_threads(service, user_domain: str) -> list[dict]:
    """
    Return unread threads from the last 24 hours, skipping any whose sender
    belongs to user_domain.  Each entry contains thread metadata and body text.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    query = f"is:unread after:{int(cutoff.timestamp())}"

    raw = (
        service.users()
        .threads()
        .list(userId="me", q=query, maxResults=50)
        .execute()
    )

    threads = []
    for t in raw.get("threads", []):
        thread_data = (
            service.users()
            .threads()
            .get(
                userId="me",
                id=t["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )

        messages = thread_data.get("messages", [])
        if not messages:
            continue

        first_msg = messages[0]
        headers = {
            h["name"]: h["value"]
            for h in first_msg.get("payload", {}).get("headers", [])
        }

        from_header = headers.get("From", "")
        sender_domain = _sender_domain(from_header)

        if user_domain and sender_domain == user_domain:
            continue

        sender_name, sender_email = email.utils.parseaddr(from_header)

        # Fetch full body from the latest message in the thread
        latest_id = messages[-1]["id"]
        full_msg = (
            service.users()
            .messages()
            .get(userId="me", id=latest_id, format="full")
            .execute()
        )
        body = _extract_body(full_msg.get("payload", {}))

        threads.append(
            {
                "thread_id": t["id"],
                "latest_message_id": latest_id,
                "subject": headers.get("Subject", "(no subject)"),
                "sender_name": sender_name or sender_email,
                "sender_email": sender_email,
                "sender_domain": sender_domain,
                "snippet": full_msg.get("snippet", ""),
                "body": body[:2000],
            }
        )

    return threads


def prioritize_threads(threads: list[dict], client_domains: list[str]) -> list[dict]:
    """
    Sort threads so that client domains (in clients.txt order) come first,
    then all others. Return the top three.
    """
    client_domains = [d.lower() for d in client_domains]

    def rank(thread):
        try:
            return client_domains.index(thread["sender_domain"])
        except ValueError:
            return len(client_domains)

    return sorted(threads, key=rank)[:3]


def _claude_complete(client: anthropic.Anthropic, prompt: str, max_tokens: int) -> str:
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


def one_sentence_summary(thread: dict, ai: anthropic.Anthropic) -> str:
    prompt = (
        "Summarise this email in exactly one concise sentence (≤20 words).\n\n"
        f"Subject: {thread['subject']}\n{thread['snippet']}"
    )
    return _claude_complete(ai, prompt, 60)


def draft_reply_text(thread: dict, ai: anthropic.Anthropic) -> str:
    prompt = (
        "Write a professional email reply under 100 words. "
        "Output the body text only — no subject line, no salutation placeholder.\n\n"
        f"From: {thread['sender_name']} <{thread['sender_email']}>\n"
        f"Subject: {thread['subject']}\n\n"
        f"{thread['body']}"
    )
    return _claude_complete(ai, prompt, 200)


def create_gmail_draft(service, thread: dict, reply_body: str) -> str:
    """Create a Gmail draft reply associated with the thread. Returns the draft subject."""
    subject = thread["subject"]
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    msg = MIMEText(reply_body)
    msg["To"] = thread["sender_email"]
    msg["Subject"] = subject
    msg["In-Reply-To"] = thread["latest_message_id"]
    msg["References"] = thread["latest_message_id"]

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().drafts().create(
        userId="me",
        body={"message": {"raw": raw, "threadId": thread["thread_id"]}},
    ).execute()
    return subject


def post_slack_briefing(items: list[dict], slack_token: str) -> None:
    """Post the three-thread briefing to #morning-standup."""
    client = WebClient(token=slack_token)
    date_str = datetime.now().strftime("%A, %B %-d")
    lines = [f"*Morning Email Briefing — {date_str}*\n"]

    for i, item in enumerate(items, 1):
        t = item["thread"]
        lines.append(
            f"{i}. *From:* {t['sender_name']} ({t['sender_email']})\n"
            f"   *Summary:* {item['summary']}\n"
            f"   *Draft reply subject:* _{item['draft_subject']}_"
        )

    try:
        client.chat_postMessage(channel=SLACK_CHANNEL, text="\n".join(lines))
    except SlackApiError as exc:
        raise RuntimeError(f"Slack post failed: {exc.response['error']}") from exc


def run_triage() -> None:
    """Orchestrate the full morning triage run."""
    user_email = os.environ.get("USER_EMAIL", "")
    user_domain = user_email.split("@")[1].lower() if "@" in user_email else ""
    slack_token = os.environ["SLACK_BOT_TOKEN"]
    anthropic_api_key = os.environ["ANTHROPIC_API_KEY"]

    print(f"[{datetime.now().isoformat()}] Starting morning triage…")

    client_domains = load_client_domains()
    print(f"Client domains: {client_domains}")

    service = get_gmail_service()
    ai = anthropic.Anthropic(api_key=anthropic_api_key)

    threads = fetch_unread_threads(service, user_domain)
    print(f"Unread threads (excluding own domain): {len(threads)}")

    top = prioritize_threads(threads, client_domains)
    print(f"Top threads selected: {len(top)}")

    items = []
    for thread in top:
        print(f"  → {thread['subject']} | from {thread['sender_email']}")
        summary = one_sentence_summary(thread, ai)
        reply = draft_reply_text(thread, ai)
        draft_subject = create_gmail_draft(service, thread, reply)
        items.append({"thread": thread, "summary": summary, "draft_subject": draft_subject})

    post_slack_briefing(items, slack_token)
    print(f"[{datetime.now().isoformat()}] Triage complete — briefing posted to Slack.")


if __name__ == "__main__":
    run_triage()
