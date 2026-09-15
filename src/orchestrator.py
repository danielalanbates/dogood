"""Agent factory: runs solver inline, one issue at a time. No subprocesses.

Architecture (simplified 2026-03-07):
- Previously spawned concurrent subprocesses (claude CLI as child processes)
- Now runs solver.solve_issue() directly in the same process
- One issue at a time, sequentially — no concurrency, no subprocess crashes
- All issue selection, prioritization, rate limiting, and quality gates preserved
"""

import asyncio
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import (
    AGENT_CLAIM_TTL_MINUTES,
    MIN_STARS_DEFAULT, LOG_FILE,
    load_model_tiers,
)
from src.concurrency import (
    ConnectionPool, SharedRateLimiter, LogWriter,
    claim_issue, release_claim, cleanup_agent_work_dir,
)
from src.rate_coordinator import is_in_cooldown, seconds_until_clear
from src.pr_safety import can_create_pr, record_pr_created, check_close_ratio, can_pr_repo, record_repo_pr
from src.db import (
    get_next_unclaimed_issue, get_next_tagged_issue,
    record_agent_run, update_agent_run,
    get_next_feedback_revision, update_feedback_status,
)
from src.model_selector import get_next_tier
from src.telegram import notify_plain, notify_github_attention
from src.utils import generate_agent_id, now_iso


class FactoryHaltedError(Exception):
    """Raised when the factory detects a systemic failure and must halt."""
    pass


RATE_LIMIT_SIGNAL_FILE = Path("/tmp/dogood-rate-limit-signal.json")
RATE_LEARNING_FILE = Path("/tmp/dogood-rate-learning.json")
FACTORY_STATUS_FILE = Path("/tmp/dogood-factory-status.json")


class TierDistributor:
    """Manages the distribution of agents across model tiers.

    Strategy: start everyone at tier 1. When rate limits hit a model,
    promote 1 agent to the next tier to use a different rate limit pool.
    Track what we learn about each model's rate limits.
    """

    def __init__(self):
        self._tier_floor = 1  # minimum tier to assign
        self._active_tiers: dict[str, int] = {}  # agent_id -> tier number
        self._rate_limit_events: list[dict] = []  # recent events for learning
        self._models_saturated: set[str] = set()  # models currently at limit
        self._load_learning()

    def assign_tier(self, agent_id: str, complexity: float) -> dict:
        """Assign a model tier using a fixed distribution pattern.

        Default: 4 agents at sonnet-low (floor), 1 agent at sonnet-high.
        When rate limits raise the floor, the distribution shifts up but
        keeps the same 4:1 ratio (4 at floor, 1 at floor+2 or max).
        """
        # Count how many active agents are at the "high" slot (floor + 2)
        high_tier_num = min(self._tier_floor + 2, len(load_model_tiers()))
        high_count = sum(1 for t in self._active_tiers.values()
                         if t >= high_tier_num)

        # 1 out of every 5 agents gets the high slot
        if high_count < 1:
            target_tier = high_tier_num
        else:
            target_tier = self._tier_floor

        # If the target model is saturated, bump up
        tier_dict = self._get_tier(target_tier)
        while tier_dict and tier_dict["model"] in self._models_saturated:
            next_dict = get_next_tier(tier_dict)
            if next_dict:
                tier_dict = next_dict
                target_tier = tier_dict["tier"]
            else:
                break  # at max, nothing to do

        tier_dict = self._get_tier(target_tier)
        if not tier_dict:
            tier_dict = load_model_tiers()[0].copy()

        self._active_tiers[agent_id] = tier_dict["tier"]
        return tier_dict

    def report_rate_limit(self, model: str):
        """Called when any agent hits a rate limit on a specific model.

        Bumps the tier floor so the NEXT agent uses a different pool.
        """
        now = time.time()
        self._rate_limit_events.append({
            "model": model, "time": now
        })
        # Keep last 50 events
        self._rate_limit_events = self._rate_limit_events[-50:]

        # Mark this model as saturated (clear after 2 minutes)
        self._models_saturated.add(model)

        # Count recent hits on this model (last 5 min)
        recent = [e for e in self._rate_limit_events
                  if e["model"] == model and now - e["time"] < 300]

        # If 2+ hits on this model in 5 min, bump the floor past it
        if len(recent) >= 2:
            for tier in load_model_tiers():
                if tier["model"] == model:
                    if tier["tier"] >= self._tier_floor:
                        new_floor = tier["tier"] + 1
                        if new_floor <= len(load_model_tiers()):
                            self._tier_floor = new_floor
                            print(f"  [TIER SHIFT] {model} saturated — "
                                  f"floor raised to tier {self._tier_floor} "
                                  f"({self._get_tier(self._tier_floor)['label']})",
                                  flush=True)

        self._record_learning(model, now)

    def clear_saturation(self, model: str):
        """Clear saturation flag after successful use of a model."""
        self._models_saturated.discard(model)
        # Also consider lowering the floor if the model is clear
        now = time.time()
        recent = [e for e in self._rate_limit_events
                  if e["model"] == model and now - e["time"] < 300]
        if not recent and self._tier_floor > 1:
            # No recent hits — try stepping the floor back down
            self._tier_floor = max(1, self._tier_floor - 1)
            print(f"  [TIER SHIFT] {model} clear — "
                  f"floor lowered to tier {self._tier_floor}",
                  flush=True)

    def is_at_max_tier(self) -> bool:
        """Check if the floor is already at the highest tier."""
        return self._tier_floor >= len(load_model_tiers())

    def release_agent(self, agent_id: str):
        """Remove an agent from tracking."""
        self._active_tiers.pop(agent_id, None)

    def get_distribution_summary(self) -> str:
        """Summary of current tier distribution for logging."""
        counts: dict[int, int] = {}
        for tier_num in self._active_tiers.values():
            counts[tier_num] = counts.get(tier_num, 0) + 1
        parts = []
        for tier in load_model_tiers():
            n = counts.get(tier["tier"], 0)
            if n > 0:
                parts.append(f"{tier['label']}={n}")
        return ", ".join(parts) if parts else "none active"

    def _get_tier(self, tier_num: int) -> dict | None:
        for t in load_model_tiers():
            if t["tier"] == tier_num:
                return t.copy()
        return None

    def _record_learning(self, model: str, timestamp: float):
        """Write rate limit observations to a learning file."""
        try:
            data = {}
            if RATE_LEARNING_FILE.exists():
                data = json.loads(RATE_LEARNING_FILE.read_text())

            if model not in data:
                data[model] = {"hits": [], "estimated_rpm": None}

            hits = data[model]["hits"]
            hits.append(timestamp)
            # Keep last 100 hits per model
            data[model]["hits"] = hits[-100:]

            # Estimate RPM cap: look at hits in the last 10 minutes
            recent = [h for h in hits if timestamp - h < 600]
            if len(recent) >= 3:
                # Time span between first and last hit
                span_minutes = (recent[-1] - recent[0]) / 60
                if span_minutes > 0:
                    estimated_rpm = len(recent) / span_minutes
                    data[model]["estimated_rpm"] = round(estimated_rpm, 1)
                    data[model]["last_updated"] = datetime.now().isoformat()

            tmp = RATE_LEARNING_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.rename(RATE_LEARNING_FILE)
        except Exception:
            pass

    def _load_learning(self):
        """Load previous rate limit learning data."""
        try:
            if RATE_LEARNING_FILE.exists():
                data = json.loads(RATE_LEARNING_FILE.read_text())
                for model, info in data.items():
                    rpm = info.get("estimated_rpm")
                    if rpm:
                        print(f"  [LEARNED] {model}: ~{rpm} RPM at limit",
                              flush=True)
        except Exception:
            pass


class AgentFactory:
    """Runs solver inline, one issue at a time. No subprocesses."""

    def __init__(self, max_concurrent: int = 1,
                 min_stars: int = MIN_STARS_DEFAULT,
                 max_cost_usd: float = 0.0):
        # Always 1 at a time — no concurrency
        self.max_concurrent = 1
        self.min_stars = min_stars
        self.max_cost_usd = max_cost_usd  # 0 = unlimited
        self.pool = ConnectionPool()
        self.rate_limiter = SharedRateLimiter(self.pool)
        self.log_writer = LogWriter(LOG_FILE)
        self._stats = {"started": 0, "succeeded": 0, "failed": 0, "skipped": 0, "escalated": 0}
        self._total_cost = 0.0
        self._tier_distributor = TierDistributor()
        self._active_agents = {}  # agent_id -> {repo, issue, started}
        # Self-diagnosis state
        self._consecutive_failures = 0
        self._consecutive_error_class = None
        self._error_class_counts: dict[str, int] = {}
        self._diagnosis_history: list[dict] = []  # last 50 failure records

    def _write_status(self, factory_running: bool = True):
        """Write factory status to a file for the status bar app to read."""
        try:
            status = {
                "active_agents": len(self._active_agents),
                "agents": {aid: info for aid, info in self._active_agents.items()},
                "stats": self._stats.copy(),
                "max_concurrent": self.max_concurrent,
                "factory_running": factory_running,
                "error_class_counts": self._error_class_counts.copy(),
                "consecutive_failures": self._consecutive_failures,
                "consecutive_error_class": self._consecutive_error_class,
                "updated": datetime.now().isoformat(),
            }
            FACTORY_STATUS_FILE.write_text(json.dumps(status))
        except Exception:
            pass

    def _classify_error(self, error_text: str, extra_context: str = "",
                        returncode: int = 1) -> dict:
        """Classify an error into a category for diagnosis.

        Works in inline mode (error_text is a Python exception/error string)
        and legacy subprocess mode (error_text is stdout, extra_context is stderr).

        Args:
            error_text: Primary error string (exception message or stdout)
            extra_context: Additional context (stderr or traceback)
            returncode: Exit code (default 1 for inline mode)
        """
        # Guard against None arguments
        error_text = error_text or ""
        extra_context = extra_context or ""

        combined = f"{error_text}\n{extra_context}".lower()
        raw = f"{error_text}\n{extra_context}"[-500:]

        # Exit code 2 = skip (quality gate, CLA, anti-AI, etc.) — benign, not a failure
        if returncode == 2:
            return {"class": "skipped", "message": "Skipped (quality gate/policy)", "raw": raw}

        # Quality gate / skip patterns — classify separately from SDK errors
        skip_patterns = ["quality gate", "blocked_quality_gate", "skipped:",
                         "anti-ai policy", "requires cla", "unsupported language",
                         "duplicate pr", "blocked org", "issue quality too low",
                         "skipped_low_quality"]
        for pat in skip_patterns:
            if pat in combined:
                return {"class": "skipped", "message": f"Skip: {pat}", "raw": raw}

        # Billing / credit errors
        billing_patterns = ["credit balance", "billing_error", "payment required",
                            "insufficient_quota", "balance is too low",
                            "error result: success", "reached your fable limit", "manage usage credits"]
        for pat in billing_patterns:
            if pat in combined:
                return {"class": "billing_error", "message": f"Billing: {pat}", "raw": raw}

        # Auth errors
        auth_patterns = ["not logged in", "invalid api key", "unauthorized",
                         "invalid_api_key", "authentication_error", "api key is invalid"]
        for pat in auth_patterns:
            if pat in combined:
                return {"class": "auth_error", "message": f"Auth: {pat}", "raw": raw}

        # Rate limits (tracked for diagnosis even though handled elsewhere)
        rate_patterns = ["rate_limit", "rate limit", "hit your limit", "you've hit your limit",
                         "you've hit your limit", "too many requests"]
        for pat in rate_patterns:
            if pat in combined:
                return {"class": "rate_limit", "message": f"Rate limit: {pat}", "raw": raw}

        # "No changes produced" — per-issue problem, not systemic
        no_changes_patterns = ["no changes produced", "no changes produced by claude"]
        for pat in no_changes_patterns:
            if pat in combined:
                return {"class": "no_changes", "message": f"No changes: {pat}", "raw": raw}

        # Repo / git errors
        repo_patterns = ["clone failed", "push failed", "git push", "git clone",
                         "repository not found", "remote: repository not found",
                         "fatal: could not read from remote",
                         "connection reset by peer", "clone timed out"]
        for pat in repo_patterns:
            if pat in combined:
                return {"class": "repo_error", "message": f"Repo: {pat}", "raw": raw}

        # SDK / subprocess errors
        sdk_patterns = ["command failed with exit code", "traceback (most recent",
                        "modulenotfounderror", "importerror",
                        "fatal error in message reader",
                        "processerror", "cliconnectionerror"]
        
        # Check if SDK error contains rate limit info (captured by our patched SDK)
        if any(p in raw for p in rate_patterns):
            return {"class": "rate_limit", "message": "Rate limit hit (CLI output contained limit message)", "raw": raw}
        for pat in sdk_patterns:
            if pat in combined:
                return {"class": "sdk_error", "message": f"SDK: {pat}", "raw": raw}

        # Unknown — extract a useful snippet for the message
        snippet = error_text[-200:] if error_text.strip() else extra_context[-200:] if extra_context.strip() else f"exit code {returncode}"
        return {"class": "unknown", "message": snippet.strip()[:200], "raw": raw}

    async def _diagnose_and_react(self, error_info: dict, agent_id: str, issue_info: dict):
        """Update failure counters and trigger corrective action if needed."""
        error_class = error_info["class"]

        # Skips are benign — don't count toward failure streaks
        if error_class == "skipped":
            self._error_class_counts[error_class] = self._error_class_counts.get(error_class, 0) + 1
            return

        # Repo errors and "no changes" are per-issue, not systemic — don't count toward failure streaks
        if error_class in ("repo_error", "no_changes"):
            self._error_class_counts[error_class] = self._error_class_counts.get(error_class, 0) + 1
            self._consecutive_failures = 0
            self._consecutive_error_class = None
            return

        # Update consecutive failure tracking
        if error_class == self._consecutive_error_class:
            self._consecutive_failures += 1
        else:
            self._consecutive_failures = 1
            self._consecutive_error_class = error_class

        # Update class counts
        self._error_class_counts[error_class] = self._error_class_counts.get(error_class, 0) + 1

        # Append to diagnosis history (ring buffer of 50)
        self._diagnosis_history.append({
            "time": now_iso(),
            "agent_id": agent_id,
            "issue": f"{issue_info.get('full_name', '?')}#{issue_info.get('number', '?')}",
            "class": error_class,
            "message": error_info["message"][:200],
        })
        self._diagnosis_history = self._diagnosis_history[-50:]

        # Trigger corrective actions based on error class and streak
        n = self._consecutive_failures

        if error_class == "billing_error" and n >= 1:
            await self._pause_factory(
                reason=f"billing_error (streak={n})",
                message=(
                    f"FACTORY PAUSED: Billing error detected\n"
                    f"Error: {error_info['message']}\n"
                    f"The API key has run out of credits. "
                    f"Factory will auto-resume in 5 hours."
                ),
                pause_hours=5,
            )
        elif error_class == "auth_error" and n >= 1:
            await self._pause_factory(
                reason=f"auth_error (streak={n})",
                message=(
                    f"FACTORY HALTED: Authentication error\n"
                    f"Error: {error_info['message']}\n"
                    f"API key is invalid or expired. Manual intervention required."
                ),
                pause_hours=0,  # indefinite — halt
            )
        elif error_class == "rate_limit" and n >= 5:
            await self._pause_factory(
                reason=f"rate_limit (streak={n})",
                message=(
                    f"FACTORY PAUSED: {n} consecutive rate limits\n"
                    f"All model tiers appear saturated. "
                    f"Factory will auto-resume in 1 hour."
                ),
                pause_hours=1,
            )
        elif error_class == "sdk_error" and n >= 10:
            await self._pause_factory(
                reason=f"sdk_error (streak={n})",
                message=(
                    f"FACTORY PAUSED: {n} consecutive SDK errors\n"
                    f"Last error: {error_info['message'][:150]}\n"
                    f"Factory will auto-resume in 1 hour."
                ),
                pause_hours=1,
            )
        elif error_class == "unknown" and n >= 5:
            await self._pause_factory(
                reason=f"unknown_error (streak={n})",
                message=(
                    f"FACTORY PAUSED: {n} consecutive unknown errors\n"
                    f"Last error: {error_info['message'][:150]}\n"
                    f"Factory will auto-resume in 30 minutes and retry with different issues."
                ),
                pause_hours=0.5,  # 30 min pause then auto-resume
            )
        # repo_error: no factory-level action (per-issue skip handled elsewhere)

    async def _pause_factory(self, reason: str, message: str, pause_hours: float):
        """Pause or halt the factory, send notification, write logs."""
        # Build diagnostic report
        report_lines = [
            message,
            "",
            f"--- Diagnostic Report ---",
            f"Error breakdown: {json.dumps(self._error_class_counts)}",
            f"Factory stats: {json.dumps(self._stats)}",
            f"Consecutive failures: {self._consecutive_failures} ({self._consecutive_error_class})",
        ]
        # Last 5 failures
        recent = self._diagnosis_history[-5:]
        if recent:
            report_lines.append("Last failures:")
            for entry in recent:
                report_lines.append(
                    f"  [{entry['time']}] {entry['class']}: {entry['issue']} — {entry['message'][:80]}"
                )

        report = "\n".join(report_lines)

        # Factory diagnostics logged locally only (CEO only wants actionable alerts)
        print(f"  [DIAGNOSIS] {report[:500]}", flush=True)

        # Write to markdown log
        self.log_writer.append_entry(
            f"## {datetime.now().strftime('%Y-%m-%d %H:%M')} — FACTORY {'PAUSED' if pause_hours > 0 else 'HALTED'}\n"
            f"**Reason:** {reason}\n"
            f"**Error counts:** {json.dumps(self._error_class_counts)}\n"
            f"**Stats:** started={self._stats['started']}, succeeded={self._stats['succeeded']}, "
            f"failed={self._stats['failed']}\n"
            f"---"
        )

        # Update status file
        try:
            status = {
                "factory_running": False,
                "pause_status": "paused" if pause_hours > 0 else "halted",
                "pause_reason": reason,
                "pause_until": (datetime.now().isoformat() if pause_hours == 0
                                else datetime.fromtimestamp(
                                    time.time() + pause_hours * 3600
                                ).isoformat()),
                "error_class_counts": self._error_class_counts.copy(),
                "consecutive_failures": self._consecutive_failures,
                "stats": self._stats.copy(),
                "updated": datetime.now().isoformat(),
            }
            FACTORY_STATUS_FILE.write_text(json.dumps(status))
        except Exception:
            pass

        print(f"  [DIAGNOSIS] {reason}: factory {'pausing' if pause_hours > 0 else 'halting'}",
              flush=True)

        if pause_hours > 0:
            print(f"  [DIAGNOSIS] Sleeping {pause_hours}h, will auto-resume...", flush=True)
            await asyncio.sleep(pause_hours * 3600)
            # Reset counters on resume
            self._consecutive_failures = 0
            self._consecutive_error_class = None
            print(f"  [DIAGNOSIS] Resuming factory after {pause_hours}h pause", flush=True)
        else:
            raise FactoryHaltedError(reason)

    async def run(self, max_issues: int = 100):
        """Main factory loop: pick issue, solve inline, repeat. One at a time."""
        budget_msg = f", budget=${self.max_cost_usd:.2f}" if self.max_cost_usd else ""
        tiers = load_model_tiers()
        tier1_label = tiers[0]["label"] if tiers else "unknown"
        print(f"Agent Factory starting: INLINE mode (no subprocesses), "
              f"min_stars={self.min_stars}, max_issues={max_issues}{budget_msg}, "
              f"model={tier1_label}", flush=True)

        issues_started = 0

        bounty_signal = Path("/tmp/bounty-agent-active.signal")

        while issues_started < max_issues:
            # Check if bounty agent needs priority — pause factory if bounties active
            if bounty_signal.exists():
                try:
                    signal_data = json.loads(bounty_signal.read_text())
                    bounty_count = signal_data.get("count", 0)
                    if bounty_count > 0:
                        print(f"  [PAUSED] Bounty Agent active ({bounty_count} bounties) — "
                              f"yielding rate limits", flush=True)
                        await asyncio.sleep(60)
                        continue
                except Exception:
                    pass

            # Check shared rate limit cooldown
            if is_in_cooldown():
                wait_secs = seconds_until_clear()
                print(f"  [COOLDOWN] Anthropic API rate limited — waiting {wait_secs:.0f}s",
                      flush=True)
                await asyncio.sleep(wait_secs + 5)
                continue

            # PR safety: daily cap + close ratio guard
            pr_allowed, pr_reason = can_create_pr()
            if not pr_allowed:
                # Sleep until local midnight + 60s buffer so the date rolls over
                # and the safety file resets. Avoids spam-logging every 5 minutes.
                now = datetime.now()
                midnight = (now + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                sleep_secs = (midnight - now).total_seconds() + 60
                print(f"  [PR SAFETY] {pr_reason} — sleeping {sleep_secs/3600:.1f}h "
                      f"until midnight", flush=True)
                self._write_status(factory_running=False)
                await asyncio.sleep(sleep_secs)
                self._write_status(factory_running=True)
                continue

            ratio_ok, ratio_msg = check_close_ratio()
            if not ratio_ok:
                print(f"  [PR SAFETY] {ratio_msg}", flush=True)
                await asyncio.sleep(600)
                continue

            # Check budget
            if self.max_cost_usd and self._total_cost >= self.max_cost_usd * 0.8:
                if self._total_cost >= self.max_cost_usd:
                    print(f"  Budget exhausted: ${self._total_cost:.2f} / ${self.max_cost_usd:.2f}",
                          flush=True)
                    break
                print(f"  WARNING: At 80% budget: ${self._total_cost:.2f} / ${self.max_cost_usd:.2f}",
                      flush=True)

            conn = self.pool.get()

            # PRIORITY #1: Check for feedback that needs revision
            feedback_item = get_next_feedback_revision(conn)
            if feedback_item:
                agent_id = generate_agent_id()
                tiers = load_model_tiers()
                mandatory = feedback_item.get("mandatory_model", "opus-high")
                model_tier = next((t for t in tiers if t.get("label") == mandatory),
                                  tiers[-1]).copy()
                print(f"  [FEEDBACK] Priority revision: contribution #{feedback_item['id']} "
                      f"— {feedback_item['full_name']} (PR: {feedback_item.get('pr_url', '?')})",
                      flush=True)

                update_feedback_status(conn, feedback_item["id"], "in_progress")
                record_agent_run(conn, {
                    "id": agent_id,
                    "issue_id": feedback_item.get("issue_id"),
                    "repo_id": feedback_item.get("repo_id"),
                    "model": model_tier["model"],
                    "effort": model_tier["effort"],
                    "status": "fixing",
                })

                self._stats["started"] += 1
                issues_started += 1
                self._active_agents[agent_id] = {
                    "repo": feedback_item.get("full_name", "?"),
                    "type": "feedback",
                    "model": model_tier.get("label", "unknown"),
                    "started": now_iso(),
                }
                self._write_status()

                await self._run_feedback_inline(agent_id, feedback_item, model_tier)
                continue

            # PRIORITY #2: Christian repos
            issue = get_next_tagged_issue(conn, "christian", min_stars=0)
            if issue:
                issue["_tagged"] = "christian"
                print(f"  [CHRISTIAN] Prioritizing Christian repo: "
                      f"{issue['full_name']}#{issue['number']}", flush=True)

            # PRIORITY #3: Regular issues
            if not issue:
                tiers = load_model_tiers()
                tier1_label = tiers[0]["label"] if tiers else ""
                issue = get_next_unclaimed_issue(conn, self.min_stars,
                                                 model_label=tier1_label)
            if not issue:
                print("No eligible issues found.")
                break

            agent_id = generate_agent_id()

            # Claim the issue
            claimed = await claim_issue(self.pool, issue["id"], agent_id,
                                        AGENT_CLAIM_TTL_MINUTES)
            if not claimed:
                continue

            # Per-repo rate limit: max 1 PR per repo per 24 hours
            repo_allowed, repo_reason = can_pr_repo(issue["full_name"])
            if not repo_allowed:
                print(f"  [REPO RATE LIMIT] {repo_reason} — skipping", flush=True)
                await release_claim(self.pool, issue["id"], agent_id, "rate_limited")
                continue

            # Select model tier
            tiers = load_model_tiers()
            model_tier = tiers[0].copy()

            # Bounty issues: use top tier
            if issue.get("is_bounty"):
                model_tier = tiers[-1].copy()
                print(f"  Agent {agent_id}: BOUNTY detected — using {model_tier['label']}")

            # Sponsor/donor repos: always use opus
            from src.db import get_sponsor_repos
            sponsor_repos = get_sponsor_repos(conn)
            if issue.get("full_name") in sponsor_repos:
                model_tier = tiers[-1].copy()
                print(f"  Agent {agent_id}: SPONSOR repo — using {model_tier['label']}")

            print(f"  Agent {agent_id}: {issue['full_name']}#{issue['number']} "
                  f"[{model_tier['label']}] — {issue.get('title', '')[:50]}",
                  flush=True)

            # Record agent run
            record_agent_run(conn, {
                "id": agent_id,
                "issue_id": issue["id"],
                "repo_id": issue.get("repo_id") or issue.get("rid"),
                "model": model_tier["model"],
                "effort": model_tier["effort"],
                "status": "starting",
            })

            self._stats["started"] += 1
            issues_started += 1
            task_type = "bounty" if issue.get("is_bounty") else "fix"
            self._active_agents[agent_id] = {
                "repo": issue.get("full_name", "?"),
                "issue": f"#{issue.get('number', '?')}",
                "type": task_type,
                "model": model_tier.get("label", "unknown"),
                "started": now_iso(),
            }
            self._write_status()

            # Run solver INLINE — no subprocess, same process
            await self._run_agent_inline(agent_id, issue, model_tier)

        print(f"\nFactory complete: {self._stats}")
        self._write_status(factory_running=False)
        return self._stats

    def _check_rate_limit_signals(self):
        """Read rate limit signals from agents and update tier distribution."""
        try:
            if not RATE_LIMIT_SIGNAL_FILE.exists():
                return
            sig = json.loads(RATE_LIMIT_SIGNAL_FILE.read_text())
            model = sig.get("model")
            sig_time = sig.get("time", 0)
            # Only process recent signals (within last 2 min)
            if model and time.time() - sig_time < 120:
                self._tier_distributor.report_rate_limit(model)
                RATE_LIMIT_SIGNAL_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    async def _run_agent_inline(self, agent_id: str, issue: dict, model_tier: dict):
        """Run solver.solve_issue() directly in this process. No subprocess."""
        from src.solver import Solver
        conn = self.pool.get()
        full_name = issue["full_name"]
        issue_number = issue["number"]

        try:
            update_agent_run(conn, agent_id, status="fixing")

            # Wait for rate limit
            await self.rate_limiter.wait_for_slot("github_api")

            # Create solver and run inline
            solver = Solver(
                agent_id=agent_id,
                model_tier=model_tier,
                is_bounty=bool(issue.get("is_bounty")),
            )
            result = await solver.solve_issue(issue["id"])

            if result.get("success"):
                pr_url = result.get("pr_url", "")
                if pr_url:
                    record_pr_created()
                    record_repo_pr(full_name)
                    update_agent_run(conn, agent_id,
                                     status="pr_created",
                                     pr_url=pr_url,
                                     finished_at=now_iso(),
                                     cost_usd=model_tier.get("max_budget_usd", 0))
                    self._stats["succeeded"] += 1
                    self._consecutive_failures = 0
                    self._consecutive_error_class = None
                    print(f"  Agent {agent_id}: PR created — {pr_url}", flush=True)

                    # PR creation logged but not sent to Telegram (CEO only wants actionable alerts)

                    self._tier_distributor.clear_saturation(model_tier["model"])

                    self.log_writer.append_entry(
                        f"## {datetime.now().strftime('%Y-%m-%d %H:%M')} — PR SUBMITTED\n"
                        f"**Repo:** {full_name}\n"
                        f"**Issue/PR:** #{issue_number} — {issue.get('title', '')}\n"
                        f"**Model:** {model_tier['label']}\n"
                        f"**PR:** {pr_url}\n"
                        f"**Agent:** {agent_id}\n"
                        f"**Action needed:** No\n"
                        f"---"
                    )
                else:
                    update_agent_run(conn, agent_id, status="failed",
                                     error="No PR URL in result", finished_at=now_iso())
                    self._stats["failed"] += 1
                    print(f"  Agent {agent_id}: success but no PR URL", flush=True)
            else:
                error = result.get("error") or "Unknown error"

                # Classify the error for diagnosis
                error_info = self._classify_error(error)

                # Check if it's a skip vs real failure
                is_skip = any(kw in error.lower() for kw in [
                    "unsupported language", "duplicate pr", "anti-ai", "requires cla",
                    "quality gate", "blocked org", "blocked_quality_gate",
                    "skipped_blocked_org", "skipped_language", "skipped_duplicate_pr",
                    "issue quality too low", "skipped_low_quality",
                    "pending human approval",
                ])
                if is_skip:
                    update_agent_run(conn, agent_id, status="failed",
                                     error=f"skipped: {error[:200]}",
                                     finished_at=now_iso())
                    self._stats["skipped"] = self._stats.get("skipped", 0) + 1
                    print(f"  Agent {agent_id}: skipped — {error[:80]}", flush=True)
                    self._tier_distributor.clear_saturation(model_tier["model"])
                else:
                    update_agent_run(conn, agent_id, status="failed",
                                     error=error[:500],
                                     error_class=error_info["class"],
                                     finished_at=now_iso())
                    self._stats["failed"] += 1
                    print(f"  Agent {agent_id}: failed [{error_info['class']}] — {error[:80]}",
                          flush=True)
                    await self._diagnose_and_react(error_info, agent_id, issue)

        except FactoryHaltedError:
            raise
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            error_info = self._classify_error(str(e), tb, 1)
            update_agent_run(conn, agent_id, status="failed",
                             error=str(e)[:500],
                             error_class=error_info["class"],
                             finished_at=now_iso())
            self._stats["failed"] += 1
            print(f"  Agent {agent_id}: exception [{error_info['class']}] — {e}", flush=True)
            print(f"  Traceback:\n{tb[-500:]}", flush=True)
            await self._diagnose_and_react(error_info, agent_id, issue)

        finally:
            self._tier_distributor.release_agent(agent_id)
            await release_claim(self.pool, issue["id"], agent_id, "completed")
            cleanup_agent_work_dir(agent_id)
            self._active_agents.pop(agent_id, None)
            self._write_status()

    async def _run_feedback_inline(self, agent_id: str, contribution: dict,
                                    model_tier: dict):
        """Run solver.solve_feedback() directly in this process. No subprocess."""
        from src.solver import Solver
        conn = self.pool.get()
        contribution_id = contribution["id"]

        try:
            solver = Solver(
                agent_id=agent_id,
                model_tier=model_tier,
            )
            result = await solver.solve_feedback(contribution_id)

            if result.get("success"):
                update_agent_run(conn, agent_id, status="pr_created",
                                 pr_url=contribution.get("pr_url", ""),
                                 finished_at=now_iso())
                self._stats["succeeded"] += 1
                self._consecutive_failures = 0
                self._consecutive_error_class = None
                print(f"  Agent {agent_id}: feedback addressed — {contribution.get('pr_url', '')}",
                      flush=True)

                # Feedback addressed logged but not sent to Telegram (CEO only wants actionable alerts)

                self.log_writer.append_entry(
                    f"## {datetime.now().strftime('%Y-%m-%d %H:%M')} — FEEDBACK ADDRESSED\n"
                    f"**Repo:** {contribution['full_name']}\n"
                    f"**PR:** {contribution.get('pr_url', '')}\n"
                    f"**Reviewer:** {contribution.get('feedback_reviewer', '?')}\n"
                    f"**Model:** {model_tier['label']}\n"
                    f"**Agent:** {agent_id}\n"
                    f"**Action needed:** No\n"
                    f"---"
                )
            else:
                error = result.get("error") or "Unknown error"
                error_info = self._classify_error(error)

                update_agent_run(conn, agent_id, status="failed",
                                 error=error[:500],
                                 error_class=error_info["class"],
                                 finished_at=now_iso())
                self._stats["failed"] += 1
                print(f"  Agent {agent_id}: feedback fix failed [{error_info['class']}] — {error[:120]}",
                      flush=True)

                # Permanent failures — don't retry
                permanent_patterns = ["pr is closed", "not open", "merged",
                                      "anti-ai", "repo not found", "archived"]
                is_permanent = any(pat in error.lower() for pat in permanent_patterns)
                if is_permanent:
                    update_feedback_status(conn, contribution_id, "skipped")
                    print(f"  Permanently skipping contribution #{contribution_id}: {error[:80]}")
                else:
                    retry_key = f"feedback_retries_{contribution_id}"
                    self._stats[retry_key] = self._stats.get(retry_key, 0) + 1
                    if self._stats[retry_key] >= 3:
                        update_feedback_status(conn, contribution_id, "skipped")
                        print(f"  Skipping contribution #{contribution_id} after {self._stats[retry_key]} failed attempts")
                    else:
                        update_feedback_status(conn, contribution_id, "needs_revision")

                issue_info = {
                    "full_name": contribution.get("full_name", "?"),
                    "number": contribution.get("pr_url", "feedback").split("/")[-1] if contribution.get("pr_url") else "?",
                }
                await self._diagnose_and_react(error_info, agent_id, issue_info)

        except FactoryHaltedError:
            raise
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            error_info = self._classify_error(str(e), tb, 1)
            update_agent_run(conn, agent_id, status="failed",
                             error=str(e)[:500],
                             error_class=error_info["class"],
                             finished_at=now_iso())
            self._stats["failed"] += 1
            print(f"  Agent {agent_id}: feedback error [{error_info['class']}] — {e}", flush=True)
            update_feedback_status(conn, contribution_id, "needs_revision")
            issue_info = {
                "full_name": contribution.get("full_name", "?"),
                "number": "?",
            }
            await self._diagnose_and_react(error_info, agent_id, issue_info)

        finally:
            self._tier_distributor.release_agent(agent_id)
            cleanup_agent_work_dir(agent_id)
            self._active_agents.pop(agent_id, None)
            self._write_status()
