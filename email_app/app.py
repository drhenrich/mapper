"""
Superhuman-style AI Email Manager
- Gmail OAuth2 for live email access
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
import threading
import concurrent.futures
import time
import re
import tempfile
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Optional

import streamlit as st
import plotly.graph_objects as go
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
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.readonly",
]
DB_PATH = Path(__file__).parent / "email_app.db"
PERSONA_FILE = Path(__file__).parent / "persona.json"
TOKEN_FILE = Path(__file__).parent / "token.json"
CREDENTIALS_FILE = Path(__file__).parent / "credentials.json"

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


# ─── Gmail OAuth2 ────────────────────────────────────────────────────────────
def get_gmail_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
        elif CREDENTIALS_FILE.exists():
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        else:
            return None
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def get_calendar_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        return None
    return build("calendar", "v3", credentials=creds)


def fetch_emails(service, max_results=50):
    """Fetch recent unread emails from Gmail."""
    result = service.users().messages().list(
        userId="me",
        labelIds=["INBOX"],
        q="is:unread",
        maxResults=max_results,
    ).execute()
    messages = result.get("messages", [])
    emails = []
    for msg in messages:
        detail = service.users().messages().get(
            userId="me", id=msg["id"], format="full"
        ).execute()
        headers = {h["name"]: h["value"] for h in detail["payload"]["headers"]}
        body = _extract_body(detail["payload"])
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


def _extract_body(payload):
    """Recursively extract plain-text body from Gmail payload."""
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore") if data else ""
    if "parts" in payload:
        for part in payload["parts"]:
            text = _extract_body(part)
            if text:
                return text
    return ""


def send_email(service, to: str, subject: str, body: str, thread_id: str = "", in_reply_to: str = ""):
    """Send an email (optionally as a reply)."""
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


def archive_email(service, message_id: str):
    service.users().messages().modify(
        userId="me", id=message_id, body={"removeLabelIds": ["INBOX"]}
    ).execute()


def mark_read(service, message_id: str):
    service.users().messages().modify(
        userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}
    ).execute()


# ─── Claude AI ───────────────────────────────────────────────────────────────
def get_anthropic_client():
    api_key = os.environ.get("ANTHROPIC_API_KEY") or st.session_state.get("anthropic_api_key", "")
    if not api_key:
        return None
    return anthropic.Anthropic(api_key=api_key)


def categorize_emails_with_claude(emails: list[dict]) -> MorningBriefing:
    """Use Claude to categorize and summarize emails into Urgent/FYI/Noise."""
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
        messages=[
            {
                "role": "user",
                "content": f"""You are an expert email triage assistant. Analyze these emails and categorize each as:
- Urgent: requires immediate action (deadlines, important questions from key people, time-sensitive)
- FYI: informational, read later (newsletters you care about, CC emails, updates)
- Noise: can be archived/ignored (promotions, spam, irrelevant notifications)

For each email provide a one-line summary and (for Urgent) a clear action item.

Emails to analyze:
{email_list}

Return the full structured categorization.""",
            }
        ],
    )

    data = json.loads(response.content[0].text if hasattr(response.content[0], "text") else response.content[-1].text)
    briefing = MorningBriefing(**data)
    # Attach snippet/to from original emails
    id_map = {e["message_id"]: e for e in emails}
    for email_list_attr in [briefing.urgent_emails, briefing.fyi_emails, briefing.noise_emails]:
        for em in email_list_attr:
            orig = id_map.get(em.message_id, {})
            em.snippet = orig.get("snippet", "")
            em.to = orig.get("to", "")
    return briefing


def draft_reply_with_claude(email: dict, persona: dict, instruction: str = "") -> str:
    """Draft an email reply using Claude, adapting to the user's writing style."""
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
    # Extract text from response (skip thinking blocks)
    for block in response.content:
        if hasattr(block, "text"):
            return block.text
    return ""


def generate_morning_briefing_text(briefing: MorningBriefing) -> str:
    """Generate a spoken morning briefing from the categorized emails."""
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
        messages=[
            {
                "role": "user",
                "content": f"""Create a concise 30-second spoken morning email briefing.

Stats: {urgent_count} urgent, {fyi_count} for your info, {noise_count} noise emails.

Urgent items:
{urgent_summary or "None"}

Write a natural, spoken-word briefing (no markdown, no lists — just flowing speech). Keep it under 100 words.""",
            }
        ],
    )
    for block in response.content:
        if hasattr(block, "text"):
            return block.text
    return "Good morning! You have emails to review."


def analyze_writing_style(sample_emails: list[str]) -> dict:
    """Analyze writing samples to build a persona profile."""
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
        messages=[
            {
                "role": "user",
                "content": f"""Analyze these email writing samples and extract the writing style profile.

Samples:
{samples}

Identify: tone (formal/informal/friendly), typical greeting phrase, typical closing phrase, average email length (brief/moderate/detailed), and key style notes.""",
            }
        ],
    )
    for block in response.content:
        if hasattr(block, "text"):
            return json.loads(block.text)
    return {}


def get_calendar_free_slots(calendar_service, days_ahead: int = 5) -> list[dict]:
    """Find free slots in Google Calendar for the next N days."""
    if not calendar_service:
        return []
    now = datetime.utcnow().isoformat() + "Z"
    end = (datetime.utcnow() + timedelta(days=days_ahead)).isoformat() + "Z"
    events_result = calendar_service.events().list(
        calendarId="primary",
        timeMin=now,
        timeMax=end,
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    events = events_result.get("items", [])
    busy_slots = []
    for event in events:
        start = event["start"].get("dateTime", event["start"].get("date"))
        end_ev = event["end"].get("dateTime", event["end"].get("date"))
        busy_slots.append({"start": start, "end": end_ev, "title": event.get("summary", "Busy")})
    return busy_slots


def suggest_meeting_times(calendar_service, email_body: str) -> str:
    """Use Claude + Calendar to suggest meeting times."""
    busy = get_calendar_free_slots(calendar_service)
    client = get_anthropic_client()
    if not client:
        return "Could not suggest meeting times."

    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=256,
        messages=[
            {
                "role": "user",
                "content": f"""The user received this email requesting a meeting:
{email_body}

Their calendar shows these busy slots:
{json.dumps(busy, indent=2)}

Suggest 3 specific meeting times in the next 5 days during business hours (9am-6pm) that don't conflict with busy slots. Be concise and natural.""",
            }
        ],
    )
    for block in response.content:
        if hasattr(block, "text"):
            return block.text
    return ""


# ─── Voice features (OpenAI audio) ───────────────────────────────────────────
def text_to_speech(text: str) -> Optional[bytes]:
    """Convert text to speech using OpenAI TTS."""
    if not OPENAI_AVAILABLE:
        return None
    api_key = os.environ.get("OPENAI_API_KEY") or st.session_state.get("openai_api_key", "")
    if not api_key:
        return None
    openai_client = openai.OpenAI(api_key=api_key)
    response = openai_client.audio.speech.create(
        model="tts-1",
        voice="alloy",
        input=text,
    )
    return response.content


def speech_to_text(audio_bytes: bytes) -> str:
    """Transcribe audio using OpenAI Whisper."""
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
    conn.execute(
        "INSERT OR REPLACE INTO snooze VALUES (?, ?, ?, ?)",
        (message_id, subject, sender, snooze_until),
    )
    conn.commit()
    conn.close()


def add_followup(message_id: str, subject: str, sender: str, days: int = 2, note: str = ""):
    followup_date = (datetime.now() + timedelta(days=days)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO followups VALUES (?, ?, ?, ?, ?)",
        (message_id, subject, sender, followup_date, note),
    )
    conn.commit()
    conn.close()


def get_due_followups() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT * FROM followups WHERE followup_date <= ?",
        (datetime.now().isoformat(),),
    ).fetchall()
    conn.close()
    return [{"message_id": r[0], "subject": r[1], "sender": r[2], "date": r[3], "note": r[4]} for r in rows]


def record_stat(field: str):
    today = datetime.now().date().isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        f"INSERT OR IGNORE INTO stats (date) VALUES (?)", (today,)
    )
    conn.execute(f"UPDATE stats SET {field} = {field} + 1 WHERE date = ?", (today,))
    conn.commit()
    conn.close()


def get_stats(days: int = 30) -> list[dict]:
    since = (datetime.now() - timedelta(days=days)).date().isoformat()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT date, processed, replied, snoozed, archived FROM stats WHERE date >= ? ORDER BY date",
        (since,),
    ).fetchall()
    conn.close()
    return [{"date": r[0], "processed": r[1], "replied": r[2], "snoozed": r[3], "archived": r[4]} for r in rows]


# ─── CRM / Contacts ──────────────────────────────────────────────────────────
def update_contact(email: str, name: str):
    today = datetime.now().date().isoformat()
    conn = sqlite3.connect(DB_PATH)
    existing = conn.execute("SELECT email_count FROM contacts WHERE email = ?", (email,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE contacts SET name=?, last_contact=?, email_count=email_count+1 WHERE email=?",
            (name, today, email),
        )
    else:
        conn.execute("INSERT INTO contacts VALUES (?, ?, ?, 1, ?)", (email, name, today, ""))
    conn.commit()
    conn.close()


def get_contact(email: str) -> dict:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT * FROM contacts WHERE email = ?", (email,)).fetchone()
    conn.close()
    if row:
        return {"email": row[0], "name": row[1], "last_contact": row[2], "email_count": row[3], "notes": row[4]}
    return {}


def get_all_contacts() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT * FROM contacts ORDER BY email_count DESC LIMIT 50").fetchall()
    conn.close()
    return [{"email": r[0], "name": r[1], "last_contact": r[2], "email_count": r[3], "notes": r[4]} for r in rows]


# ─── Background draft generation ─────────────────────────────────────────────
_draft_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)


def generate_draft_background(email: dict, persona: dict):
    """Generate a draft in the background and store it in DB."""
    def _run():
        draft = draft_reply_with_claude(email, persona)
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT OR REPLACE INTO drafts VALUES (?, ?, ?)",
            (email["message_id"], draft, datetime.now().isoformat()),
        )
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
    """Attempt to unsubscribe via List-Unsubscribe header."""
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


# ─── Plotly Treemap ──────────────────────────────────────────────────────────
def build_treemap(briefing: MorningBriefing) -> go.Figure:
    labels = ["Inbox"]
    parents = [""]
    values = [0]
    colors = ["#1e1e2e"]
    custom = [""]

    categories = [
        ("Urgent", briefing.urgent_emails, "#e74c3c"),
        ("FYI", briefing.fyi_emails, "#f39c12"),
        ("Noise", briefing.noise_emails, "#95a5a6"),
    ]

    for cat_name, emails, color in categories:
        if not emails:
            continue
        labels.append(cat_name)
        parents.append("Inbox")
        values.append(len(emails))
        colors.append(color)
        custom.append(f"{len(emails)} emails")

        for em in emails:
            label = f"{em.sender[:20]}\n{em.subject[:30]}"
            labels.append(label)
            parents.append(cat_name)
            # Weight: Urgent=3, FYI=2, Noise=1
            weight = {"Urgent": 3, "FYI": 2, "Noise": 1}.get(cat_name, 1)
            values.append(weight)
            colors.append(color)
            custom.append(f"{em.one_line_summary}<br>ID: {em.message_id}")

    fig = go.Figure(go.Treemap(
        labels=labels,
        parents=parents,
        values=values,
        marker=dict(colors=colors, line=dict(width=2, color="#0d0d1a")),
        customdata=custom,
        hovertemplate="<b>%{label}</b><br>%{customdata}<extra></extra>",
        textfont=dict(color="white", size=11),
    ))
    fig.update_layout(
        margin=dict(t=10, l=0, r=0, b=0),
        paper_bgcolor="#0d0d1a",
        plot_bgcolor="#0d0d1a",
        height=450,
    )
    return fig


# ─── Session state init ──────────────────────────────────────────────────────
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
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


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

    # Gmail connection
    if GOOGLE_AVAILABLE:
        if st.session_state.gmail_service is None:
            if CREDENTIALS_FILE.exists():
                if st.button("🔗 Connect Gmail"):
                    with st.spinner("Connecting to Gmail..."):
                        svc = get_gmail_service()
                        if svc:
                            st.session_state.gmail_service = svc
                            st.session_state.calendar_service = get_calendar_service()
                            st.success("Gmail connected!")
                        else:
                            st.error("Could not connect. Check credentials.json")
            else:
                st.info("Place `credentials.json` in the `email_app/` folder to enable Gmail.")
        else:
            st.success("✅ Gmail connected")
            if st.button("🔄 Refresh Emails"):
                with st.spinner("Fetching emails..."):
                    st.session_state.raw_emails = fetch_emails(st.session_state.gmail_service)
                    st.session_state.briefing = None  # reset to re-analyze
    else:
        st.warning("Install google-api-python-client for Gmail integration.")

    st.markdown("---")

    # Demo mode
    if st.button("📧 Load Demo Emails"):
        st.session_state.raw_emails = _demo_emails()
        st.session_state.briefing = None
        st.info("Demo emails loaded. Click 'Analyze' in the main view.")

    # Follow-up reminders
    due = get_due_followups()
    if due:
        st.markdown("### ⏰ Follow-ups Due")
        for f in due[:3]:
            st.warning(f"**{f['subject']}** from {f['sender']}")


def _demo_emails() -> list[dict]:
    return [
        {
            "message_id": "demo_1",
            "thread_id": "thread_1",
            "subject": "Q4 Budget Approval Needed by EOD",
            "sender": "CFO <cfo@company.com>",
            "to": "me@example.com",
            "date": datetime.now().strftime("%a, %d %b %Y %H:%M:%S"),
            "snippet": "We need your sign-off on the Q4 budget by end of day today...",
            "body": "Hi, We need your sign-off on the Q4 budget proposal by end of day today. The board meeting is tomorrow morning at 9am. Please review and approve or send your comments. Best, CFO",
            "list_unsubscribe": "",
            "in_reply_to": "",
        },
        {
            "message_id": "demo_2",
            "thread_id": "thread_2",
            "subject": "Team lunch tomorrow?",
            "sender": "Alice <alice@team.com>",
            "to": "me@example.com",
            "date": datetime.now().strftime("%a, %d %b %Y %H:%M:%S"),
            "snippet": "Hey, are you free for team lunch tomorrow around noon?",
            "body": "Hey, are you free for team lunch tomorrow around noon? We're thinking of trying that new Thai place. Let me know! Alice",
            "list_unsubscribe": "",
            "in_reply_to": "",
        },
        {
            "message_id": "demo_3",
            "thread_id": "thread_3",
            "subject": "Your Weekly Newsletter - Top Tech Stories",
            "sender": "TechDigest <news@techdigest.com>",
            "to": "me@example.com",
            "date": datetime.now().strftime("%a, %d %b %Y %H:%M:%S"),
            "snippet": "This week in tech: AI breakthroughs, new smartphone releases...",
            "body": "This week in tech: AI breakthroughs, new smartphone releases, and market updates. Click to read more.",
            "list_unsubscribe": "<https://techdigest.com/unsubscribe?token=abc123>",
            "in_reply_to": "",
        },
        {
            "message_id": "demo_4",
            "thread_id": "thread_4",
            "subject": "Critical bug in production - needs immediate fix",
            "sender": "DevOps <devops@company.com>",
            "to": "me@example.com",
            "date": datetime.now().strftime("%a, %d %b %Y %H:%M:%S"),
            "snippet": "We have a P0 bug causing payment failures. Need your input on the fix...",
            "body": "URGENT: Payment processing is failing for 15% of users due to a database timeout issue. We've identified the root cause and need your approval to push the hotfix to production. Response needed ASAP.",
            "list_unsubscribe": "",
            "in_reply_to": "",
        },
        {
            "message_id": "demo_5",
            "thread_id": "thread_5",
            "subject": "50% Off - Flash Sale Today Only!",
            "sender": "Deals <deals@shopexample.com>",
            "to": "me@example.com",
            "date": datetime.now().strftime("%a, %d %b %Y %H:%M:%S"),
            "snippet": "Don't miss our biggest sale of the year!",
            "body": "FLASH SALE: 50% off all items today only! Use code SAVE50 at checkout.",
            "list_unsubscribe": "<https://shopexample.com/unsubscribe>",
            "in_reply_to": "",
        },
        {
            "message_id": "demo_6",
            "thread_id": "thread_6",
            "subject": "Project Alpha - Milestone Review",
            "sender": "Project Manager <pm@partner.com>",
            "to": "me@example.com",
            "date": datetime.now().strftime("%a, %d %b %Y %H:%M:%S"),
            "snippet": "Sharing the milestone review for Project Alpha. Everything on track.",
            "body": "Hi, Attached is the Q3 milestone review for Project Alpha. We're 95% on track. No blockers at the moment. Will share the full report in the Friday all-hands. Thanks, PM",
            "list_unsubscribe": "",
            "in_reply_to": "",
        },
    ]


# ─── Main tabs ───────────────────────────────────────────────────────────────
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
                    # Auto-generate drafts for urgent emails
                    for em in briefing.urgent_emails:
                        email_data = next(
                            (e for e in st.session_state.raw_emails if e["message_id"] == em.message_id), {}
                        )
                        if email_data:
                            generate_draft_background(email_data, st.session_state.persona)
                    # Update CRM contacts
                    for em_list in [briefing.urgent_emails, briefing.fyi_emails]:
                        for em in em_list:
                            email_addr = re.findall(r"<([^>]+)>", em.sender)
                            addr = email_addr[0] if email_addr else em.sender
                            name = em.sender.split("<")[0].strip()
                            update_contact(addr, name)
                    record_stat("processed")
                    st.rerun()
        elif not st.session_state.raw_emails:
            st.info("Load emails using the sidebar — connect Gmail or use demo emails.")

        if st.session_state.briefing:
            briefing = st.session_state.briefing
            b = briefing

            # Stats row
            c1, c2, c3 = st.columns(3)
            c1.metric("🔴 Urgent", len(b.urgent_emails))
            c2.metric("🟡 FYI", len(b.fyi_emails))
            c3.metric("⚪ Noise", len(b.noise_emails))

            # Morning briefing audio
            briefing_col1, briefing_col2 = st.columns([2, 1])
            with briefing_col1:
                if st.button("🔊 Listen to Morning Briefing"):
                    text = generate_morning_briefing_text(briefing)
                    audio = text_to_speech(text)
                    if audio:
                        st.audio(audio, format="audio/mp3")
                    else:
                        st.info(text)

            # Treemap
            fig = build_treemap(briefing)
            clicked = st.plotly_chart(fig, use_container_width=True, key="treemap")

            # Email lists
            for cat_name, emails, emoji in [
                ("🔴 Urgent", b.urgent_emails, "🔴"),
                ("🟡 For Your Info", b.fyi_emails, "🟡"),
                ("⚪ Noise", b.noise_emails, "⚪"),
            ]:
                if emails:
                    with st.expander(f"{cat_name} ({len(emails)})", expanded=(cat_name.startswith("🔴"))):
                        for em in emails:
                            col_a, col_b, col_c = st.columns([4, 1, 1])
                            with col_a:
                                if st.button(
                                    f"**{em.subject[:40]}**\n{em.sender[:30]}",
                                    key=f"sel_{em.message_id}",
                                ):
                                    orig = next(
                                        (e for e in st.session_state.raw_emails if e["message_id"] == em.message_id),
                                        {},
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
                                    if st.session_state.gmail_service:
                                        archive_email(st.session_state.gmail_service, em.message_id)
                                        record_stat("archived")
                                    st.toast("Archived")
                            st.caption(f"{em.one_line_summary}")
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
                    result = try_unsubscribe(unsub)
                    st.info(result)
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

            # Voice input
            if AUDIO_RECORDER_AVAILABLE:
                audio_data = st_audiorec()
                if audio_data:
                    transcribed = speech_to_text(audio_data)
                    if transcribed:
                        st.session_state.reply_draft = transcribed
                        st.toast("Transcribed!")

            # Draft reply
            if not st.session_state.reply_draft:
                if st.button("✍️ Draft Reply with Claude"):
                    with st.spinner("Drafting..."):
                        instruction = st.text_input("Special instruction (optional):", key="draft_instruction")
                        draft = draft_reply_with_claude(sel, st.session_state.persona, instruction)
                        st.session_state.reply_draft = draft

            reply_text = st.text_area(
                "Reply:",
                value=st.session_state.reply_draft,
                height=200,
                key="reply_area",
            )

            send_col1, send_col2 = st.columns(2)
            with send_col1:
                if st.button("📤 Send Reply", type="primary"):
                    if st.session_state.gmail_service and reply_text:
                        send_email(
                            st.session_state.gmail_service,
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
                    elif not st.session_state.gmail_service:
                        st.warning("Gmail not connected.")
            with send_col2:
                if st.button("🎙️ Read Reply Aloud"):
                    audio = text_to_speech(reply_text)
                    if audio:
                        st.audio(audio, format="audio/mp3")

            # Contact info
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
        import plotly.express as px
        import pandas as pd

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
        import pandas as pd
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
        else:
            st.info("No style profile yet. Paste email samples and click 'Analyze'.")

        st.markdown("#### Manual Overrides")
        if st.session_state.persona:
            p = st.session_state.persona
            new_tone = st.text_input("Tone:", value=p.get("tone", "professional"))
            new_greeting = st.text_input("Greeting:", value=p.get("greeting", "Hi,"))
            new_closing = st.text_input("Closing:", value=p.get("closing", "Best regards,"))
            if st.button("Save Manual Profile"):
                st.session_state.persona.update({
                    "tone": new_tone,
                    "greeting": new_greeting,
                    "closing": new_closing,
                })
                save_persona(st.session_state.persona)
                st.success("Profile updated!")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 5: SETTINGS
# ═══════════════════════════════════════════════════════════════════════════════
with tab_settings:
    st.markdown("### ⚙️ Settings")

    st.markdown("#### Gmail Credentials")
    st.markdown("""
    1. Go to [Google Cloud Console](https://console.cloud.google.com)
    2. Create a project and enable Gmail API + Google Calendar API
    3. Create OAuth 2.0 credentials (Desktop App)
    4. Download `credentials.json` and place it in the `email_app/` folder
    5. Click "Connect Gmail" in the sidebar
    """)

    if CREDENTIALS_FILE.exists():
        st.success("✅ credentials.json found")
    else:
        uploaded = st.file_uploader("Upload credentials.json", type="json")
        if uploaded:
            CREDENTIALS_FILE.write_bytes(uploaded.read())
            st.success("Saved! Reconnect Gmail from the sidebar.")

    st.markdown("---")
    st.markdown("#### Email Fetch Settings")
    max_emails = st.slider("Max emails to fetch", 10, 100, 50)

    st.markdown("---")
    st.markdown("#### Snooze Defaults")
    snooze_hours = st.select_slider("Default snooze duration", [1, 2, 4, 8, 24], value=4)

    st.markdown("---")
    st.markdown("#### Clear Data")
    if st.button("🗑️ Clear All Local Data", type="secondary"):
        if DB_PATH.exists():
            DB_PATH.unlink()
            init_db()
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()
        st.session_state.clear()
        st.success("All data cleared.")
        st.rerun()

    st.markdown("---")
    st.markdown("#### About")
    st.markdown("""
    **AI Email Manager** — Superhuman-style email management powered by:
    - **Claude claude-opus-4-8** (Anthropic) for AI categorization, summarization & reply drafting
    - **Gmail API** for live email access
    - **OpenAI TTS/Whisper** for voice features
    - **Google Calendar API** for meeting scheduling
    - **Plotly** for visual treemap inbox
    """)
