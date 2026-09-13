"""PR safety guards — shared across github-helper and bounty-agent.

Protects Daniel's GitHub account from spam flags by enforcing:
1. Daily PR cap (default 10/day across all systems)
2. Close ratio guard (auto-pause if >50% of recent PRs are closed without merge)
3. Per-repo rate limit (max 1 PR per repo per 24 hours)
"""

import json
import subprocess
import time
from datetime import datetime, date
from pathlib import Path

SAFETY_FILE = Path("/tmp/dogood-pr-safety.json")
REPO_RATE_LIMIT_FILE = Path("/tmp/dogood-rate-limits.json")
DAILY_PR_CAP = 10
CLOSE_RATIO_THRESHOLD = 0.50
CLOSE_RATIO_WINDOW = 20  # look at last N PRs
REPO_PR_COOLDOWN_HOURS = 24  # max 1 PR per repo per this many hours

def _read_safety() -> dict:
    try:
        if SAFETY_FILE.exists():
            data = json.loads(SAFETY_FILE.read_text())
            if data.get("date") == str(date.today()):
                return data
    except Exception:
        pass
    return {"date": str(date.today()), "pr_count": 0, "paused": False, "pause_reason": ""}

def _write_safety(data: dict):
    data["date"] = str(date.today())
    data["updated"] = datetime.now().isoformat()
    SAFETY_FILE.write_text(json.dumps(data, indent=2))

def record_pr_created():
    """Call this after every successful PR creation."""
    data = _read_safety()
    data["pr_count"] = data.get("pr_count", 0) + 1
    _write_safety(data)

def can_create_pr() -> tuple[bool, str]:
    """Check if it's safe to create another PR.

    Returns (allowed, reason).
    """
    data = _read_safety()

    if data.get("paused"):
        return False, f"Paused: {data.get('pause_reason', 'manual pause')}"

    count = data.get("pr_count", 0)
    if count >= DAILY_PR_CAP:
        return False, f"Daily PR cap reached ({count}/{DAILY_PR_CAP})"

    return True, f"{count}/{DAILY_PR_CAP} PRs today"

def check_close_ratio(username: str = "danielalanbates") -> tuple[bool, str]:
    """Check recent PR merge/close ratio via gh CLI.

    Returns (safe, message). Safe=False means close rate is too high.
    Only checks periodically (cached in safety file).
    """
    data = _read_safety()

    # Cache for 30 minutes to avoid hammering the API
    last_check = data.get("close_ratio_checked")
    if last_check:
        try:
            elapsed = time.time() - datetime.fromisoformat(last_check).timestamp()
            if elapsed < 1800:
                ratio = data.get("close_ratio", 0)
                if ratio > CLOSE_RATIO_THRESHOLD:
                    return False, f"Close ratio {ratio:.0%} exceeds {CLOSE_RATIO_THRESHOLD:.0%} — pausing"
                return True, f"Close ratio {ratio:.0%} (cached)"
        except Exception:
            pass

    # Fetch recent PRs
    try:
        r = subprocess.run(
            ["gh", "search", "prs",
             f"--author={username}",
             "--sort=created", "--order=desc",
             f"--limit={CLOSE_RATIO_WINDOW}",
             "--json", "state,mergedAt"],
            capture_output=True, text=True, timeout=15
        )
        if r.returncode != 0 or not r.stdout.strip():
            return True, "Could not check close ratio (API error)"

        prs = json.loads(r.stdout)
        if len(prs) < 5:
            # Not enough data to judge
            data["close_ratio"] = 0
            data["close_ratio_checked"] = datetime.now().isoformat()
            _write_safety(data)
            return True, f"Only {len(prs)} recent PRs — too few to judge"

        merged = sum(1 for p in prs if p.get("mergedAt"))
        closed_unmerged = sum(1 for p in prs if p.get("state") == "CLOSED" and not p.get("mergedAt"))
        total_resolved = merged + closed_unmerged

        if total_resolved == 0:
            ratio = 0.0
        else:
            ratio = closed_unmerged / total_resolved

        data["close_ratio"] = ratio
        data["close_ratio_checked"] = datetime.now().isoformat()
        data["close_ratio_detail"] = f"{merged} merged, {closed_unmerged} closed of {len(prs)} recent"

        if ratio > CLOSE_RATIO_THRESHOLD:
            data["paused"] = True
            data["pause_reason"] = f"Close ratio {ratio:.0%} ({closed_unmerged}/{total_resolved}) exceeds threshold"
            _write_safety(data)
            return False, data["pause_reason"]

        _write_safety(data)
        return True, f"Close ratio {ratio:.0%} ({merged} merged, {closed_unmerged} closed)"

    except Exception as e:
        return True, f"Close ratio check failed: {e}"

def get_daily_pr_count() -> int:
    return _read_safety().get("pr_count", 0)

def reset_pause():
    """Manually clear a pause (e.g., from Telegram command)."""
    data = _read_safety()
    data["paused"] = False
    data["pause_reason"] = ""
    _write_safety(data)


# ---------------------------------------------------------------------------
# Per-repo rate limiting: max 1 PR per repo per 24 hours
# ---------------------------------------------------------------------------

def _read_repo_rates() -> dict:
    """Read the per-repo rate limit tracker.

    Returns dict of {repo_full_name: last_pr_timestamp_iso}.
    Automatically prunes entries older than REPO_PR_COOLDOWN_HOURS.
    """
    try:
        if REPO_RATE_LIMIT_FILE.exists():
            data = json.loads(REPO_RATE_LIMIT_FILE.read_text())
            # Prune stale entries (older than cooldown window)
            now = time.time()
            cutoff = now - (REPO_PR_COOLDOWN_HOURS * 3600)
            pruned = {}
            for repo, ts_iso in data.items():
                try:
                    ts = datetime.fromisoformat(ts_iso).timestamp()
                    if ts > cutoff:
                        pruned[repo] = ts_iso
                except Exception:
                    pass
            return pruned
    except Exception:
        pass
    return {}


def _write_repo_rates(data: dict):
    """Write the per-repo rate limit tracker atomically."""
    tmp = REPO_RATE_LIMIT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(REPO_RATE_LIMIT_FILE)


def can_pr_repo(full_name: str) -> tuple[bool, str]:
    """Check if we can submit a PR to this repo (1 per 24h limit).

    Args:
        full_name: Repository in "owner/repo" format.

    Returns:
        (allowed, reason) tuple.
    """
    data = _read_repo_rates()
    last_ts_iso = data.get(full_name)
    if not last_ts_iso:
        return True, "No recent PR to this repo"

    try:
        last_ts = datetime.fromisoformat(last_ts_iso).timestamp()
        elapsed_hours = (time.time() - last_ts) / 3600
        if elapsed_hours < REPO_PR_COOLDOWN_HOURS:
            remaining = REPO_PR_COOLDOWN_HOURS - elapsed_hours
            return False, (f"Rate limited: PR submitted to {full_name} "
                          f"{elapsed_hours:.1f}h ago, next allowed in {remaining:.1f}h")
    except Exception:
        pass

    return True, "Cooldown expired"


def record_repo_pr(full_name: str):
    """Record that a PR was submitted to a repo. Call after successful PR creation."""
    data = _read_repo_rates()
    data[full_name] = datetime.now().isoformat()
    _write_repo_rates(data)
