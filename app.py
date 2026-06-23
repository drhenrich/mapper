"""
Superhuman-style AI Email Manager
- Initial mailbox setup wizard: IMAP / POP3 / Gmail OAuth2
- Claude claude-opus-4-8 for categorization, summarization, reply drafting
- OpenAI TTS/Whisper for voice features (audio only)
- Plotly Treemap for visual inbox
- SQLite for snooze/follow-up persistence
- Google Calendar integration
"""

import os
import json
import base64
import sqlite3
import concurrent.futures
import re
import tempfile
import imaplib
import poplib
import smtplib
import email as email_lib
from email.header import decode_header as _decode_mime_words
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Optional

import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
import anthropic
import requests
from pydantic import BaseModel

# ─── Google OAuth2 ───────────────────────────────────────────────────────────
try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request as GoogleRequest
    from googleapiclient.discovery import build
    GOOGLE_AVAILABLE = True
except ImportError:
    GOOGLE_AVAILABLE = False

# ─── OpenAI (audio only) ─────────────────────────────────────────────────────
try:
    import openai
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

# ─── Audio recorder ──────────────────────────────────────────────────────────
try:
    from streamlit_audio_recorder import st_audiorec
    AUDIO_RECORDER_AVAILABLE = True
except ImportError:
    AUDIO_RECORDER_AVAILABLE = False

# ─── Config ──────────────────────────────────────────────────────────────────
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.readonly",
]
DB_PATH = Path(__file__).parent / "email_app.db"
PERSONA_FILE = Path(__file__).parent / "persona.json"
TOKEN_FILE = Path(__file__).parent / "token.json"
CREDENTIALS_FILE = Path(__file__).parent / "credentials.json"
MAILBOX_CONFIG_FILE = Path(__file__).parent / "mailbox_config.json"

PROVIDER_PRESETS = {
    "Gmail (OAuth2)": {
        "type": "gmail_oauth",
        "description": "Secure Google sign-in — recommended for Gmail. No password stored.",
    },
    "Gmail (IMAP)": {
        "type": "imap",
        "host": "imap.gmail.com",
        "port": 993,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "ssl": True,
        "description": "Gmail via IMAP/SMTP using an App Password (requires 2FA on your Google account).",
    },
    "Outlook / Microsoft 365": {
        "type": "imap",
        "host": "outlook.office365.com",
        "port": 993,
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "ssl": True,
        "description": "Outlook, Hotmail, Live, or Microsoft 365 accounts.",
    },
    "Yahoo Mail": {
        "type": "imap",
        "host": "imap.mail.yahoo.com",
        "port": 993,
        "smtp_host": "smtp.mail.yahoo.com",
        "smtp_port": 587,
        "ssl": True,
        "description": "Yahoo Mail — requires an App Password from Yahoo Account Security.",
    },
    "iCloud Mail": {
        "type": "imap",
        "host": "imap.mail.me.com",
        "port": 993,
        "smtp_host": "smtp.mail.me.com",
        "smtp_port": 587,
        "ssl": True,
        "description": "Apple iCloud Mail (@icloud.com, @me.com, @mac.com).",
    },
    "Custom IMAP": {
        "type": "imap",
        "host": "",
        "port": 993,
        "smtp_host": "",
        "smtp_port": 587,
        "ssl": True,
        "description": "Any IMAP-compatible provider (company mail, self-hosted, etc.).",
    },
    "Custom POP3": {
        "type": "pop3",
        "host": "",
        "port": 995,
        "smtp_host": "",
        "smtp_port": 587,
        "ssl": True,
        "description": "POP3 — downloads messages locally. Note: no server-side read/archive.",
    },
}

st.set_page_config(
    page_title="AI Email Manager",
    page_icon="✉️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Pydantic models ─────────────────────────────────────────────────────────
class EmailSummary(BaseModel):
    sender: str
    subject: str
    priority: str  # "Urgent", "FYI", "Noise"
    one_line_summary: str
    action_item: Optional[str] = None
    message_id: Optional[str] = None
    thread_id: Optional[str] = None
    snippet: Optional[str] = None
    date: Optional[str] = None
    to: Optional[str] = None
    labels: list[str] = []


class MorningBriefing(BaseModel):
    urgent_emails: list[EmailSummary] = []
    fyi_emails: list[EmailSummary] = []
    noise_emails: list[EmailSummary] = []


# ─── Database setup ──────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snooze (
            message_id TEXT PRIMARY KEY,
            subject TEXT,
            sender TEXT,
            snooze_until TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS followups (
            message_id TEXT PRIMARY KEY,
            subject TEXT,
            sender TEXT,
            followup_date TEXT,
            note TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            date TEXT PRIMARY KEY,
            processed INTEGER DEFAULT 0,
            replied INTEGER DEFAULT 0,
            snoozed INTEGER DEFAULT 0,
            archived INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS drafts (
            message_id TEXT PRIMARY KEY,
            draft_text TEXT,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contacts (
            email TEXT PRIMARY KEY,
            name TEXT,
            last_contact TEXT,
            email_count INTEGER DEFAULT 0,
            notes TEXT
        )
    """)
    conn.commit()
    conn.close()


init_db()


# ─── Mailbox config ──────────────────────────────────────────────────────────
def load_mailbox_config() -> Optional[dict]:
    if MAILBOX_CONFIG_FILE.exists():
        return json.loads(MAILBOX_CONFIG_FILE.read_text())
    # Backward compat: if token.json exists, the user was using Gmail OAuth2
    if TOKEN_FILE.exists():
        cfg = {"type": "gmail_oauth", "provider": "Gmail (OAuth2)"}
        save_mailbox_config(cfg)
        return cfg
    return None


def save_mailbox_config(config: dict):
    MAILBOX_CONFIG_FILE.write_text(json.dumps(config, indent=2))


# ─── Email header / body helpers ─────────────────────────────────────────────
def _decode_email_header(value: str) -> str:
    """Decode MIME-encoded email header (handles =?utf-8?b?...?= etc.)."""
    if not value:
        return ""
    parts = []
    for decoded, charset in _decode_mime_words(value):
        if isinstance(decoded, bytes):
            parts.append(decoded.decode(charset or "utf-8", errors="ignore"))
        else:
            parts.append(str(decoded))
    return "".join(parts)


def _extract_imap_body(msg) -> str:
    """Extract plain-text body from email.message.Message; falls back to stripped HTML."""
    if msg.is_multipart():
        # Prefer plain text
        for part in msg.walk():
            if (part.get_content_type() == "text/plain"
                    and "attachment" not in str(part.get("Content-Disposition", ""))):
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="ignore") if payload else ""
        # HTML fallback
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                html = payload.decode(charset, errors="ignore") if payload else ""
                text = re.sub(r"<[^>]+>", " ", html)
                return re.sub(r"\s+", " ", text).strip()
        return ""
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="ignore")
        return ""


# ─── IMAP functions ──────────────────────────────────────────────────────────
def _imap_connect(config: dict) -> imaplib.IMAP4:
    if config.get("ssl", True):
        return imaplib.IMAP4_SSL(config["host"], config.get("port", 993))
    mail = imaplib.IMAP4(config["host"], config.get("port", 143))
    mail.starttls()
    return mail


def test_imap_connection(config: dict) -> tuple[bool, str]:
    try:
        mail = _imap_connect(config)
        mail.login(config["username"], config["password"])
        mail.select("INBOX")
        mail.logout()
        return True, "✅ Connection successful!"
    except imaplib.IMAP4.error as e:
        return False, f"IMAP authentication error: {e}"
    except OSError as e:
        return False, f"Cannot reach server: {e}"
    except Exception as e:
        return False, f"Connection failed: {e}"


def fetch_emails_imap(config: dict, max_results: int = 50) -> list[dict]:
    try:
        mail = _imap_connect(config)
        mail.login(config["username"], config["password"])
        mail.select("INBOX")

        _, data = mail.search(None, "UNSEEN")
        ids = data[0].split() if data[0] else []
        ids = ids[-max_results:]  # keep the latest N

        emails = []
        for eid in reversed(ids):  # newest first
            try:
                _, msg_data = mail.fetch(eid, "(RFC822)")
                raw = msg_data[0][1]
                msg = email_lib.message_from_bytes(raw)

                subject = _decode_email_header(msg.get("Subject", "(no subject)"))
                sender = _decode_email_header(msg.get("From", ""))
                to = _decode_email_header(msg.get("To", ""))
                date = msg.get("Date", "")
                msg_id = msg.get("Message-ID", "").strip() or f"imap_{eid.decode()}"
                in_reply_to = msg.get("In-Reply-To", "").strip()
                list_unsub = msg.get("List-Unsubscribe", "")

                body = _extract_imap_body(msg)
                snippet = body[:200].replace("\n", " ").strip()

                emails.append({
                    "message_id": msg_id,
                    "thread_id": in_reply_to or msg_id,
                    "subject": subject,
                    "sender": sender,
                    "to": to,
                    "date": date,
                    "snippet": snippet,
                    "body": body[:2000],
                    "list_unsubscribe": list_unsub,
                    "in_reply_to": in_reply_to,
                    "_imap_uid": eid.decode(),
                })
            except Exception:
                continue

        mail.logout()
        return emails
    except Exception as e:
        st.error(f"IMAP fetch error: {e}")
        return []


def mark_read_imap(config: dict, imap_uid: str):
    try:
        mail = _imap_connect(config)
        mail.login(config["username"], config["password"])
        mail.select("INBOX")
        mail.store(imap_uid, "+FLAGS", "\\Seen")
        mail.logout()
    except Exception:
        pass


def archive_email_imap(config: dict, imap_uid: str):
    """Mark as read and attempt to move to Trash/Archive."""
    try:
        mail = _imap_connect(config)
        mail.login(config["username"], config["password"])
        mail.select("INBOX")
        mail.store(imap_uid, "+FLAGS", "\\Seen")

        provider = config.get("provider", "")
        # Try common archive/trash folder names
        trash_candidates = []
        if "Gmail" in provider:
            trash_candidates = ["[Gmail]/All Mail"]
        else:
            trash_candidates = ["Archive", "Trash", "Deleted Items", "Deleted Messages", "INBOX.Trash"]

        moved = False
        for folder in trash_candidates:
            try:
                result = mail.copy(imap_uid, folder)
                if result[0] == "OK":
                    mail.store(imap_uid, "+FLAGS", "\\Deleted")
                    mail.expunge()
                    moved = True
                    break
            except Exception:
                continue

        if not moved:
            # Fallback: just mark as read (safe)
            pass

        mail.logout()
    except Exception:
        pass


# ─── POP3 functions ──────────────────────────────────────────────────────────
def _pop3_connect(config: dict):
    if config.get("ssl", True):
        return poplib.POP3_SSL(config["host"], config.get("port", 995))
    return poplib.POP3(config["host"], config.get("port", 110))


def test_pop3_connection(config: dict) -> tuple[bool, str]:
    try:
        mail = _pop3_connect(config)
        mail.user(config["username"])
        mail.pass_(config["password"])
        count = len(mail.list()[1])
        mail.quit()
        return True, f"✅ Connected! {count} message(s) on server."
    except poplib.error_proto as e:
        return False, f"POP3 authentication error: {e}"
    except OSError as e:
        return False, f"Cannot reach server: {e}"
    except Exception as e:
        return False, f"Connection failed: {e}"


def fetch_emails_pop3(config: dict, max_results: int = 50) -> list[dict]:
    try:
        mail = _pop3_connect(config)
        mail.user(config["username"])
        mail.pass_(config["password"])

        msg_list = mail.list()[1]
        total = len(msg_list)
        start = max(1, total - max_results + 1)

        emails = []
        for i in range(total, start - 1, -1):  # newest first
            try:
                _, lines, _ = mail.retr(i)
                raw = b"\n".join(lines)
                msg = email_lib.message_from_bytes(raw)

                subject = _decode_email_header(msg.get("Subject", "(no subject)"))
                sender = _decode_email_header(msg.get("From", ""))
                to = _decode_email_header(msg.get("To", ""))
                date = msg.get("Date", "")
                msg_id = msg.get("Message-ID", "").strip() or f"pop3_{i}"
                in_reply_to = msg.get("In-Reply-To", "").strip()
                list_unsub = msg.get("List-Unsubscribe", "")

                body = _extract_imap_body(msg)
                snippet = body[:200].replace("\n", " ").strip()

                emails.append({
                    "message_id": msg_id,
                    "thread_id": in_reply_to or msg_id,
                    "subject": subject,
                    "sender": sender,
                    "to": to,
                    "date": date,
                    "snippet": snippet,
                    "body": body[:2000],
                    "list_unsubscribe": list_unsub,
                    "in_reply_to": in_reply_to,
                    "_pop3_num": i,
                })
            except Exception:
                continue

        mail.quit()
        return emails
    except Exception as e:
        st.error(f"POP3 fetch error: {e}")
        return []


# ─── SMTP sending ─────────────────────────────────────────────────────────────
def send_email_smtp(config: dict, to: str, subject: str, body: str, in_reply_to: str = ""):
    smtp_host = config.get("smtp_host", "")
    smtp_port = int(config.get("smtp_port", 587))
    username = config["username"]
    password = config["password"]

    msg = MIMEMultipart()
    msg["From"] = username
    msg["To"] = to
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
        server.ehlo()
        server.starttls()
        server.login(username, password)
        server.send_message(msg)


# ─── Gmail OAuth2 ────────────────────────────────────────────────────────────
def get_gmail_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GOOGLE_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
        elif CREDENTIALS_FILE.exists():
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), GOOGLE_SCOPES)
            creds = flow.run_local_server(port=0)
        else:
            return None
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def get_calendar_service():
    if not TOKEN_FILE.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GOOGLE_SCOPES)
    if not creds or not creds.valid:
        return None
    return build("calendar", "v3", credentials=creds)


def _gmail_extract_body(payload) -> str:
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore") if data else ""
    if "parts" in payload:
        for part in payload["parts"]:
            text = _gmail_extract_body(part)
            if text:
                return text
    return ""


def fetch_emails_gmail(service, max_results: int = 50) -> list[dict]:
    result = service.users().messages().list(
        userId="me", labelIds=["INBOX"], q="is:unread", maxResults=max_results,
    ).execute()
    messages = result.get("messages", [])
    emails = []
    for msg in messages:
        detail = service.users().messages().get(userId="me", id=msg["id"], format="full").execute()
        headers = {h["name"]: h["value"] for h in detail["payload"]["headers"]}
        body = _gmail_extract_body(detail["payload"])
        emails.append({
            "message_id": msg["id"],
            "thread_id": detail.get("threadId", ""),
            "subject": headers.get("Subject", "(no subject)"),
            "sender": headers.get("From", ""),
            "to": headers.get("To", ""),
            "date": headers.get("Date", ""),
            "snippet": detail.get("snippet", ""),
            "body": body[:2000],
            "list_unsubscribe": headers.get("List-Unsubscribe", ""),
            "in_reply_to": headers.get("In-Reply-To", ""),
        })
    return emails


def send_email_gmail(service, to: str, subject: str, body: str, thread_id: str = "", in_reply_to: str = ""):
    msg = MIMEMultipart()
    msg["To"] = to
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.attach(MIMEText(body, "plain"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    payload = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id
    service.users().messages().send(userId="me", body=payload).execute()


def archive_email_gmail(service, message_id: str):
    service.users().messages().modify(
        userId="me", id=message_id, body={"removeLabelIds": ["INBOX"]}
    ).execute()


# ─── Unified dispatch ─────────────────────────────────────────────────────────
def fetch_emails_all(max_results: int = 50) -> list[dict]:
    """Fetch emails using whichever backend is configured."""
    cfg = st.session_state.mailbox_config
    if not cfg:
        return []
    if cfg["type"] == "gmail_oauth":
        svc = st.session_state.gmail_service
        if svc:
            return fetch_emails_gmail(svc, max_results)
        return []
    if cfg["type"] == "imap":
        return fetch_emails_imap(cfg, max_results)
    if cfg["type"] == "pop3":
        return fetch_emails_pop3(cfg, max_results)
    return []


def send_email_any(to: str, subject: str, body: str, thread_id: str = "", in_reply_to: str = ""):
    """Send an email using whichever backend is configured."""
    cfg = st.session_state.mailbox_config
    if not cfg:
        st.error("No mailbox configured.")
        return
    if cfg["type"] == "gmail_oauth":
        svc = st.session_state.gmail_service
        if svc:
            send_email_gmail(svc, to, subject, body, thread_id, in_reply_to)
        else:
            st.error("Gmail not connected.")
    else:
        send_email_smtp(cfg, to, subject, body, in_reply_to)


def archive_email_any(message_id: str, imap_uid: str = ""):
    cfg = st.session_state.mailbox_config
    if not cfg:
        return
    if cfg["type"] == "gmail_oauth":
        svc = st.session_state.gmail_service
        if svc:
            archive_email_gmail(svc, message_id)
    elif cfg["type"] == "imap" and imap_uid:
        archive_email_imap(cfg, imap_uid)
    # POP3 has no server-side archive; silently skip


# ─── Claude AI ───────────────────────────────────────────────────────────────
def get_anthropic_client():
    api_key = os.environ.get("ANTHROPIC_API_KEY") or st.session_state.get("anthropic_api_key", "")
    if not api_key:
        return None
    return anthropic.Anthropic(api_key=api_key)


def categorize_emails_with_claude(emails: list[dict]) -> MorningBriefing:
    client = get_anthropic_client()
    if not client:
        st.error("No Anthropic API key configured.")
        return MorningBriefing()

    email_list = json.dumps([
        {
            "message_id": e["message_id"],
            "thread_id": e["thread_id"],
            "sender": e["sender"],
            "subject": e["subject"],
            "date": e["date"],
            "snippet": e["snippet"],
            "body_preview": e["body"][:500],
        }
        for e in emails
    ], ensure_ascii=False, indent=2)

    schema = {
        "type": "object",
        "properties": {
            "urgent_emails": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "sender": {"type": "string"},
                        "subject": {"type": "string"},
                        "priority": {"type": "string", "enum": ["Urgent"]},
                        "one_line_summary": {"type": "string"},
                        "action_item": {"type": "string"},
                        "message_id": {"type": "string"},
                        "thread_id": {"type": "string"},
                        "date": {"type": "string"},
                    },
                    "required": ["sender", "subject", "priority", "one_line_summary", "message_id", "thread_id"],
                },
            },
            "fyi_emails": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "sender": {"type": "string"},
                        "subject": {"type": "string"},
                        "priority": {"type": "string", "enum": ["FYI"]},
                        "one_line_summary": {"type": "string"},
                        "action_item": {"type": "string"},
                        "message_id": {"type": "string"},
                        "thread_id": {"type": "string"},
                        "date": {"type": "string"},
                    },
                    "required": ["sender", "subject", "priority", "one_line_summary", "message_id", "thread_id"],
                },
            },
            "noise_emails": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "sender": {"type": "string"},
                        "subject": {"type": "string"},
                        "priority": {"type": "string", "enum": ["Noise"]},
                        "one_line_summary": {"type": "string"},
                        "message_id": {"type": "string"},
                        "thread_id": {"type": "string"},
                        "date": {"type": "string"},
                    },
                    "required": ["sender", "subject", "priority", "one_line_summary", "message_id", "thread_id"],
                },
            },
        },
        "required": ["urgent_emails", "fyi_emails", "noise_emails"],
    }

    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=4096,
        thinking={"type": "adaptive"},
        output_config={"format": {"type": "json", "json_schema": {"name": "MorningBriefing", "schema": schema}}},
        messages=[{
            "role": "user",
            "content": f"""You are an expert email triage assistant. Analyze these emails and categorize each as:
- Urgent: requires immediate action (deadlines, important questions from key people, time-sensitive)
- FYI: informational, read later (newsletters you care about, CC emails, updates)
- Noise: can be archived/ignored (promotions, spam, irrelevant notifications)

For each email provide a one-line summary and (for Urgent) a clear action item.

Emails to analyze:
{email_list}

Return the full structured categorization.""",
        }],
    )

    text_content = next((b.text for b in response.content if hasattr(b, "text") and b.text), None)
    if not text_content:
        return MorningBriefing()
    data = json.loads(text_content)
    briefing = MorningBriefing(**data)
    # Attach snippet/to from originals
    id_map = {e["message_id"]: e for e in emails}
    for em_list in [briefing.urgent_emails, briefing.fyi_emails, briefing.noise_emails]:
        for em in em_list:
            orig = id_map.get(em.message_id, {})
            em.snippet = orig.get("snippet", "")
            em.to = orig.get("to", "")
    return briefing


def draft_reply_with_claude(email: dict, persona: dict, instruction: str = "") -> str:
    client = get_anthropic_client()
    if not client:
        return "Error: No Anthropic API key."

    style_description = ""
    if persona:
        style_description = f"""
Writing style profile:
- Tone: {persona.get('tone', 'professional')}
- Typical greeting: {persona.get('greeting', 'Hi,')}
- Typical closing: {persona.get('closing', 'Best regards,')}
- Average email length: {persona.get('avg_length', 'concise')}
- Style notes: {persona.get('notes', '')}
"""

    prompt = f"""Draft a reply to this email.

Original email:
From: {email.get('sender', '')}
Subject: {email.get('subject', '')}
Body: {email.get('body', email.get('snippet', ''))}

{style_description}
{f"Additional instruction: {instruction}" if instruction else ""}

Write only the reply body text, no subject line needed."""

    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=1024,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in response.content:
        if hasattr(block, "text"):
            return block.text
    return ""


def generate_morning_briefing_text(briefing: MorningBriefing) -> str:
    client = get_anthropic_client()
    if not client:
        return "Good morning! Could not load AI briefing."

    urgent_count = len(briefing.urgent_emails)
    fyi_count = len(briefing.fyi_emails)
    noise_count = len(briefing.noise_emails)
    urgent_summary = "\n".join(
        f"- From {e.sender}: {e.one_line_summary}. Action: {e.action_item or 'Reply needed'}"
        for e in briefing.urgent_emails[:5]
    )

    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=512,
        messages=[{"role": "user", "content": f"""Create a concise 30-second spoken morning email briefing.

Stats: {urgent_count} urgent, {fyi_count} for your info, {noise_count} noise emails.

Urgent items:
{urgent_summary or "None"}

Write a natural, spoken-word briefing (no markdown, no lists — just flowing speech). Keep it under 100 words."""}],
    )
    for block in response.content:
        if hasattr(block, "text"):
            return block.text
    return "Good morning! You have emails to review."


def analyze_writing_style(sample_emails: list[str]) -> dict:
    client = get_anthropic_client()
    if not client:
        return {}

    samples = "\n\n---\n\n".join(sample_emails[:10])
    schema = {
        "type": "object",
        "properties": {
            "tone": {"type": "string"},
            "greeting": {"type": "string"},
            "closing": {"type": "string"},
            "avg_length": {"type": "string"},
            "notes": {"type": "string"},
        },
        "required": ["tone", "greeting", "closing", "avg_length", "notes"],
    }

    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=512,
        output_config={"format": {"type": "json", "json_schema": {"name": "PersonaProfile", "schema": schema}}},
        messages=[{"role": "user", "content": f"""Analyze these email writing samples and extract the writing style profile.

Samples:
{samples}

Identify: tone (formal/informal/friendly), typical greeting phrase, typical closing phrase, average email length (brief/moderate/detailed), and key style notes."""}],
    )
    for block in response.content:
        if hasattr(block, "text"):
            return json.loads(block.text)
    return {}


def get_calendar_free_slots(calendar_service, days_ahead: int = 5) -> list[dict]:
    if not calendar_service:
        return []
    now = datetime.utcnow().isoformat() + "Z"
    end = (datetime.utcnow() + timedelta(days=days_ahead)).isoformat() + "Z"
    events_result = calendar_service.events().list(
        calendarId="primary", timeMin=now, timeMax=end, singleEvents=True, orderBy="startTime",
    ).execute()
    return [
        {"start": e["start"].get("dateTime", e["start"].get("date")),
         "end": e["end"].get("dateTime", e["end"].get("date")),
         "title": e.get("summary", "Busy")}
        for e in events_result.get("items", [])
    ]


def suggest_meeting_times(calendar_service, email_body: str) -> str:
    busy = get_calendar_free_slots(calendar_service)
    client = get_anthropic_client()
    if not client:
        return "Could not suggest meeting times."
    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=256,
        messages=[{"role": "user", "content": f"""The user received this email requesting a meeting:
{email_body}

Their calendar shows these busy slots:
{json.dumps(busy, indent=2)}

Suggest 3 specific meeting times in the next 5 days during business hours (9am-6pm) that don't conflict. Be concise and natural."""}],
    )
    for block in response.content:
        if hasattr(block, "text"):
            return block.text
    return ""


# ─── Voice features (OpenAI audio) ───────────────────────────────────────────
def text_to_speech(text: str) -> Optional[bytes]:
    if not OPENAI_AVAILABLE:
        return None
    api_key = os.environ.get("OPENAI_API_KEY") or st.session_state.get("openai_api_key", "")
    if not api_key:
        return None
    openai_client = openai.OpenAI(api_key=api_key)
    response = openai_client.audio.speech.create(model="tts-1", voice="alloy", input=text)
    return response.content


def speech_to_text(audio_bytes: bytes) -> str:
    if not OPENAI_AVAILABLE:
        return ""
    api_key = os.environ.get("OPENAI_API_KEY") or st.session_state.get("openai_api_key", "")
    if not api_key:
        return ""
    openai_client = openai.OpenAI(api_key=api_key)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name
    with open(tmp_path, "rb") as f:
        transcript = openai_client.audio.transcriptions.create(model="whisper-1", file=f)
    os.unlink(tmp_path)
    return transcript.text


# ─── Snooze / Follow-up ──────────────────────────────────────────────────────
def snooze_email(message_id: str, subject: str, sender: str, hours: int = 4):
    snooze_until = (datetime.now() + timedelta(hours=hours)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO snooze VALUES (?, ?, ?, ?)", (message_id, subject, sender, snooze_until))
    conn.commit()
    conn.close()


def add_followup(message_id: str, subject: str, sender: str, days: int = 2, note: str = ""):
    followup_date = (datetime.now() + timedelta(days=days)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO followups VALUES (?, ?, ?, ?, ?)", (message_id, subject, sender, followup_date, note))
    conn.commit()
    conn.close()


def get_due_followups() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT * FROM followups WHERE followup_date <= ?", (datetime.now().isoformat(),)).fetchall()
    conn.close()
    return [{"message_id": r[0], "subject": r[1], "sender": r[2], "date": r[3], "note": r[4]} for r in rows]


_VALID_STAT_FIELDS = {"processed", "replied", "snoozed", "archived"}


def record_stat(field: str):
    if field not in _VALID_STAT_FIELDS:
        raise ValueError(f"Invalid stat field: {field}")
    today = datetime.now().date().isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO stats (date) VALUES (?)", (today,))
    conn.execute(f"UPDATE stats SET {field} = {field} + 1 WHERE date = ?", (today,))
    conn.commit()
    conn.close()


def get_stats(days: int = 30) -> list[dict]:
    since = (datetime.now() - timedelta(days=days)).date().isoformat()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT date, processed, replied, snoozed, archived FROM stats WHERE date >= ? ORDER BY date", (since,)
    ).fetchall()
    conn.close()
    return [{"date": r[0], "processed": r[1], "replied": r[2], "snoozed": r[3], "archived": r[4]} for r in rows]


# ─── CRM / Contacts ──────────────────────────────────────────────────────────
def update_contact(email: str, name: str):
    today = datetime.now().date().isoformat()
    conn = sqlite3.connect(DB_PATH)
    existing = conn.execute("SELECT email_count FROM contacts WHERE email = ?", (email,)).fetchone()
    if existing:
        conn.execute("UPDATE contacts SET name=?, last_contact=?, email_count=email_count+1 WHERE email=?", (name, today, email))
    else:
        conn.execute("INSERT INTO contacts VALUES (?, ?, ?, 1, ?)", (email, name, today, ""))
    conn.commit()
    conn.close()


def get_contact(email: str) -> dict:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT * FROM contacts WHERE email = ?", (email,)).fetchone()
    conn.close()
    return {"email": row[0], "name": row[1], "last_contact": row[2], "email_count": row[3], "notes": row[4]} if row else {}


def get_all_contacts() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT * FROM contacts ORDER BY email_count DESC LIMIT 50").fetchall()
    conn.close()
    return [{"email": r[0], "name": r[1], "last_contact": r[2], "email_count": r[3], "notes": r[4]} for r in rows]


# ─── Background draft generation ─────────────────────────────────────────────
_draft_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)


def generate_draft_background(email: dict, persona: dict):
    def _run():
        draft = draft_reply_with_claude(email, persona)
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT OR REPLACE INTO drafts VALUES (?, ?, ?)", (email["message_id"], draft, datetime.now().isoformat()))
        conn.commit()
        conn.close()
    _draft_executor.submit(_run)


def get_draft(message_id: str) -> Optional[str]:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT draft_text FROM drafts WHERE message_id = ?", (message_id,)).fetchone()
    conn.close()
    return row[0] if row else None


# ─── Newsletter unsubscribe ───────────────────────────────────────────────────
def try_unsubscribe(list_unsubscribe_header: str) -> str:
    if not list_unsubscribe_header:
        return "No unsubscribe header found."
    urls = re.findall(r"<(https?://[^>]+)>", list_unsubscribe_header)
    for url in urls:
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code < 400:
                return f"Unsubscribed via {url}"
        except Exception:
            continue
    mailto = re.findall(r"<mailto:([^>]+)>", list_unsubscribe_header)
    if mailto:
        return f"Send unsubscribe email to: {mailto[0]}"
    return "Could not auto-unsubscribe. Manual action needed."


# ─── Persona storage ─────────────────────────────────────────────────────────
def load_persona() -> dict:
    if PERSONA_FILE.exists():
        return json.loads(PERSONA_FILE.read_text())
    return {}


def save_persona(persona: dict):
    PERSONA_FILE.write_text(json.dumps(persona, indent=2))


# ─── Demo emails ─────────────────────────────────────────────────────────────
def _demo_emails() -> list[dict]:
    now = datetime.now().strftime("%a, %d %b %Y %H:%M:%S")
    return [
        {"message_id": "demo_1", "thread_id": "thread_1", "subject": "Q4 Budget Approval Needed by EOD",
         "sender": "CFO <cfo@company.com>", "to": "me@example.com", "date": now,
         "snippet": "We need your sign-off on the Q4 budget by end of day today...",
         "body": "Hi, We need your sign-off on the Q4 budget proposal by end of day today. The board meeting is tomorrow morning at 9am. Please review and approve or send your comments. Best, CFO",
         "list_unsubscribe": "", "in_reply_to": ""},
        {"message_id": "demo_2", "thread_id": "thread_2", "subject": "Team lunch tomorrow?",
         "sender": "Alice <alice@team.com>", "to": "me@example.com", "date": now,
         "snippet": "Hey, are you free for team lunch tomorrow around noon?",
         "body": "Hey, are you free for team lunch tomorrow around noon? We're thinking of trying that new Thai place. Let me know! Alice",
         "list_unsubscribe": "", "in_reply_to": ""},
        {"message_id": "demo_3", "thread_id": "thread_3", "subject": "Your Weekly Newsletter - Top Tech Stories",
         "sender": "TechDigest <news@techdigest.com>", "to": "me@example.com", "date": now,
         "snippet": "This week in tech: AI breakthroughs, new smartphone releases...",
         "body": "This week in tech: AI breakthroughs, new smartphone releases, and market updates.",
         "list_unsubscribe": "<https://techdigest.com/unsubscribe?token=abc123>", "in_reply_to": ""},
        {"message_id": "demo_4", "thread_id": "thread_4", "subject": "Critical bug in production - needs immediate fix",
         "sender": "DevOps <devops@company.com>", "to": "me@example.com", "date": now,
         "snippet": "We have a P0 bug causing payment failures. Need your input on the fix...",
         "body": "URGENT: Payment processing is failing for 15% of users due to a database timeout issue. We've identified the root cause and need your approval to push the hotfix to production. Response needed ASAP.",
         "list_unsubscribe": "", "in_reply_to": ""},
        {"message_id": "demo_5", "thread_id": "thread_5", "subject": "50% Off - Flash Sale Today Only!",
         "sender": "Deals <deals@shopexample.com>", "to": "me@example.com", "date": now,
         "snippet": "Don't miss our biggest sale of the year!",
         "body": "FLASH SALE: 50% off all items today only! Use code SAVE50 at checkout.",
         "list_unsubscribe": "<https://shopexample.com/unsubscribe>", "in_reply_to": ""},
        {"message_id": "demo_6", "thread_id": "thread_6", "subject": "Project Alpha - Milestone Review",
         "sender": "Project Manager <pm@partner.com>", "to": "me@example.com", "date": now,
         "snippet": "Sharing the milestone review for Project Alpha. Everything on track.",
         "body": "Hi, Attached is the Q3 milestone review for Project Alpha. We're 95% on track. No blockers. Will share the full report in the Friday all-hands. Thanks, PM",
         "list_unsubscribe": "", "in_reply_to": ""},
    ]


# ─── Plotly Treemap ──────────────────────────────────────────────────────────
def build_treemap(briefing: MorningBriefing) -> go.Figure:
    labels, parents, values, colors, custom = ["Inbox"], [""], [0], ["#1e1e2e"], [""]

    for cat_name, emails, color in [
        ("Urgent", briefing.urgent_emails, "#e74c3c"),
        ("FYI", briefing.fyi_emails, "#f39c12"),
        ("Noise", briefing.noise_emails, "#95a5a6"),
    ]:
        if not emails:
            continue
        labels.append(cat_name)
        parents.append("Inbox")
        values.append(len(emails))
        colors.append(color)
        custom.append(f"{len(emails)} emails")

        for em in emails:
            labels.append(f"{em.sender[:20]}\n{em.subject[:30]}")
            parents.append(cat_name)
            values.append({"Urgent": 3, "FYI": 2, "Noise": 1}.get(cat_name, 1))
            colors.append(color)
            custom.append(f"{em.one_line_summary}<br>ID: {em.message_id}")

    fig = go.Figure(go.Treemap(
        labels=labels, parents=parents, values=values,
        marker=dict(colors=colors, line=dict(width=2, color="#0d0d1a")),
        customdata=custom,
        hovertemplate="<b>%{label}</b><br>%{customdata}<extra></extra>",
        textfont=dict(color="white", size=11),
    ))
    fig.update_layout(margin=dict(t=10, l=0, r=0, b=0), paper_bgcolor="#0d0d1a", height=450)
    return fig


# ─── Session state init ──────────────────────────────────────────────────────
_initial_mailbox_config = load_mailbox_config()

for key, default in {
    "briefing": None,
    "raw_emails": [],
    "selected_email": None,
    "gmail_service": None,
    "calendar_service": None,
    "persona": load_persona(),
    "reply_draft": "",
    "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
    "openai_api_key": os.environ.get("OPENAI_API_KEY", ""),
    "max_emails": 50,
    "mailbox_config": _initial_mailbox_config,
    "mailbox_configured": _initial_mailbox_config is not None,
    "_setup_step": 1,
    "_test_result": None,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

# Auto-connect Gmail OAuth2 if token exists and config says oauth
if (st.session_state.mailbox_configured
        and st.session_state.mailbox_config
        and st.session_state.mailbox_config.get("type") == "gmail_oauth"
        and st.session_state.gmail_service is None
        and GOOGLE_AVAILABLE
        and TOKEN_FILE.exists()):
    try:
        svc = get_gmail_service()
        if svc:
            st.session_state.gmail_service = svc
            st.session_state.calendar_service = get_calendar_service()
    except Exception:
        pass


# ─── Mailbox setup wizard ────────────────────────────────────────────────────
def show_mailbox_setup():
    st.markdown("# ✉️ AI Email Manager")
    st.markdown("### Connect your mailbox to get started")
    st.markdown("---")

    col_form, col_info = st.columns([3, 2])

    with col_form:
        provider = st.selectbox(
            "Email provider:",
            list(PROVIDER_PRESETS.keys()),
            key="setup_provider",
        )
        preset = PROVIDER_PRESETS[provider]
        st.caption(preset.get("description", ""))

        st.markdown("---")

        if preset["type"] == "gmail_oauth":
            # ── Gmail OAuth2 ──────────────────────────────────────────────────
            if not GOOGLE_AVAILABLE:
                st.error("Install `google-api-python-client google-auth-oauthlib` to use Gmail OAuth2.")
            elif not CREDENTIALS_FILE.exists():
                st.warning("Upload your `credentials.json` from Google Cloud Console to proceed.")
                uploaded = st.file_uploader("Upload credentials.json", type="json", key="setup_creds_upload")
                if uploaded:
                    CREDENTIALS_FILE.write_bytes(uploaded.read())
                    st.success("Saved — click Sign in with Google below.")
                    st.rerun()
            else:
                st.success("✅ credentials.json found")
                if st.button("🔗 Sign in with Google", type="primary", key="setup_google_signin"):
                    with st.spinner("Opening Google sign-in in your browser..."):
                        try:
                            svc = get_gmail_service()
                            if svc:
                                cfg = {"type": "gmail_oauth", "provider": "Gmail (OAuth2)"}
                                save_mailbox_config(cfg)
                                st.session_state.mailbox_config = cfg
                                st.session_state.gmail_service = svc
                                st.session_state.calendar_service = get_calendar_service()
                                st.session_state.mailbox_configured = True
                                st.rerun()
                            else:
                                st.error("Sign-in failed. Check credentials.json.")
                        except Exception as e:
                            st.error(f"OAuth error: {e}")

        else:
            # ── IMAP / POP3 form ─────────────────────────────────────────────
            if "Gmail" in provider:
                st.info("💡 Gmail requires an **App Password** — go to myaccount.google.com → Security → App Passwords.")
            elif "Yahoo" in provider:
                st.info("💡 Yahoo requires an **App Password** — go to Account Security → Generate app password.")

            c1, c2 = st.columns(2)
            with c1:
                host = st.text_input("Incoming server (IMAP/POP3 host):", value=preset.get("host", ""), key="setup_host")
                port = st.number_input("Port:", value=int(preset.get("port", 993)), min_value=1, max_value=65535, key="setup_port")
                use_ssl = st.checkbox("SSL/TLS", value=preset.get("ssl", True), key="setup_ssl")
            with c2:
                smtp_host = st.text_input("Outgoing server (SMTP host):", value=preset.get("smtp_host", ""), key="setup_smtp_host")
                smtp_port = st.number_input("SMTP port:", value=int(preset.get("smtp_port", 587)), min_value=1, max_value=65535, key="setup_smtp_port")

            username = st.text_input("Email address:", key="setup_username")
            password = st.text_input("Password / App Password:", type="password", key="setup_password")

            btn_col1, btn_col2 = st.columns(2)
            with btn_col1:
                if st.button("🔌 Test Connection", key="setup_test"):
                    if host and username and password:
                        cfg = {
                            "type": preset["type"],
                            "provider": provider,
                            "host": host,
                            "port": int(port),
                            "ssl": use_ssl,
                            "smtp_host": smtp_host,
                            "smtp_port": int(smtp_port),
                            "username": username,
                            "password": password,
                        }
                        with st.spinner("Testing connection..."):
                            if preset["type"] == "imap":
                                ok, msg = test_imap_connection(cfg)
                            else:
                                ok, msg = test_pop3_connection(cfg)
                        st.session_state._test_result = (ok, msg)
                        st.rerun()
                    else:
                        st.warning("Fill in all fields before testing.")

            if st.session_state._test_result:
                ok, msg = st.session_state._test_result
                if ok:
                    st.success(msg)
                else:
                    st.error(msg)

            with btn_col2:
                if st.button("💾 Save & Connect", type="primary", key="setup_save"):
                    if host and username and password:
                        cfg = {
                            "type": preset["type"],
                            "provider": provider,
                            "host": host,
                            "port": int(port),
                            "ssl": use_ssl,
                            "smtp_host": smtp_host,
                            "smtp_port": int(smtp_port),
                            "username": username,
                            "password": password,
                        }
                        save_mailbox_config(cfg)
                        st.session_state.mailbox_config = cfg
                        st.session_state.mailbox_configured = True
                        st.session_state._test_result = None
                        st.rerun()
                    else:
                        st.warning("Fill in all required fields.")

    with col_info:
        st.markdown("#### What you'll need")
        st.markdown("""
**Gmail (OAuth2)**
- A `credentials.json` from Google Cloud Console
- Enable Gmail API and Calendar API
- Create OAuth 2.0 credentials (Desktop App)

**Gmail (IMAP)**
- 2-Factor Authentication enabled on your Google account
- An App Password (not your main password)

**Outlook / Microsoft 365**
- Your full email address and password
- IMAP must be enabled in Outlook settings

**Yahoo Mail**
- IMAP must be enabled in Yahoo account settings
- An App Password from Account Security

**iCloud Mail**
- Two-Factor Authentication enabled
- An App-Specific Password from appleid.apple.com

**Custom IMAP/POP3**
- Check your email provider's help center for server settings
        """)

        st.markdown("---")
        st.markdown("#### Or try Demo Mode")
        if st.button("📧 Skip setup — use demo emails", key="setup_demo"):
            cfg = {"type": "demo", "provider": "Demo"}
            save_mailbox_config(cfg)
            st.session_state.mailbox_config = cfg
            st.session_state.mailbox_configured = True
            st.session_state.raw_emails = _demo_emails()
            st.rerun()


# ─── Sidebar ─────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ✉️ AI Email Manager")
    st.markdown("---")

    # API keys
    with st.expander("🔑 API Keys", expanded=not st.session_state.anthropic_api_key):
        anthropic_key = st.text_input("Anthropic API Key", value=st.session_state.anthropic_api_key, type="password")
        openai_key = st.text_input("OpenAI API Key (voice)", value=st.session_state.openai_api_key, type="password")
        if st.button("Save Keys"):
            st.session_state.anthropic_api_key = anthropic_key
            st.session_state.openai_api_key = openai_key
            st.success("Keys saved.")

    st.markdown("---")

    # Mailbox status
    if st.session_state.mailbox_configured and st.session_state.mailbox_config:
        cfg = st.session_state.mailbox_config
        provider = cfg.get("provider", cfg.get("type", "Unknown"))
        mtype = cfg.get("type", "")

        if mtype == "gmail_oauth":
            icon = "📬"
            detail = "Gmail OAuth2"
            connected = st.session_state.gmail_service is not None
        elif mtype == "imap":
            icon = "📥"
            detail = cfg.get("username", "IMAP")
            connected = True
        elif mtype == "pop3":
            icon = "📨"
            detail = cfg.get("username", "POP3")
            connected = True
        elif mtype == "demo":
            icon = "🎭"
            detail = "Demo mode"
            connected = True
        else:
            icon = "📧"
            detail = provider
            connected = False

        status = "✅" if connected else "⚠️"
        st.markdown(f"**{icon} {provider}**")
        st.caption(f"{status} {detail}")

        if st.button("🔄 Fetch Emails"):
            with st.spinner("Fetching..."):
                if mtype == "demo":
                    st.session_state.raw_emails = _demo_emails()
                else:
                    st.session_state.raw_emails = fetch_emails_all(st.session_state.max_emails)
                st.session_state.briefing = None
            st.toast(f"Loaded {len(st.session_state.raw_emails)} emails")

        if st.button("🔁 Change Mailbox", key="change_mailbox"):
            st.session_state.mailbox_configured = False
            st.session_state.mailbox_config = None
            st.session_state.gmail_service = None
            st.session_state.raw_emails = []
            st.session_state.briefing = None
            if MAILBOX_CONFIG_FILE.exists():
                MAILBOX_CONFIG_FILE.unlink()
            st.rerun()
    else:
        st.info("No mailbox connected yet.")

    st.markdown("---")

    if st.button("📧 Load Demo Emails"):
        st.session_state.raw_emails = _demo_emails()
        st.session_state.briefing = None
        st.toast("Demo emails loaded — click Analyze in the main view.")

    # Follow-up reminders
    due = get_due_followups()
    if due:
        st.markdown("### ⏰ Follow-ups Due")
        for f in due[:3]:
            st.warning(f"**{f['subject']}** from {f['sender']}")


# ─── Route: setup vs main app ─────────────────────────────────────────────────
if not st.session_state.mailbox_configured:
    show_mailbox_setup()
    st.stop()


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN TABS
# ═══════════════════════════════════════════════════════════════════════════════
tab_inbox, tab_analytics, tab_crm, tab_persona, tab_settings = st.tabs([
    "📬 Visual Inbox",
    "📊 Analytics",
    "👥 CRM",
    "🎭 Persona",
    "⚙️ Settings",
])


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 1: VISUAL INBOX
# ═══════════════════════════════════════════════════════════════════════════════
with tab_inbox:
    col_main, col_detail = st.columns([3, 2])

    with col_main:
        st.markdown("### Morning Briefing")

        if st.session_state.raw_emails and st.session_state.briefing is None:
            if st.button("🤖 Analyze Emails with Claude", type="primary"):
                with st.spinner("Claude is categorizing your emails..."):
                    briefing = categorize_emails_with_claude(st.session_state.raw_emails)
                    st.session_state.briefing = briefing
                    for em in briefing.urgent_emails:
                        email_data = next(
                            (e for e in st.session_state.raw_emails if e["message_id"] == em.message_id), {}
                        )
                        if email_data:
                            generate_draft_background(email_data, st.session_state.persona)
                    for em_list in [briefing.urgent_emails, briefing.fyi_emails]:
                        for em in em_list:
                            email_addr = re.findall(r"<([^>]+)>", em.sender)
                            addr = email_addr[0] if email_addr else em.sender
                            name = em.sender.split("<")[0].strip()
                            update_contact(addr, name)
                    record_stat("processed")
                    st.rerun()
        elif not st.session_state.raw_emails:
            st.info("Fetch emails using the sidebar, or load demo emails.")

        if st.session_state.briefing:
            briefing = st.session_state.briefing
            b = briefing

            c1, c2, c3 = st.columns(3)
            c1.metric("🔴 Urgent", len(b.urgent_emails))
            c2.metric("🟡 FYI", len(b.fyi_emails))
            c3.metric("⚪ Noise", len(b.noise_emails))

            if st.button("🔊 Listen to Morning Briefing"):
                text = generate_morning_briefing_text(briefing)
                audio = text_to_speech(text)
                if audio:
                    st.audio(audio, format="audio/mp3")
                else:
                    st.info(text)

            st.plotly_chart(build_treemap(briefing), use_container_width=True, key="treemap")

            for cat_name, emails in [
                ("🔴 Urgent", b.urgent_emails),
                ("🟡 For Your Info", b.fyi_emails),
                ("⚪ Noise", b.noise_emails),
            ]:
                if emails:
                    with st.expander(f"{cat_name} ({len(emails)})", expanded=cat_name.startswith("🔴")):
                        for em in emails:
                            col_a, col_b, col_c = st.columns([4, 1, 1])
                            with col_a:
                                if st.button(
                                    f"**{em.subject[:40]}**\n{em.sender[:30]}",
                                    key=f"sel_{em.message_id}",
                                ):
                                    orig = next(
                                        (e for e in st.session_state.raw_emails if e["message_id"] == em.message_id), {}
                                    )
                                    st.session_state.selected_email = {**orig, **em.model_dump()}
                                    st.session_state.reply_draft = get_draft(em.message_id) or ""
                                    st.rerun()
                            with col_b:
                                if st.button("💤", key=f"snooze_{em.message_id}", help="Snooze 4h"):
                                    snooze_email(em.message_id, em.subject, em.sender)
                                    record_stat("snoozed")
                                    st.toast("Snoozed for 4 hours")
                            with col_c:
                                if st.button("📥", key=f"arch_{em.message_id}", help="Archive"):
                                    imap_uid = next(
                                        (e.get("_imap_uid", "") for e in st.session_state.raw_emails
                                         if e["message_id"] == em.message_id), ""
                                    )
                                    archive_email_any(em.message_id, imap_uid)
                                    record_stat("archived")
                                    st.toast("Archived")
                            st.caption(em.one_line_summary)
                            st.markdown("---")

    with col_detail:
        sel = st.session_state.selected_email
        if sel:
            st.markdown(f"### {sel.get('subject', '')}")
            st.caption(f"From: {sel.get('sender', '')} | {sel.get('date', '')}")
            st.markdown(sel.get("body", sel.get("snippet", "")))
            st.markdown("---")

            action_cols = st.columns(4)
            with action_cols[0]:
                if st.button("📅 Follow-up", key="followup_btn"):
                    add_followup(sel["message_id"], sel.get("subject", ""), sel.get("sender", ""))
                    st.toast("Follow-up set for 2 days")
            with action_cols[1]:
                if st.button("💤 Snooze", key="snooze_detail"):
                    snooze_email(sel["message_id"], sel.get("subject", ""), sel.get("sender", ""))
                    st.toast("Snoozed")
            with action_cols[2]:
                unsub = sel.get("list_unsubscribe", "")
                if unsub and st.button("🚫 Unsub", key="unsub_btn"):
                    st.info(try_unsubscribe(unsub))
            with action_cols[3]:
                if st.button("📆 Meeting", key="meeting_btn"):
                    if st.session_state.calendar_service:
                        times = suggest_meeting_times(
                            st.session_state.calendar_service,
                            sel.get("body", sel.get("snippet", "")),
                        )
                        st.info(times)
                    else:
                        st.warning("Connect Google Calendar first.")

            st.markdown("#### Reply")

            if AUDIO_RECORDER_AVAILABLE:
                audio_data = st_audiorec()
                if audio_data:
                    transcribed = speech_to_text(audio_data)
                    if transcribed:
                        st.session_state.reply_draft = transcribed
                        st.toast("Transcribed!")

            draft_instruction = st.text_input("Special instruction (optional):", key="draft_instruction")
            if not st.session_state.reply_draft:
                if st.button("✍️ Draft Reply with Claude"):
                    with st.spinner("Drafting..."):
                        draft = draft_reply_with_claude(sel, st.session_state.persona, draft_instruction)
                        st.session_state.reply_draft = draft
                        st.rerun()

            reply_text = st.text_area("Reply:", value=st.session_state.reply_draft, height=200, key="reply_area")

            send_col1, send_col2 = st.columns(2)
            with send_col1:
                if st.button("📤 Send Reply", type="primary"):
                    if reply_text:
                        try:
                            send_email_any(
                                to=sel.get("sender", ""),
                                subject=f"Re: {sel.get('subject', '')}",
                                body=reply_text,
                                thread_id=sel.get("thread_id", ""),
                                in_reply_to=sel.get("message_id", ""),
                            )
                            record_stat("replied")
                            st.success("Sent!")
                            st.session_state.selected_email = None
                            st.session_state.reply_draft = ""
                        except Exception as e:
                            st.error(f"Send failed: {e}")
                    else:
                        st.warning("Reply is empty.")
            with send_col2:
                if st.button("🎙️ Read Reply Aloud"):
                    audio = text_to_speech(reply_text)
                    if audio:
                        st.audio(audio, format="audio/mp3")

            email_addr = re.findall(r"<([^>]+)>", sel.get("sender", ""))
            addr = email_addr[0] if email_addr else sel.get("sender", "")
            contact = get_contact(addr)
            if contact:
                st.markdown("---")
                st.markdown("#### 👤 Contact")
                st.caption(f"**{contact['name']}** | {contact['email']}")
                st.caption(f"Last contact: {contact['last_contact']} | {contact['email_count']} emails")
        else:
            st.info("Select an email from the left to read and reply.")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 2: ANALYTICS
# ═══════════════════════════════════════════════════════════════════════════════
with tab_analytics:
    st.markdown("### 📊 Email Performance Dashboard")

    stats = get_stats(30)
    if stats:
        df = pd.DataFrame(stats)
        df["date"] = pd.to_datetime(df["date"])

        col1, col2 = st.columns(2)
        with col1:
            fig = px.bar(df, x="date", y=["processed", "replied", "archived", "snoozed"],
                         title="Email Activity (Last 30 Days)", barmode="group")
            st.plotly_chart(fig, use_container_width=True)
        with col2:
            totals = df[["processed", "replied", "archived", "snoozed"]].sum()
            fig2 = go.Figure(go.Pie(labels=totals.index, values=totals.values))
            fig2.update_layout(title="Action Distribution")
            st.plotly_chart(fig2, use_container_width=True)

        reply_rate = (totals["replied"] / totals["processed"] * 100) if totals["processed"] else 0
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Emails Processed", int(totals["processed"]))
        m2.metric("Replies Sent", int(totals["replied"]))
        m3.metric("Reply Rate", f"{reply_rate:.1f}%")
        m4.metric("Snoozed", int(totals["snoozed"]))
    else:
        st.info("Process some emails to see analytics here.")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 3: CRM
# ═══════════════════════════════════════════════════════════════════════════════
with tab_crm:
    st.markdown("### 👥 Smart Contact Panel")
    contacts = get_all_contacts()
    if contacts:
        df_contacts = pd.DataFrame(contacts)
        st.dataframe(df_contacts, use_container_width=True)

        selected_contact = st.selectbox("Select contact for details:", [c["email"] for c in contacts])
        if selected_contact:
            c = get_contact(selected_contact)
            st.markdown(f"**{c['name']}** ({c['email']})")
            st.caption(f"Last contact: {c['last_contact']} | Total emails: {c['email_count']}")
            notes = st.text_area("Notes:", value=c.get("notes", ""), key="contact_notes")
            if st.button("Save Notes"):
                conn = sqlite3.connect(DB_PATH)
                conn.execute("UPDATE contacts SET notes=? WHERE email=?", (notes, selected_contact))
                conn.commit()
                conn.close()
                st.success("Saved")
    else:
        st.info("CRM will populate as you process emails.")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 4: PERSONA
# ═══════════════════════════════════════════════════════════════════════════════
with tab_persona:
    st.markdown("### 🎭 Writing Style Trainer")
    st.markdown("Paste samples of your email writing so Claude can learn your style.")

    col_p1, col_p2 = st.columns(2)
    with col_p1:
        sample_text = st.text_area(
            "Paste your email samples here (separate multiple with '---'):",
            height=250,
            placeholder="Hi Alice,\n\nThanks for the update. I'll review by EOD.\n\nBest,\nMe\n---\nHello team,\n..."
        )
        if st.button("🤖 Analyze My Writing Style"):
            if sample_text:
                samples = [s.strip() for s in sample_text.split("---") if s.strip()]
                with st.spinner("Claude is learning your style..."):
                    persona = analyze_writing_style(samples)
                    st.session_state.persona = persona
                    save_persona(persona)
                    st.success("Style profile saved!")
            else:
                st.warning("Please paste some email samples first.")

    with col_p2:
        if st.session_state.persona:
            p = st.session_state.persona
            st.markdown("#### Current Style Profile")
            st.json(p)
            st.markdown("#### Manual Overrides")
            new_tone = st.text_input("Tone:", value=p.get("tone", "professional"))
            new_greeting = st.text_input("Greeting:", value=p.get("greeting", "Hi,"))
            new_closing = st.text_input("Closing:", value=p.get("closing", "Best regards,"))
            if st.button("Save Manual Profile"):
                st.session_state.persona.update({"tone": new_tone, "greeting": new_greeting, "closing": new_closing})
                save_persona(st.session_state.persona)
                st.success("Profile updated!")
        else:
            st.info("No style profile yet. Paste email samples and click 'Analyze'.")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 5: SETTINGS
# ═══════════════════════════════════════════════════════════════════════════════
with tab_settings:
    st.markdown("### ⚙️ Settings")

    # Current mailbox
    st.markdown("#### Current Mailbox")
    cfg = st.session_state.mailbox_config or {}
    st.json({"provider": cfg.get("provider", "—"), "type": cfg.get("type", "—"),
             "host": cfg.get("host", "—"), "username": cfg.get("username", "—")})

    if st.button("🔁 Change Mailbox", key="settings_change_mailbox"):
        st.session_state.mailbox_configured = False
        st.session_state.mailbox_config = None
        st.session_state.gmail_service = None
        st.session_state.raw_emails = []
        st.session_state.briefing = None
        if MAILBOX_CONFIG_FILE.exists():
            MAILBOX_CONFIG_FILE.unlink()
        st.rerun()

    st.markdown("---")
    st.markdown("#### Gmail Credentials (OAuth2)")
    if CREDENTIALS_FILE.exists():
        st.success("✅ credentials.json found")
    else:
        uploaded = st.file_uploader("Upload credentials.json", type="json")
        if uploaded:
            CREDENTIALS_FILE.write_bytes(uploaded.read())
            st.success("Saved! Reconnect from Change Mailbox.")

    st.markdown("---")
    st.markdown("#### Fetch Settings")
    st.session_state.max_emails = st.slider("Max emails to fetch", 10, 100, st.session_state.max_emails)

    st.markdown("---")
    st.markdown("#### Clear Data")
    if st.button("🗑️ Clear All Local Data", type="secondary"):
        for f in [DB_PATH, TOKEN_FILE, MAILBOX_CONFIG_FILE, PERSONA_FILE]:
            if f.exists():
                f.unlink()
        init_db()
        st.session_state.clear()
        st.success("All data cleared.")
        st.rerun()

    st.markdown("---")
    st.markdown("#### About")
    st.markdown("""
**AI Email Manager** — Superhuman-style email management powered by:
- **Claude Opus 4.8** (`claude-opus-4-8`, Anthropic) for AI categorization, summarization & reply drafting
- **Gmail API** / **IMAP** / **POP3** for live email access
- **SMTP** for sending from any provider
- **OpenAI TTS/Whisper** for voice features
- **Google Calendar API** for meeting scheduling
- **Plotly** for visual treemap inbox
    """)
