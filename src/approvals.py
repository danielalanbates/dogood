"""Human approval queue: nothing is posted to GitHub until Daniel replies "yes".

Every outward action (PR, push, comment, close) is recorded as a list of shell
commands. Daniel gets a Telegram message with the request id and replies
"yes <id>" or "no <id>"; only then are the commands run.

Copyright (c) 2026 Bates LLC. All rights reserved.
"""

import fcntl
import json
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone

from src.config import DATA_DIR

APPROVALS_FILE = DATA_DIR / "approvals.jsonl"
LOCK_FILE = DATA_DIR / "approvals.lock"


@contextmanager
def _locked():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _read() -> list[dict]:
    if not APPROVALS_FILE.exists():
        return []
    return [json.loads(line) for line in APPROVALS_FILE.read_text().splitlines() if line.strip()]


def _write(entries: list[dict]):
    tmp = APPROVALS_FILE.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(e) + "\n" for e in entries))
    tmp.replace(APPROVALS_FILE)


def request(kind: str, summary: str, commands: list[list[str]], meta: dict | None = None,
            ask: bool = True) -> int:
    """Queue an outward action. With ask=False it is recorded but Daniel isn't pinged."""
    with _locked():
        entries = _read()
        entry_id = max((e["id"] for e in entries), default=0) + 1
        entries.append({
            "id": entry_id,
            "kind": kind,
            "summary": summary,
            "commands": commands,
            "meta": meta or {},
            "status": "pending" if ask else "not_proposed",
            "created": datetime.now(timezone.utc).isoformat(),
        })
        _write(entries)
    if ask:
        from src.telegram import notify_plain
        notify_plain(f"🤝 Do Good wants to post (#{entry_id}, {kind}):\n{summary}\n\n"
                     f"Reply \"yes {entry_id}\" to post or \"no {entry_id}\" to discard.")
    return entry_id


def pending() -> list[dict]:
    return [e for e in _read() if e["status"] == "pending"]


def resolve(entry_id: int | None, approve: bool) -> str:
    """Approve (run the commands) or reject a pending request. Returns a message for Daniel."""
    with _locked():
        entries = _read()
        waiting = [e for e in entries if e["status"] == "pending"]
        if not waiting:
            return "Nothing is waiting for approval."
        if entry_id is None:
            if len(waiting) > 1:
                return ("More than one request is waiting — say which: "
                        + ", ".join(f"#{e['id']}" for e in waiting))
            entry = waiting[0]
        else:
            entry = next((e for e in waiting if e["id"] == entry_id), None)
            if not entry:
                return f"#{entry_id} isn't waiting for approval."
        entry["status"] = "running" if approve else "rejected"
        entry["resolved"] = datetime.now(timezone.utc).isoformat()
        _write(entries)

    if not approve:
        return f"Discarded #{entry['id']}. Nothing was posted."

    output = ""
    for cmd in entry["commands"]:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        output = (result.stdout or "").strip()
        if result.returncode != 0:
            _set_status(entry["id"], "failed", (result.stderr or output)[-500:])
            return f"#{entry['id']} failed at `{' '.join(cmd[:3])}`: {(result.stderr or output)[-300:]}"

    _set_status(entry["id"], "posted", output[-500:])
    _after_post(entry, output)
    return f"Posted #{entry['id']}. {output[-200:]}".strip()


def _set_status(entry_id: int, status: str, detail: str = ""):
    with _locked():
        entries = _read()
        for e in entries:
            if e["id"] == entry_id:
                e["status"] = status
                e["detail"] = detail
        _write(entries)


def _after_post(entry: dict, output: str):
    """Keep the factory's own bookkeeping in sync once a PR really exists."""
    if entry["kind"] != "pull request":
        return
    try:
        from src.pr_safety import record_pr_created
        from src.db import get_connection, update_contribution_status
        record_pr_created()
        contrib_id = entry["meta"].get("contribution_id")
        if contrib_id:
            update_contribution_status(get_connection(), contrib_id, "pr_created", output.strip())
    except Exception as e:
        print(f"  [APPROVALS] bookkeeping after post failed: {e}", flush=True)
