# AI Email Manager

A Superhuman-style AI email management app built with Streamlit and Claude.

## Features

- **AI Email Briefing**: Claude claude-opus-4-8 categorizes emails into Urgent / FYI / Noise
- **Visual Treemap**: Plotly treemap showing email priority and weight at a glance
- **Voice Features**: TTS morning briefing (OpenAI TTS-1), STT reply dictation (Whisper)
- **AI Reply Drafting**: Claude drafts replies in your personal writing style
- **Persona Training**: Paste your email samples, Claude learns your tone and style
- **Gmail Integration**: Live email access, read, reply, archive via Gmail API
- **Snooze & Follow-ups**: SQLite-backed reminders
- **Analytics Dashboard**: 30-day email activity charts
- **Smart CRM**: Contact history panel auto-populated from your emails
- **Background Drafts**: Auto-generates reply drafts for urgent emails in the background
- **Google Calendar Agent**: Suggests meeting times based on your calendar availability
- **Newsletter Unsubscribe**: One-click unsubscribe via List-Unsubscribe headers

## Setup

### 1. Install dependencies

```bash
cd email_app
pip install -r requirements.txt
```

### 2. Configure API keys

Set environment variables or enter them in the app sidebar:

```bash
export ANTHROPIC_API_KEY=your_key_here
export OPENAI_API_KEY=your_key_here  # optional, for voice features
```

### 3. Gmail OAuth2

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a project, enable **Gmail API** and **Google Calendar API**
3. Create **OAuth 2.0 credentials** (Desktop App type)
4. Download `credentials.json` and place it in this `email_app/` folder
5. Run the app and click "Connect Gmail" in the sidebar — a browser will open for auth

### 4. Run

```bash
streamlit run app.py
```

## Usage

1. **Connect Gmail** (sidebar) or load **Demo Emails** to get started
2. Click **"Analyze Emails with Claude"** to get your AI briefing
3. Click any email in the treemap or list to open it
4. Use **"Draft Reply with Claude"** or dictate via microphone
5. Send directly from the app
6. Train your **Persona** in the Persona tab for better drafts

## Architecture

```
email_app/
├── app.py              # Main Streamlit application
├── requirements.txt    # Python dependencies
├── credentials.json    # Google OAuth2 credentials (you add this)
├── token.json          # Auto-generated after first Gmail login
├── persona.json        # Auto-saved writing style profile
└── email_app.db        # SQLite database (snooze, follow-ups, stats, CRM)
```
