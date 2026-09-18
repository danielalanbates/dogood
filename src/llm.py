"""One place that decides which model runs each Do Good role and how to call it.

Model ids (as chosen in the menu bar app, stored in shared/dogood_models.json):
  claude-*        Claude Code CLI / Agent SDK
  gemini-*        Gemini API (key from DaemonManager secrets.json) — text only
  agy:<model>     Antigravity CLI (`agy -p`) — can edit files, so it can solve issues

Copyright (c) 2026 Bates LLC. All rights reserved.
"""

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

MODEL_CHOICE_FILE = Path.home() / "Library/Application Support/BatesAI/shared/dogood_models.json"
DAEMON_MANAGER_SECRETS = Path.home() / "Library/Application Support/DaemonManager/secrets.json"
AGY = str(Path.home() / ".local/bin/agy")

# role -> (default model, whether the role must be able to edit files)
ROLES = {
    "scout": ("agy:gemini-3.8-flash-low", False),     # finds and rates issues
    "primary": ("agy:gemini-3.8-flash-high", True),       # fixer
    "reviewer": ("agy:gemini-3.1-pro-high", False),     # 95% acceptance review
    "replies": ("agy:gemini-3.8-flash-low", False),   # replies to maintainers
    "chat": ("agy:gemini-3.8-flash-low", False),      # Telegram conversation
}


def provider(model: str) -> str:
    if model.startswith("agy:"):
        return "agy"
    if model.startswith("gemini-"):
        return "gemini"
    return "claude"


def model_for(role: str) -> str:
    default, needs_tools = ROLES[role]
    env_override = {"primary": "DOGOOD_PRIMARY_MODEL"}.get(role)
    if env_override and os.getenv(env_override):
        default = os.environ[env_override]
    try:
        chosen = json.loads(MODEL_CHOICE_FILE.read_text()).get(role)
    except Exception:
        chosen = None
    if not isinstance(chosen, str) or not chosen:
        return default
    if needs_tools and provider(chosen) == "gemini":
        return default
    return chosen


def gemini_key() -> str | None:
    try:
        secrets = json.loads(DAEMON_MANAGER_SECRETS.read_text())
        return secrets.get("GEMINI_API_KEY") or secrets.get("GOOGLE_API_KEY")
    except Exception:
        return os.environ.get("GEMINI_API_KEY")


def gemini_generate(model: str, contents: list[dict], system: str = "", timeout: int = 60) -> str:
    key = gemini_key()
    if not key:
        raise RuntimeError("no Gemini API key in DaemonManager secrets.json")
    body = {"contents": contents}
    if system:
        body["system_instruction"] = {"parts": [{"text": system}]}
    import time
    last_error = None
    for attempt, name in enumerate([model, "gemini-flash-lite-latest", "gemini-2.5-flash-lite"]):
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{name}:generateContent",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "x-goog-api-key": key},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.load(resp)
            return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"]).strip()
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code not in (429, 500, 503):
                raise
            time.sleep(1 + attempt)
    # API quota gone: Antigravity has its own OAuth quota.
    history = "\n\n".join(f"{'Daniel' if c.get('role') != 'model' else 'You'}: {c['parts'][0]['text']}" for c in contents)
    full = f"{system}\n\n{history}" if system else history
    r = subprocess.run([AGY, "-p", full, "--model", "gemini-3.8-flash-low", "--output-format", "text",
                        "--print-timeout", f"{timeout}s"], capture_output=True, text=True,
                       timeout=timeout + 30, env=_clean_env(), cwd="/tmp")
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    raise last_error


def _clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if "CLAUDE" not in k.upper()}


def complete(role: str, prompt: str, system: str = "", timeout: int = 300, cwd: str | None = None) -> str:
    """Single-shot text answer from the role's model. Raises on failure."""
    model = model_for(role)
    kind = provider(model)
    if kind == "gemini":
        return gemini_generate(model, [{"role": "user", "parts": [{"text": prompt}]}], system, timeout)
    full = f"{system}\n\n{prompt}" if system else prompt
    if kind == "agy":
        cmd = [AGY, "-p", full, "--model", model[4:], "--output-format", "text",
               "--print-timeout", f"{timeout}s"]
    else:
        cmd = ["claude", "-p", full, "--model", model, "--output-format", "text"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 30,
                       env=_clean_env(), cwd=cwd)
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        raise RuntimeError(f"{model} failed (rc={r.returncode}): {(r.stderr or out)[-300:]}")
    return out


def run_agent_in(clone_path: Path, prompt: str, system: str, timeout: int = 1800, model: str | None = None) -> str:
    """Let an Antigravity model work on the clone with file-edit tools. Returns its final text."""
    if not model:
        chosen = model_for("primary")
        if chosen.startswith("agy:"):
            model = chosen[4:]
        else:
            model = "gemini-3.8-flash-high"
    elif model.startswith("agy:"):
        model = model[4:]
    cmd = [AGY, "-p", f"{system}\n\n{prompt}", "--model", model,
           "--dangerously-skip-permissions", "--add-dir", str(clone_path),
           "--output-format", "text", "--print-timeout", f"{timeout}s"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 60,
                       env=_clean_env(), cwd=str(clone_path))
    if r.returncode != 0:
        raise RuntimeError(f"{model} agent failed (rc={r.returncode}): {(r.stderr or r.stdout)[-500:]}")
    return (r.stdout or "").strip()
