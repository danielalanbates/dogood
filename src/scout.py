"""Scout: the Do Good bot that finds problems worth fixing.

Each cycle it:
  1. Rescans GitHub for fresh open issues (at most every RESCAN_HOURS).
  2. Has the "scout" model (menu bar → AI Models → Scout) read unscored issues and
     rate 0-100 how well-defined, still-open, and fixable-in-one-PR each one is.
The Fixer then takes the highest-rated issues first and skips anything under MIN_SCORE.

Copyright (c) 2026 Bates LLC. All rights reserved.
"""

import asyncio
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from src.db import get_connection

STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "scout_state.json"
RESCAN_HOURS = 12
BATCH = 8            # issues per AI call
PER_CYCLE = 40       # issues rated per cycle
CYCLE_SLEEP = 30 * 60
MIN_SCORE = 40       # Fixer skips issues Scout rated below this


def ensure_columns(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(issues)")}
    for name, kind in (("scout_score", "REAL"), ("scout_note", "TEXT"), ("scout_at", "TIMESTAMP")):
        if name not in cols:
            conn.execute(f"ALTER TABLE issues ADD COLUMN {name} {kind}")
    conn.commit()


def _state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def rescan_if_due() -> None:
    state = _state()
    if time.time() - state.get("last_rescan", 0) < RESCAN_HOURS * 3600:
        return
    from src.scanner import Scanner
    scanner = Scanner()
    if not scanner.token:
        print("  [SCOUT] no GITHUB_TOKEN; skipping rescan", flush=True)
        return
    print(f"  [SCOUT] rescanning GitHub for open issues...", flush=True)
    start = time.time()
    try:
        found = asyncio.run(scanner.scan_issues_for_repos(1000, 10))
        print(f"  [SCOUT] rescan found {found:,} issues in {(time.time() - start) / 60:.0f}m", flush=True)
    except Exception as e:
        print(f"  [SCOUT] rescan error: {e}", flush=True)
    state["last_rescan"] = time.time()
    _save_state(state)


def _candidates(conn: sqlite3.Connection, limit: int) -> list[dict]:
    from src.config import SUPPORTED_LANGUAGES
    ph = ",".join("?" for _ in SUPPORTED_LANGUAGES)
    rows = conn.execute(f"""
        SELECT i.id, i.number, i.title, i.body, i.labels, i.comments_count, i.updated_at,
               r.full_name, r.language
        FROM issues i JOIN repositories r ON i.repo_id = r.id
        WHERE i.state = 'open' AND i.is_assigned = 0 AND i.scout_score IS NULL
          AND r.language IN ({ph}) AND r.stars >= 1000
          AND LENGTH(COALESCE(i.body, '')) >= 50
          AND i.updated_at > datetime('now', '-2 years')
          AND r.full_name NOT IN (SELECT full_name FROM repo_blacklist WHERE forgiven_at IS NULL)
          AND i.id NOT IN (SELECT issue_id FROM contributions WHERE issue_id IS NOT NULL)
        ORDER BY r.combined_score DESC, i.priority_score DESC
        LIMIT ?
    """, [*SUPPORTED_LANGUAGES, limit]).fetchall()
    return [dict(r) for r in rows]


PROMPT = """You are Scout for the Do Good Factory, which sends small, high-quality pull requests to open-source projects.
Rate each GitHub issue 0-100 for how likely an AI coding agent can fix it in ONE small PR that maintainers would merge.
High: clear bug or small, well-specified change; reproducible; maintainers seem to want outside help.
Low: vague, design discussion, needs maintainer decision, huge feature, probably already fixed, needs hardware/secrets, or spam.
Return ONLY a JSON array: [{{"id": <id>, "score": <0-100>, "note": "<under 15 words>"}}]

ISSUES:
{issues}"""


def rate_batch(conn: sqlite3.Connection, batch: list[dict]) -> int:
    from src.llm import complete, model_for
    text = "\n\n".join(
        f"id={b['id']} | {b['full_name']}#{b['number']} ({b['language']}, {b['comments_count']} comments, "
        f"updated {str(b['updated_at'])[:10]})\nLabels: {b['labels']}\nTitle: {b['title']}\n{(b['body'] or '')[:1200]}"
        for b in batch
    )
    try:
        answer = complete("scout", PROMPT.format(issues=text), timeout=240, cwd="/tmp")
    except Exception as e:
        print(f"  [SCOUT] {model_for('scout')} error: {str(e)[:200]}", flush=True)
        return 0
    m = re.search(r"\[.*\]", answer, re.S)
    try:
        ratings = json.loads(m.group(0)) if m else []
    except json.JSONDecodeError:
        ratings = []
    ids = {b["id"] for b in batch}
    now = datetime.now(timezone.utc).isoformat()
    saved = 0
    for r in ratings:
        try:
            iid, score = int(r["id"]), float(r["score"])
        except (KeyError, TypeError, ValueError):
            continue
        if iid in ids:
            conn.execute("UPDATE issues SET scout_score=?, scout_note=?, scout_at=? WHERE id=?",
                         (max(0.0, min(100.0, score)), str(r.get("note", ""))[:200], now, iid))
            saved += 1
    conn.commit()
    return saved


def run_forever() -> None:
    from src.llm import model_for
    print(f"Scout started — model {model_for('scout')}", flush=True)
    while True:
        try:
            conn = get_connection()
            ensure_columns(conn)
            todo = _candidates(conn, PER_CYCLE)
            rated = 0
            for i in range(0, len(todo), BATCH):
                rated += rate_batch(conn, todo[i:i + BATCH])
            good = conn.execute("SELECT COUNT(*) FROM issues WHERE state='open' AND scout_score >= ?",
                                (MIN_SCORE,)).fetchone()[0]
            conn.close()
            print(f"[{datetime.now():%H:%M}] Scout ({model_for('scout')}): rated {rated}/{len(todo)}, "
                  f"{good:,} open issues rated worth fixing", flush=True)
            rescan_if_due()
        except Exception as e:
            print(f"  [SCOUT] cycle error: {e}", flush=True)
        time.sleep(CYCLE_SLEEP)
