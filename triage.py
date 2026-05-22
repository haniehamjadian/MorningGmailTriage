#!/usr/bin/env python3
"""Morning Gmail Triage — surfaces priority emails, drafts replies, briefs Slack."""

import argparse
import base64
import json
import os
import re
import sys
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import parseaddr
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

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
REPO_ROOT = Path(__file__).parent


# ---------------------------------------------------------------------------
# clients.txt
# ---------------------------------------------------------------------------

def load_client_domains(path: str = "clients.txt") -> dict[str, int]:
    """Return {lowercase_domain: rank} from tab-separated clients.txt."""
    domains: dict[str, int] = {}
    filepath = REPO_ROOT / path
    with open(filepath) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            rank_str, domain = parts[0].strip(), parts[1].strip().lower()
            if not domain or "." not in domain:
                continue
            try:
                domains[domain] = int(rank_str)
            except ValueError:
                continue
    return domains


# ---------------------------------------------------------------------------
# Gmail auth
# ---------------------------------------------------------------------------

def get_gmail_service(
    credentials_path: str | None = None,
    token_path: str | None = None,
    auth_only: bool = False,
):
    """Return an authenticated Gmail API service object.

    For first-time setup, run with --auth to complete the browser OAuth flow.
    In CI, write token.json from the GMAIL_TOKEN_JSON env var before calling.
    """
    creds_file = credentials_path or os.environ.get(
        "GMAIL_CREDENTIALS_PATH", str(REPO_ROOT / "credentials.json")
    )
    tok_file = token_path or os.environ.get(
        "GMAIL_TOKEN_PATH", str(REPO_ROOT / "token.json")
    )

    creds: Credentials | None = None
    if Path(tok_file).exists():
        creds = Credentials.from_authorized_user_file(tok_file, SCOPES)

    if auth_only or not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        elif auth_only or not creds:
            flow = InstalledAppFlow.from_client_secrets_file(creds_file, SCOPES)
            creds = flow.run_local_server(port=0)
        Path(tok_file).write_text(creds.to_json())
        if auth_only:
            print(f"token.json saved to {tok_file}")
            print(
                "Copy its contents (base64-encoded) to the GMAIL_TOKEN_JSON_B64 "
                "GitHub Actions secret."
            )
            return None

    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# Gmail thread fetching
# ---------------------------------------------------------------------------

def _get_header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _decode_body(payload: dict) -> str:
    """Recursively extract plain-text body from a message payload."""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        text = _decode_body(part)
        if text:
            return text
    return ""


def extract_thread_metadata(thread: dict) -> dict:
    """Pull the fields we need from a raw Gmail thread resource."""
    messages = thread["messages"]
    first_msg = messages[0]
    last_msg = messages[-1]

    first_headers = first_msg["payload"]["headers"]
    last_headers = last_msg["payload"]["headers"]

    raw_from = _get_header(last_headers, "From")
    _, sender_email = parseaddr(raw_from)
    sender_email = sender_email.lower().strip()
    sender_domain = sender_email.split("@")[1] if "@" in sender_email else ""

    subject = _get_header(first_headers, "Subject") or "(no subject)"
    message_id = _get_header(last_headers, "Message-ID")
    references = _get_header(last_headers, "References")
    body_text = _decode_body(last_msg["payload"]) or last_msg.get("snippet", "")

    return {
        "thread_id": thread["id"],
        "subject": subject,
        "sender_email": sender_email,
        "sender_domain": sender_domain,
        "snippet": last_msg.get("snippet", ""),
        "body_text": body_text[:3000],
        "received_ts": int(last_msg.get("internalDate", "0")),
        "original_message_id": message_id,
        "references": references,
    }


def fetch_unread_threads(service, max_results: int = 50) -> list[dict]:
    """Return raw thread resources for unread messages from the last 24 hours."""
    result = (
        service.users()
        .threads()
        .list(userId="me", q="is:unread newer_than:1d", maxResults=max_results)
        .execute()
    )
    threads = []
    for item in result.get("threads", []):
        thread = (
            service.users()
            .threads()
            .get(userId="me", threadId=item["id"], format="full")
            .execute()
        )
        threads.append(thread)
    return threads


# ---------------------------------------------------------------------------
# Filtering and ranking
# ---------------------------------------------------------------------------

def filter_threads(threads: list[dict], user_domain: str) -> list[dict]:
    """Drop threads where the sender is from the user's own domain."""
    return [t for t in threads if t["sender_domain"] != user_domain.lower()]


def rank_threads(threads: list[dict], client_domains: dict[str, int]) -> list[dict]:
    """Sort: client-domain threads first (by rank), then all others by recency."""

    def sort_key(t: dict) -> tuple:
        domain = t["sender_domain"]
        rank = client_domains.get(domain)
        if rank is not None:
            return (0, rank, -t["received_ts"])
        return (1, 0, -t["received_ts"])

    return sorted(threads, key=sort_key)


# ---------------------------------------------------------------------------
# Anthropic — draft reply and summary
# ---------------------------------------------------------------------------

def draft_reply(ai: anthropic.Anthropic, thread: dict) -> str:
    """Generate a professional email reply under 100 words using claude-sonnet-4-6."""
    response = ai.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        system=(
            "You are a professional email assistant. Write a concise, professional "
            "reply in under 100 words. Do not include a subject line. Do not use "
            "hollow openers like 'I hope this email finds you well'. Go straight to "
            "the point."
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    f"Reply to this email:\n"
                    f"From: {thread['sender_email']}\n"
                    f"Subject: {thread['subject']}\n\n"
                    f"{thread['body_text']}"
                ),
            }
        ],
    )
    return response.content[0].text.strip()


def summarize_thread(ai: anthropic.Anthropic, thread: dict) -> str:
    """Return a one-sentence summary using claude-haiku-4-5-20251001."""
    response = ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        system="Summarize this email in exactly one sentence, maximum 25 words.",
        messages=[
            {
                "role": "user",
                "content": (
                    f"Subject: {thread['subject']}\n\n"
                    f"{thread['snippet']}"
                ),
            }
        ],
    )
    return response.content[0].text.strip()


# ---------------------------------------------------------------------------
# Gmail draft creation
# ---------------------------------------------------------------------------

def create_gmail_draft(
    service,
    thread: dict,
    reply_body: str,
    user_id: str = "me",
) -> str:
    """Create a draft reply in the thread. Returns the draft's subject line."""
    subject = thread["subject"]
    if not re.match(r"(?i)^re:", subject):
        subject = f"Re: {subject}"

    msg = MIMEText(reply_body, "plain", "utf-8")
    msg["To"] = thread["sender_email"]
    msg["Subject"] = subject
    if thread["original_message_id"]:
        msg["In-Reply-To"] = thread["original_message_id"]
        refs = thread["references"]
        msg["References"] = (
            f"{refs} {thread['original_message_id']}".strip()
            if refs
            else thread["original_message_id"]
        )

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    service.users().drafts().create(
        userId=user_id,
        body={"message": {"raw": raw, "threadId": thread["thread_id"]}},
    ).execute()
    return subject


# ---------------------------------------------------------------------------
# Slack briefing
# ---------------------------------------------------------------------------

def post_slack_briefing(
    slack: WebClient,
    channel: str,
    briefing_items: list[dict],
) -> None:
    """Post a formatted morning briefing to the specified Slack channel."""
    today = datetime.now(timezone.utc).strftime("%A, %B %-d")
    count = len(briefing_items)
    noun = "thread" if count == 1 else "threads"

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Morning Email Briefing — {today}",
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"{count} priority {noun} from the last 24 hours.",
                }
            ],
        },
        {"type": "divider"},
    ]

    for i, item in enumerate(briefing_items, 1):
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{i}. {item['subject']}*\n"
                        f"*From:* {item['sender']}\n"
                        f"*Summary:* {item['summary']}\n"
                        f"*Draft subject:* _{item['draft_subject']}_"
                    ),
                },
            }
        )
        if i < count:
            blocks.append({"type": "divider"})

    if not briefing_items:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "No external unread threads found in the last 24 hours.",
                },
            }
        )

    try:
        slack.chat_postMessage(
            channel=channel,
            blocks=blocks,
            text=f"Morning Email Briefing — {today}: {count} priority {noun}.",
        )
    except SlackApiError as e:
        print(f"Slack error: {e.response['error']}", file=sys.stderr)
        raise


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def main() -> None:
    user_email = os.environ["USER_EMAIL"]
    user_domain = user_email.split("@")[1].lower()
    slack_token = os.environ["SLACK_BOT_TOKEN"]
    slack_channel = os.environ.get("SLACK_CHANNEL", "#morning-standup")

    client_domains = load_client_domains()
    print(f"Loaded {len(client_domains)} client domain(s): {', '.join(client_domains)}")

    service = get_gmail_service()
    ai = anthropic.Anthropic()
    slack = WebClient(token=slack_token)

    print("Fetching unread threads from the last 24 hours…")
    raw_threads = fetch_unread_threads(service)
    all_threads = [extract_thread_metadata(t) for t in raw_threads]
    print(f"  {len(all_threads)} unread thread(s) fetched")

    filtered = filter_threads(all_threads, user_domain)
    print(f"  {len(filtered)} thread(s) after filtering out @{user_domain}")

    ranked = rank_threads(filtered, client_domains)
    top = ranked[:3]
    print(f"  {len(top)} thread(s) selected for triage")

    briefing_items = []
    for thread in top:
        print(f"\nProcessing: {thread['subject']!r} from {thread['sender_email']}")
        summary = summarize_thread(ai, thread)
        reply_text = draft_reply(ai, thread)
        draft_subject = create_gmail_draft(service, thread, reply_text)
        print(f"  Draft created: {draft_subject!r}")
        briefing_items.append(
            {
                "subject": thread["subject"],
                "sender": thread["sender_email"],
                "summary": summary,
                "draft_subject": draft_subject,
            }
        )

    print(f"\nPosting briefing to {slack_channel}…")
    post_slack_briefing(slack, slack_channel, briefing_items)
    print("Done.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Morning Gmail Triage",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "First-time setup:\n"
            "  1. python triage.py --auth   (complete browser OAuth once)\n"
            "  2. Set env vars (see .env.example)\n"
            "  3. python triage.py          (run manually to verify)\n"
        ),
    )
    parser.add_argument(
        "--auth",
        action="store_true",
        help="Run interactive OAuth flow to generate token.json",
    )
    args = parser.parse_args()

    if args.auth:
        get_gmail_service(auth_only=True)
    else:
        main()
