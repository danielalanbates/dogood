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
    return send_message(message) is not None


def send_message(message: str) -> int | None:
    """Send plain text; returns Telegram's message_id so reactions can be matched later."""
    resp = _make_request(API_URL, {"chat_id": CHAT_ID, "text": message})
    if resp.get("ok"):
        return resp.get("result", {}).get("message_id")
    return None


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
        "allowed_updates": ["message", "message_reaction"]
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
        self.sent_messages: dict[int, str] = {}

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
                    self._handle_update(update)

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

    def _handle_update(self, update: dict):
        """Turn any message, sticker, photo caption, or reaction into text for Gemini."""
        reaction = update.get("message_reaction")
        if reaction:
            if str(reaction.get("chat", {}).get("id", "")) != CHAT_ID:
                return
            emojis = [r.get("emoji", "") for r in reaction.get("new_reaction", []) if r.get("type") == "emoji"]
            if not emojis:
                return
            reacted = self._find_message(reaction.get("message_id"))
            self._handle_message(f"(Daniel reacted {''.join(emojis)} to your message)",
                                 {"text": reacted} if reacted else {})
            return

        msg = update.get("message", {})
        if str(msg.get("chat", {}).get("id", "")) != CHAT_ID:
            return
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if not text and msg.get("sticker"):
            text = f"(sticker {msg['sticker'].get('emoji', '')})"
        if not text:
            kinds = [k for k in ("photo", "voice", "video", "document", "audio") if k in msg]
            if not kinds:
                return
            text = f"(Daniel sent a {kinds[0]} with no text)"
        self._handle_message(text, msg.get("reply_to_message", {}))

    def _find_message(self, message_id: int | None) -> str:
        """Text of a message we sent, so a reaction can be understood in context."""
        if message_id is None:
            return ""
        if message_id in self.sent_messages:
            return self.sent_messages[message_id]
        from src import approvals
        for e in approvals._read():
            if e.get("meta", {}).get("telegram_message_id") == message_id:
                return f"Do Good wants to post (#{e['id']}, {e['kind']}): {e['summary']}"
        return ""

    def _handle_message(self, text: str, reply_to: dict):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] Daniel: {text[:200]}", flush=True)
        reply = self._ask_gemini(text, reply_to or {})
        if reply is None:
            reply = self._run_command(text, (reply_to or {}).get("text", "")) or "Sorry, my AI brain is offline for a moment. Simple things like \"status\", \"start\", \"stop\" or \"yes 3\" still work."
        self.conversation.append({"role": "user", "text": text[:1000]})
        if not reply:
            return
        print(f"[{ts}] <- {reply[:120]}", flush=True)
        self.conversation.append({"role": "assistant", "text": reply[:1000]})
        for i in range(0, len(reply), 4000):
            message_id = send_message(reply[i:i + 4000])
            if message_id:
                self.sent_messages[message_id] = reply[i:i + 4000][:1000]

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

    def _ask_gemini(self, text: str, reply_to: dict) -> str | None:
        """Chat exactly like Gemini Flash, with Do Good controls. None means Gemini is unavailable."""
        from src import approvals
        waiting = approvals.pending()
        waiting_text = "\n".join(f"#{e['id']} ({e['kind']}): {e['summary'][:400]}" for e in waiting) or "none"
        reply_text = (reply_to or {}).get("text", "")
        system = (
            "You are Daniel's assistant, chatting with Daniel Bates on Telegram through his Philanthropy bot. "
            "Talk exactly as you normally would as a general assistant: warm, natural, helpful on any topic, and brief "
            "enough for a phone. Never use profanity.\n\n"
            "You also run the Do Good Factory, which fixes open-source GitHub issues. Nothing is "
            "posted to GitHub unless Daniel approves that specific request.\n"
            "Read Daniel loosely, including emojis, stickers and reactions: 👍 ✅ 👌 🙌 🚀 💯 or "
            "\"sure\"/\"do it\" usually mean yes; 👎 ❌ 🚫 🗑️ or \"nah\" usually mean no; "
            "▶️ means start, ⏹️ ⏸️ 🛑 mean stop. Use the conversation to decide what an emoji refers to.\n"
            "To act, put each action on its own line as `CMD: <action>`, where action is one of: "
            "status, start, stop, pending, yes <n>, no <n>. You may add a short normal reply too. "
            "Only approve or reject when it is clear which request Daniel means; if several are "
            "waiting and it is unclear, ask. Don't take an action Daniel didn't ask for.\n"
            "Interpret intent, not keywords: \"how's it going\", \"anything new?\", \"what's up with the factory\" "
            "mean status; \"fire it up\", \"get to work\" mean start; \"take a break\", \"shut it down\" mean stop. "
            "Never reply with a command menu or list of keywords; just talk. Don't use markdown headers.\n\n"
            f"FACTORY STATUS:\n{self._status_text()}\n\n"
            f"REQUESTS WAITING FOR DANIEL:\n{waiting_text}\n"
            + (f"\nDaniel is replying or reacting to this message:\n{reply_text[:1500]}\n" if reply_text else "")
        )
        contents = [
            {"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["text"]}]}
            for m in self.conversation
        ]
        contents.append({"role": "user", "parts": [{"text": text}]})
        from src.llm import model_for, provider, gemini_generate, complete
        model = model_for("chat")
        try:
            if provider(model) == "gemini":
                answer = gemini_generate(model, contents, system)
            else:
                history = "\n".join(f"{'Daniel' if c['role'] == 'user' else 'You'}: {c['parts'][0]['text']}"
                                     for c in contents)
                answer = complete("chat", history, system=system, timeout=120)
        except Exception as e:
            print(f"  [CHAT] {model} error: {e}", flush=True)
            return None

        results, lines = [], []
        for line in answer.splitlines():
            m = re.match(r"^\s*`?CMD:\s*(.+?)`?\s*$", line)
            if m:
                results.append(self._run_command(m.group(1)) or f"(couldn't do: {m.group(1)})")
            else:
                lines.append(line)
        reply = "\n".join(lines).strip()
        if not results:
            return reply
        # Second pass: let Gemini tell Daniel what happened in plain conversation,
        # instead of dumping raw command output.
        followup = contents + [
            {"role": "model", "parts": [{"text": answer}]},
            {"role": "user", "parts": [{"text": "(system) The actions ran. Results:\n" + "\n---\n".join(results)
             + "\nNow reply to Daniel conversationally, like a friend giving a quick update. Summarize what matters "
               "in plain words (no raw labels like 'factory: on', no command lists, no CMD lines). "
               "If a request is waiting, say what it is and that he can just say yes or no."}]},
        ]
        try:
            if provider(model) == "gemini":
                spoken = gemini_generate(model, followup, system)
            else:
                spoken = complete("chat", followup[-1]["parts"][0]["text"], system=system, timeout=120)
            spoken = "\n".join(l for l in spoken.splitlines() if not re.match(r"^\s*`?CMD:", l)).strip()
            return spoken or "\n\n".join(results)
        except Exception as e:
            print(f"  [CHAT] followup error: {e}", flush=True)
            return "\n\n".join(x for x in [reply, *results] if x)

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
