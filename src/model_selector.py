"""Model tier selection engine based on issue complexity."""

import json
import math
from src.config import load_model_tiers, MAX_OPUS_PER_ISSUE


# Keywords that signal simple issues
SIMPLE_KEYWORDS = {
    "typo", "typos", "spelling", "rename", "whitespace", "indent",
    "formatting", "css", "style", "yaml", "yml", "config",
    "readme", "docs", "documentation", "comment", "todo",
    "unused import", "unused variable", "lint", "linting",
}

# Keywords that signal complex issues
COMPLEX_KEYWORDS = {
    "refactor", "race condition", "deadlock", "memory leak",
    "architecture", "redesign", "rewrite", "migration",
    "security", "vulnerability", "authentication", "authorization",
    "performance", "optimization", "concurrent", "async",
    "breaking change", "api change", "backwards compatible",
}

# Languages with higher inherent complexity
COMPLEX_LANGUAGES = {"rust", "c", "c++", "go", "java", "scala", "haskell"}
SIMPLE_LANGUAGES = {"markdown", "yaml", "json", "css", "html", "toml"}

# --- Legacy Haiku Filters (DEPRECATED) ---
# Kept for backward compatibility with db.py query filters.
# All issues now use opus-high (Claude Opus 4.6 with extended thinking).
# These filters are no longer used for model selection but may still be
# referenced by the issue selection query for quality filtering.
HAIKU_FILTERS = {
    "min_body_length": 200,
    "min_comments": 0,
    "max_complexity": 0.50,
    "exclude_labels": {
        "enhancement", "feature", "feature-request", "feature request",
        "refactor", "refactoring", "performance", "security",
        "breaking", "breaking-change", "architecture", "design",
        "migration", "api", "api-change",
    },
    "exclude_languages": COMPLEX_LANGUAGES,
    "prefer_labels": {
        "bug", "typo", "docs", "documentation", "good first issue",
        "good-first-issue", "easy", "beginner", "help wanted", "help-wanted",
        "low-hanging-fruit", "trivial",
    },
}


def score_complexity(issue: dict, repo: dict = None) -> float:
    """Score issue complexity from 0.0 (trivial) to 1.0 (very complex).

    Signals and weights:
    - Issue body length (15%)
    - Comment count (10%)
    - Labels (15%)
    - Language complexity (20%)
    - Repo stars/size (15%)
    - Open issues ratio (10%)
    - Title keywords (15%)
    """
    scores = []

    # 1. Issue body length (15%) — longer = more complex
    body = issue.get("body") or ""
    body_len = len(body)
    body_score = min(body_len / 3000, 1.0)
    scores.append(("body_length", body_score, 0.15))

    # 2. Comment count (10%) — more discussion = more complex
    comments = issue.get("comments_count") or 0
    comment_score = min(comments / 15, 1.0)
    scores.append(("comments", comment_score, 0.10))

    # 3. Labels (15%)
    labels_raw = issue.get("labels") or "[]"
    if isinstance(labels_raw, str):
        labels = set(l.lower() for l in json.loads(labels_raw))
    else:
        labels = set(l.lower() for l in labels_raw)

    from src.config import BEGINNER_LABELS
    label_score = 0.7  # default medium
    if labels & BEGINNER_LABELS:
        label_score = 0.2  # beginner = simpler
    if "bug" in labels:
        label_score = max(label_score, 0.5)
    if any(l in labels for l in ("enhancement", "feature", "feature-request")):
        label_score = max(label_score, 0.6)
    if any(l in labels for l in ("critical", "security", "breaking")):
        label_score = 0.9
    scores.append(("labels", label_score, 0.15))

    # 4. Language complexity (20%)
    language = (repo or {}).get("language", "") or ""
    lang_lower = language.lower()
    if lang_lower in SIMPLE_LANGUAGES:
        lang_score = 0.1
    elif lang_lower in COMPLEX_LANGUAGES:
        lang_score = 0.8
    else:
        lang_score = 0.4  # Python, JS, TS, etc.
    scores.append(("language", lang_score, 0.20))

    # 5. Repo stars/size (15%) — bigger repos = harder to navigate
    stars = (repo or {}).get("stars", 0) or 0
    star_score = min(math.log(stars + 1) / math.log(100000), 1.0)
    scores.append(("repo_size", star_score, 0.15))

    # 6. Open issues ratio (10%)
    open_issues = (repo or {}).get("open_issues", 0) or 0
    issues_score = min(open_issues / 500, 1.0)
    scores.append(("open_issues", issues_score, 0.10))

    # 7. Title keywords (15%)
    title = (issue.get("title") or "").lower()
    combined_text = f"{title} {body[:500].lower()}"
    simple_matches = sum(1 for kw in SIMPLE_KEYWORDS if kw in combined_text)
    complex_matches = sum(1 for kw in COMPLEX_KEYWORDS if kw in combined_text)

    if simple_matches > 0 and complex_matches == 0:
        keyword_score = 0.1
    elif complex_matches > simple_matches:
        keyword_score = min(0.5 + complex_matches * 0.15, 1.0)
    elif complex_matches > 0:
        keyword_score = 0.5
    else:
        keyword_score = 0.4
    scores.append(("keywords", keyword_score, 0.15))

    # Weighted sum
    total = sum(score * weight for _, score, weight in scores)
    return round(min(max(total, 0.0), 1.0), 4)


def is_haiku_eligible(issue: dict, repo: dict = None, complexity: float = None) -> bool:
    """Check if an issue is simple enough for haiku-high to handle.

    Returns True only if the issue passes all haiku filters:
    - Complexity score below threshold
    - No excluded labels (enhancement, refactor, security, etc.)
    - Not in a complex language (Rust, C, C++, Go, Java, etc.)
    - Has enough body context
    """
    # Complexity gate
    max_cx = HAIKU_FILTERS.get("max_complexity", 0.35)
    if complexity is not None and complexity > max_cx:
        return False

    # Body length gate
    body_len = len(issue.get("body") or "")
    if body_len < HAIKU_FILTERS.get("min_body_length", 200):
        return False

    # Label exclusion gate
    labels_raw = issue.get("labels") or "[]"
    if isinstance(labels_raw, str):
        try:
            labels = {l.lower() for l in json.loads(labels_raw)}
        except (json.JSONDecodeError, TypeError):
            labels = set()
    else:
        labels = {l.lower() for l in labels_raw}

    if labels & HAIKU_FILTERS.get("exclude_labels", set()):
        return False

    # Language exclusion gate
    language = (repo or {}).get("language", "") or ""
    exclude_langs = HAIKU_FILTERS.get("exclude_languages", set())
    if language.lower() in {l.lower() for l in exclude_langs}:
        return False

    return True


def select_tier(complexity: float, issue_id: int = None, conn=None,
                issue: dict = None, repo: dict = None) -> dict:
    """Select the appropriate model tier based on complexity score.

    All issues use opus-high (Claude Opus 4.6 with extended thinking).
    Opus budget per issue is still enforced to prevent infinite retries.
    """
    tiers = load_model_tiers()
    tier_idx = 0

    # Enforce opus budget per issue (cap retries on the same issue)
    if issue_id and conn:
        from src.db import get_opus_attempts_for_issue
        opus_attempts = get_opus_attempts_for_issue(conn, issue_id)
        if opus_attempts >= MAX_OPUS_PER_ISSUE:
            return None  # Exhausted attempts on this issue

    return tiers[tier_idx].copy()


def get_next_tier(current_tier: dict) -> dict | None:
    """Get the next tier up for escalation. Returns None if already at max."""
    tiers = load_model_tiers()
    current_tier_num = current_tier["tier"]
    for t in tiers:
        if t["tier"] == current_tier_num + 1:
            return t.copy()
    return None


def get_tier_by_number(tier_num: int) -> dict | None:
    """Get a specific tier by number."""
    tiers = load_model_tiers()
    for t in tiers:
        if t["tier"] == tier_num:
            return t.copy()
    return None


def estimate_merge_probability(issue: dict, repo: dict, conn=None) -> tuple[float, list[str]]:
    """Estimate the probability that a submitted PR will be merged.

    Based on empirical analysis of 192 submitted PRs:
    - 2.6% overall merge rate (3 merges / 113 strikes)
    - help-wanted issues: 0.5 avg strikes vs 1.3 without
    - Repos with prior merges: near-guaranteed acceptance
    - Repos with ≥3 strikes: near-guaranteed rejection

    Returns (probability, reasons) where reasons explain the score.
    """
    reasons = []

    # --- Parse labels ---
    labels_raw = issue.get("labels") or "[]"
    if isinstance(labels_raw, str):
        try:
            labels = {l.lower() for l in json.loads(labels_raw)}
        except (json.JSONDecodeError, TypeError):
            labels = set()
    else:
        labels = {l.lower() for l in labels_raw}

    has_help_wanted = bool(labels & {"help wanted", "help-wanted"})

    # --- Get repo strike/merge history ---
    repo_merges = 0
    repo_strikes = 0
    if conn:
        try:
            repo_id = repo.get("id") or repo.get("rid")
            if repo_id:
                row = conn.execute(
                    "SELECT merges, strikes FROM repo_strikes WHERE repo_id = ?",
                    (repo_id,)
                ).fetchone()
                if row:
                    repo_merges = row["merges"] or 0
                    repo_strikes = row["strikes"] or 0
        except Exception:
            pass

    # --- Scoring ---
    # Base probability depends on whether repo invited contributions
    if has_help_wanted:
        prob = 0.45
        reasons.append("+0.45 base (help-wanted label)")
    else:
        prob = 0.10
        reasons.append("+0.10 base (no help-wanted)")

    # Prior merge relationship is the strongest positive signal
    if repo_merges > 0:
        bonus = min(0.30, repo_merges * 0.15)
        prob += bonus
        reasons.append(f"+{bonus:.2f} prior merges ({repo_merges})")

    # Strike history is the strongest negative signal
    if repo_strikes >= 5:
        prob -= 0.50
        reasons.append(f"-0.50 high strikes ({repo_strikes})")
    elif repo_strikes >= 3:
        prob -= 0.30
        reasons.append(f"-0.30 moderate strikes ({repo_strikes})")
    elif repo_strikes >= 1:
        prob -= 0.15
        reasons.append(f"-0.15 has strikes ({repo_strikes})")
    elif repo_strikes == 0:
        # Zero strikes = welcoming repo (either new or tolerant)
        prob += 0.10
        reasons.append("+0.10 zero strikes (welcoming repo)")
        # Extra bonus if we've submitted before and they haven't complained
        if conn:
            try:
                repo_id = repo.get("id") or repo.get("rid")
                if repo_id:
                    prior = conn.execute(
                        "SELECT COUNT(*) FROM contributions WHERE repo_id = ? AND status = 'pr_created'",
                        (repo_id,)
                    ).fetchone()[0]
                    if prior >= 2:
                        prob += 0.05
                        reasons.append(f"+0.05 tolerant repo ({prior} prior PRs, 0 strikes)")
            except Exception:
                pass

    # Issue context quality
    body_len = len(issue.get("body") or "")
    comments = issue.get("comments_count") or 0

    if body_len >= 500:
        prob += 0.05
        reasons.append("+0.05 good body length")
    elif body_len < 100:
        prob -= 0.05
        reasons.append("-0.05 very short body")

    if comments >= 3:
        prob += 0.05
        reasons.append("+0.05 well-discussed issue")
    elif comments == 0:
        prob -= 0.05
        reasons.append("-0.05 zero comments")

    # Enhancement/feature labels are harder to get merged
    if labels & {"enhancement", "feature", "feature-request", "feature request"}:
        prob -= 0.15
        reasons.append("-0.15 enhancement/feature label")

    # Mega-repos without help-wanted are risky
    stars = repo.get("stars") or 0
    if stars > 100000 and not has_help_wanted:
        prob -= 0.05
        reasons.append("-0.05 mega-repo without help-wanted")

    prob = round(max(0.0, min(1.0, prob)), 2)
    return prob, reasons
