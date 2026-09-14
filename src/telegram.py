"""Telegram two-way communication for dogood.

Outbound: notify Daniel about GitHub events.
Inbound:  pipe Daniel's messages to Claude Code for intelligent processing.
"""

import urllib.request
import subprocess
import urllib.parse
import json
import time
import re
import os
from datetime import datetime
from collections import deque
from pathlib import Path
from dotenv import load_dotenv

# Load .env file if it exists
load_dotenv(override=True)

DAEMON_MANAGER_DIR = Path.home() / "Library/Application Support/DaemonManager"
# Do Good talks to Daniel through this DaemonManager department bot.
NOTIFY_ROLE = os.environ.get("DOGOOD_TELEGRAM_ROLE", "philanthropy")


def _daemon_manager_credentials(role: str) -> tuple[str | None, str | None]:
    """Token from DaemonManager bots.json; chat id from that bot's launchd plist."""
    token = chat_id = None
    try:
        bots = json.loads((DAEMON_MANAGER_DIR / "bots.json").read_text())
        bot = next(b for b in bots if b.get("role") == role)
        token = bot.get("token")
        label = bot.get("launchdLabel")
        if label:
            import plistlib
            plist = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
            with open(plist, "rb") as f:
                chat_id = plistlib.load(f).get("EnvironmentVariables", {}).get("TELEGRAM_CHAT_ID")
    except Exception as e:
        print(f"  [TELEGRAM] DaemonManager '{role}' bot lookup failed: {e}", flush=True)
    return token, chat_id


_dm_token, _dm_chat = _daemon_manager_credentials(NOTIFY_ROLE)
BOT_TOKEN = _dm_token or os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = _dm_chat or os.environ.get("TELEGRAM_CHAT_ID")

if not BOT_TOKEN or not CHAT_ID:
    # If we're in a module that's imported, we might not want to exit immediately,
    # but for a daemon it's better to fail early with a clear message.
    print("ERROR: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in environment", flush=True)

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""
API_URL = f"{BASE_URL}/sendMessage" if BASE_URL else ""

PROJECT_DIR = "/Users/daniel/Library/Mobile Documents/com~apple~CloudDocs/Code/Tools/github-helper"

TELEGRAM_INBOX = "/tmp/telegram-inbox.jsonl"
TELEGRAM_OUTBOX = "/tmp/telegram-outbox.jsonl"


def _make_request(url: str, data: dict, timeout: int = 15) -> dict:
    """Helper to make urllib POST requests and return JSON dict."""
    try:
        req = urllib.request.Request(url, method="POST")
        req.add_header('Content-Type', 'application/json')
        payload = json.dumps(data).encode('utf-8')
        with urllib.request.urlopen(req, data=payload, timeout=timeout) as response:
            resp_body = response.read().decode('utf-8')
            return json.loads(resp_body)
    except urllib.error.HTTPError as e:
        print(f"  [TELEGRAM] HTTP Error {e.code}: {e.read().decode('utf-8', errors='ignore')}", flush=True)
    except Exception as e:
        print(f"  [TELEGRAM] Request exception: {e}", flush=True)
    return {}


# ---------------------------------------------------------------------------
# Outbound: send messages TO Daniel
# ---------------------------------------------------------------------------

def notify(message: str) -> bool:
    """Send a Telegram message to Daniel. Returns True on success."""
    data = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    resp = _make_request(API_URL, data)
    return resp.get("ok", False)


def notify_plain(message: str) -> bool:
    """Send a plain-text Telegram message (no Markdown parsing issues)."""
    data = {
        "chat_id": CHAT_ID,
        "text": message
    }
    resp = _make_request(API_URL, data)
    return resp.get("ok", False)


def notify_github_attention(event_type: str, repo: str, url: str, summary: str):
    """Notify Daniel about a GitHub event that needs his attention."""
    emoji = {
        "payment_request": "\U0001f4b0",
        "job_inquiry": "\U0001f4bc",
        "contact_request": "\U0001f4e7",
        "cla_request": "\U0001f4dd",
        "sponsor": "\U0001f4b0",
    }.get(event_type, "\U0001f4e2")

    msg = f"{emoji} *{event_type.replace('_', ' ').title()}*\n"
    msg += f"Repo: `{repo}`\n"
    msg += f"{summary}\n"
    msg += f"[View on GitHub]({url})"

    return notify(msg)


# ---------------------------------------------------------------------------
# Inbound: receive messages FROM Daniel
# ---------------------------------------------------------------------------

def get_updates(offset: int = 0, timeout: int = 30) -> list:
    """Long-poll Telegram for new messages from Daniel."""
    url = f"{BASE_URL}/getUpdates"
    data = {
        "offset": offset,
        "timeout": timeout,
        "allowed_updates": ["message"]
    }
    resp = _make_request(url, data, timeout=timeout + 10)
    return resp.get("result", [])


def extract_github_url(text: str) -> str | None:
    """Extract a GitHub PR/issue URL from text."""
    match = re.search(r'https://github\.com/[\w\-]+/[\w\-]+/(?:pull|issues)/\d+', text)
    return match.group(0) if match else None


class TelegramDaemon:
    """Polls the Philanthropy bot: commands run directly, chat goes to Gemini."""

    def __init__(self):
        self.offset = 0
        # Ring buffer of recent outbound notifications for context
        self.recent_notifications = deque(maxlen=20)
        # Conversation history — passed to Claude each call so it has memory
        self.conversation = deque(maxlen=20)

    def run(self, poll_interval: int = 5):
        """Run the Telegram daemon — long-polls for messages."""
        print("Telegram daemon started (bridge mode)", flush=True)
        print(f"  Inbox:  {TELEGRAM_INBOX}", flush=True)
        print(f"  Outbox: {TELEGRAM_OUTBOX}", flush=True)
        print(flush=True)

        while True:
            try:
                # Poll for incoming Telegram messages
                updates = get_updates(offset=self.offset, timeout=5)
                for update in updates:
                    self.offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    chat_id = str(msg.get("chat", {}).get("id", ""))

                    # Only process messages from Daniel
                    if chat_id != CHAT_ID:
                        continue

                    text = msg.get("text", "").strip()
                    if not text:
                        continue

                    reply_to = msg.get("reply_to_message", {})
                    self._handle_message(text, reply_to)

                # Poll outbox for responses from Claude Code session
                self._poll_outbox()

            except KeyboardInterrupt:
                print("\nTelegram daemon stopped.", flush=True)
                break
            except Exception as e:
                print(f"  Telegram poll error: {e}", flush=True)
                time.sleep(poll_interval)

    def _build_context(self, text: str, reply_to: dict) -> str:
        """Build context string for Claude from conversation history and notifications."""
        parts = []

        # Conversation history — this is the critical part for continuity
        if self.conversation:
            history = "\n".join(
                f"{'Daniel' if m['role'] == 'user' else 'Claude'}: {m['text']}"
                for m in self.conversation
            )
            parts.append(f"CONVERSATION HISTORY (most recent messages):\n{history}")

        # If replying to a specific notification, include it
        reply_text = reply_to.get("text", "")
        if reply_text:
            github_url = extract_github_url(reply_text)
            parts.append(f"Daniel is replying to this notification:\n---\n{reply_text[:600]}\n---")
            if github_url:
                parts.append(f"GitHub URL from notification: {github_url}")

        # Include recent notifications for context
        if self.recent_notifications:
            recent = "\n".join(
                f"  [{n['time']}] {n['type']}: {n['summary'][:100]}"
                for n in self.recent_notifications
            )
            parts.append(f"Recent notifications sent to Daniel:\n{recent}")

        return "\n\n".join(parts)

    FACTORY_LABELS = ["com.batesai.dogood.factory", "com.batesai.dogood.feedback",
                      "com.batesai.dogood.bountywatch"]
    HELP = ("Commands:\n"
            "status — what the factory is doing\n"
            "start / stop — run or halt the factory\n"
            "pending — posts waiting for your OK\n"
            "yes <n> / no <n> — post or discard request n\n"
            "Anything else, just ask.")

    def _handle_message(self, text: str, reply_to: dict):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] Daniel: {text[:200]}", flush=True)
        self.conversation.append({"role": "user", "text": text[:500]})
        reply = self._run_command(text, reply_to.get("text", "") if reply_to else "")
        if reply is None:
            reply = self._ask_gemini(text, reply_to)
        if not reply:
            return
        print(f"[{ts}] <- {reply[:120]}", flush=True)
        self.conversation.append({"role": "assistant", "text": reply[:500]})
        for i in range(0, len(reply), 4000):
            notify_plain(reply[i:i + 4000])

    def _run_command(self, text: str, reply_text: str = "") -> str | None:
        """Deterministic commands — no AI tokens. Returns None if text isn't a command."""
        from src import approvals
        words = text.strip().lower().rstrip(".!").split()
        if not words:
            return None
        verb, arg = words[0], (words[1] if len(words) > 1 else "")
        entry_id = int(arg.lstrip("#")) if arg.lstrip("#").isdigit() else None
        if entry_id is None and reply_text:
            m = re.search(r"#(\d+)", reply_text)
            entry_id = int(m.group(1)) if m else None
        if len(words) > 2:
            return None
        if verb in ("yes", "y", "approve", "ok", "post"):
            return approvals.resolve(entry_id, approve=True)
        if verb in ("no", "n", "reject", "discard", "skip"):
            return approvals.resolve(entry_id, approve=False)
        if verb in ("pending", "queue") and not arg:
            items = approvals.pending()
            if not items:
                return "Nothing is waiting for approval."
            return "\n\n".join(f"#{e['id']} ({e['kind']}): {e['summary'][:300]}" for e in items)
        if verb in ("start", "resume", "go") and arg in ("", "factory", "it"):
            return self._start_factory()
        if verb in ("stop", "halt", "pause") and arg in ("", "factory", "it"):
            return self._stop_factory()
        if verb == "status" and not arg:
            return self._status_text()
        if verb in ("help", "/help", "/start", "commands") and not arg:
            return self.HELP
        return None

    def _launchctl(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30)

    def _is_loaded(self, label: str) -> bool:
        return self._launchctl("list", label).returncode == 0

    def _start_factory(self) -> str:
        uid = os.getuid()
        started = []
        for label in self.FACTORY_LABELS:
            plist = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
            if not plist.exists():
                continue
            if not self._is_loaded(label):
                self._launchctl("bootstrap", f"gui/{uid}", str(plist))
            started.append(label.split(".")[-1])
        return f"Started: {', '.join(started)}. Nothing gets posted without your yes."

    def _stop_factory(self) -> str:
        uid = os.getuid()
        self._launchctl("bootout", f"gui/{uid}/com.batesai.dogood.factory")
        Path("/tmp/dogood-factory.pid").unlink(missing_ok=True)
        return "Factory stopped. Feedback checks and this chat stay on. Say \"start\" to resume."

    def _status_text(self) -> str:
        from src import approvals
        from src.config import primary_model, reviewer_model
        lines = []
        for label in self.FACTORY_LABELS + ["com.batesai.dogood.telegramd"]:
            lines.append(f"{label.split('.')[-1]}: {'on' if self._is_loaded(label) else 'off'}")
        try:
            log = Path("/tmp/dogood-factory.log").read_text().strip().splitlines()
            recent = [l.strip() for l in log[-40:] if l.strip() and not l.startswith("  File")]
            pause = next((l for l in reversed(recent) if "PAUSED" in l or "Sleeping" in l), None)
            lines.append(f"Last factory activity: {recent[-1][:200]}" if recent else "No factory log yet")
            if pause and recent and ("Sleeping" in recent[-1]):
                lines.append(f"Paused: {pause[:200]}")
        except OSError:
            lines.append("No factory log yet")
        lines.append(f"Waiting for your OK: {len(approvals.pending())}")
        lines.append(f"Models: solver {primary_model()}, reviewer {reviewer_model()}")
        return "\n".join(lines)

    def _ask_gemini(self, text: str, reply_to: dict) -> str:
        """Free-form chat runs on Gemini so it never spends Claude tokens."""
        try:
            secrets = json.loads((DAEMON_MANAGER_DIR / "secrets.json").read_text())
            key = secrets.get("GEMINI_API_KEY") or secrets.get("GOOGLE_API_KEY")
        except Exception:
            key = os.environ.get("GEMINI_API_KEY")
        if not key:
            return "I couldn't find a Gemini API key. " + self.HELP

        context = self._build_context(text, reply_to)
        system = (
            "You are the Do Good Factory's Telegram assistant, talking to Daniel through his "
            "Philanthropy bot. Do Good fixes open-source issues; every GitHub post waits for "
            "Daniel's yes. Be brief, plain, and honest. Never use profanity.\n"
            "If Daniel is clearly asking you to perform one of these actions, reply with ONLY "
            "the line `CMD: <action>` where action is one of: status, start, stop, pending, "
            "yes <n>, no <n>, help. Otherwise answer from the status below.\n\n"
            f"CURRENT STATUS:\n{self._status_text()}\n\n{context}"
        )
        body = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": text}]}],
        }
        model = os.environ.get("DOGOOD_CHAT_MODEL", "gemini-flash-latest")
        try:
            req = urllib.request.Request(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "x-goog-api-key": key},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.load(resp)
            answer = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            print(f"  [GEMINI] error: {e}", flush=True)
            return "Gemini didn't answer just now. " + self.HELP

        m = re.match(r"^`?CMD:\s*(.+?)`?$", answer)
        if m:
            return self._run_command(m.group(1)) or self.HELP
        return answer

    def _poll_outbox(self):
        """Check outbox for responses from Claude Code session and send them."""
        outbox = Path(TELEGRAM_OUTBOX)
        if not outbox.exists() or outbox.stat().st_size == 0:
            return
        try:
            lines = outbox.read_text().strip().split("\n")
            outbox.write_text("")  # Clear after reading
            for line in lines:
                if not line.strip():
                    continue
                msg = json.loads(line)
                text = msg.get("text", "")
                if text:
                    for i in range(0, len(text), 4000):
                        notify_plain(text[i:i + 4000])
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Sent response: {text[:80]}", flush=True)
        except Exception as e:
            print(f"Outbox error: {e}", flush=True)

    def record_notification(self, event_type: str, repo: str, url: str, summary: str):
        """Record an outbound notification for context tracking."""
        self.recent_notifications.append({
            "time": datetime.now().strftime("%H:%M"),
            "type": event_type,
            "repo": repo,
            "url": url,
            "summary": summary,
        })
