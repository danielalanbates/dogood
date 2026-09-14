"""Feedback loop: polls GitHub notifications, analyzes sentiment, takes action."""

from src.config import primary_model
import asyncio
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from src.config import (
    GITHUB_USERNAME, FEEDBACK_POLL_INTERVAL_SECONDS,
    HOSTILE_SENTIMENT_THRESHOLD, LOG_FILE, PROJECT_ROOT,
    MAX_OPUS_PER_ISSUE, ANTI_AI_KEYWORDS as ANTI_AI_KEYWORDS_CONFIG,
)
from src.concurrency import ConnectionPool, LogWriter
from src.db import (
    add_to_blacklist, remove_from_blacklist, is_blacklisted,
    add_learned_pattern, add_sponsor, get_opus_usage_for_repo,
    get_contribution_by_pr_url, update_feedback_status,
)
from src.utils import now_iso
from src.telegram import notify_github_attention, notify_plain


def _generate_ai_response(sentiment: str, reviewer_comment: str, reviewer: str,
                           repo_full_name: str, pr_number: str) -> str:
    """Generate a contextual AI response using Claude CLI (OAuth/subscription auth)."""
    import shutil

    if not shutil.which("claude"):
        print("  claude CLI not found, skipping AI response", flush=True)
        return ""

    guidelines = {
        "positive": (
            "The reviewer left a positive comment on our PR. Write a warm, genuine "
            "thank-you response. Be brief (1-3 sentences). Match their energy and tone. "
            "If they mentioned specific things they liked, acknowledge those."
        ),
        "constructive": (
            "The reviewer left constructive feedback on our PR. Acknowledge their feedback "
            "thoughtfully, let them know we'll address their points. Be specific about what "
            "they raised. Keep it brief and professional (2-4 sentences)."
        ),
        "hostile": (
            "The reviewer was hostile or anti-AI. Write a gracious, dignified exit message. "
            "Thank them for their time, say we'll withdraw the PR, and wish the project well. "
            "Do NOT be defensive or argue. Be kind and brief (2-3 sentences)."
        ),
        "sponsor": (
            "The reviewer mentioned sponsoring, donating, or financially supporting our work. "
            "Express genuine gratitude. Be warm but not over-the-top. Brief (1-3 sentences)."
        ),
        "regretful": (
            "The reviewer previously rejected us but is now reconsidering or apologizing. "
            "Be gracious and welcoming. No grudges. Express willingness to help. "
            "Brief (1-3 sentences)."
        ),
        "payment_request": (
            "The reviewer is asking about payment or bounty payouts. Thank them and direct "
            "them to email daniel@batesai.org for payment details. Brief (1-2 sentences)."
        ),
        "job_inquiry": (
            "The reviewer is offering a job or freelance opportunity. Thank them for the "
            "opportunity and direct them to email daniel@batesai.org to discuss. "
            "Brief (1-2 sentences)."
        ),
        "contact_request": (
            "The reviewer wants to get in touch. Thank them and provide daniel@batesai.org "
            "as the best contact. Brief (1-2 sentences)."
        ),
    }

    guideline = guidelines.get(sentiment, guidelines["constructive"])

    prompt = (
        f"You are responding to a GitHub PR review comment as an open-source contributor "
        f"named Daniel (github: danielalanbates). You contribute to open-source projects "
        f"to help the community.\n\n"
        f"Repo: {repo_full_name}\n"
        f"PR: #{pr_number}\n"
        f"Reviewer: @{reviewer}\n"
        f"Their comment:\n{reviewer_comment[:1000]}\n\n"
        f"Guidelines: {guideline}\n\n"
        f"IMPORTANT: Reply in the same language the reviewer used. If they wrote in "
        f"Japanese, reply in Japanese. If Spanish, reply in Spanish. Etc.\n\n"
        f"Write ONLY the response text. No markdown headers, no quotes, no meta-commentary."
    )

    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--model", primary_model(),
             "--effort", "low",
             "--max-turns", "1", "--output-format", "text"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception as e:
        print(f"  AI response generation failed: {e}", flush=True)

    return ""


# Polite exit template
POLITE_EXIT = (
    "Thank you for the feedback! I appreciate you taking the time to review. "
    "I'll withdraw this PR. Wishing this project continued success!"
)

# Compassion re-engagement template
COMPASSION_REENGAGEMENT = (
    "No worries at all! Happy to help. Let me take a look at this."
)

# Sentiment keywords for quick classification before AI analysis
HOSTILE_KEYWORDS = {
    "spam", "this is spam", "garbage", "terrible", "awful", "worst", "useless",
    "stop submitting", "go away", "not welcome", "ban this", "unwanted",
    "waste of time", "low quality", "low-quality", "junk",
}
ANTI_AI_KEYWORDS = {
    "no ai", "no llm", "ai-generated", "ban ai", "no bots",
    "ai contributions not accepted", "ai-free", "no machine",
}
POSITIVE_KEYWORDS = {
    "lgtm", "looks good", "great", "nice", "thank", "awesome",
    "well done", "excellent", "approved", "perfect", "wonderful",
    "impressive", "helpful", "appreciate",
}
SPONSOR_KEYWORDS = {
    "sponsor", "sponsoring", "donate", "donation", "fund", "funding",
    "support you", "buy you a coffee", "buy me a coffee", "buymeacoffee",
    "tip", "patreon", "ko-fi", "kofi", "coffee link", "buy a coffee",
    "support your work", "support this project", "contribute financially",
    "github sponsors", "open collective",
}
REGRET_KEYWORDS = {
    "sorry", "apologize", "apologies", "my bad", "overreacted",
    "reconsidered", "changed my mind", "give it another try",
    "come back", "welcome back",
}
# Keywords that mean Daniel needs to be notified via Telegram
CONTACT_KEYWORDS = {
    "email me", "email you", "send me an email", "send you an email",
    "your email", "my email is", "contact me", "contact you",
    "reach out to me", "reach out to you", "get in touch with me",
    "get in touch with you", "message me", "dm me", "direct message",
    "how can i reach", "talk to you", "how do i contact",
    "what is your email", "what's your email",
}
PAYMENT_KEYWORDS = {
    "payment", "pay you", "paypal", "venmo", "bank", "invoice",
    "compensation", "reward", "bounty payout", "send money",
    "wire transfer", "crypto", "wallet address",
}
JOB_KEYWORDS = {
    "hire you", "hiring you", "we're hiring", "job offer", "work for us",
    "join our team", "freelance work", "consulting gig",
    "interested in working with you", "full-time position", "part-time position",
}


def _detect_language(text: str) -> str:
    """Detect language from text using character frequency analysis.

    Returns ISO 639-1 code: 'en', 'zh', 'ja', 'ko', 'ru', 'ar', 'es', 'pt', 'de', 'fr', etc.
    """
    if not text or len(text) < 10:
        return "en"

    # Count character ranges
    cjk = 0      # Chinese/Japanese shared
    hiragana = 0  # Japanese-specific
    katakana = 0  # Japanese-specific
    hangul = 0    # Korean
    cyrillic = 0  # Russian/Ukrainian/etc
    arabic = 0    # Arabic/Persian
    latin = 0
    total = 0

    for ch in text:
        cp = ord(ch)
        if cp < 128:
            if ch.isalpha():
                latin += 1
            total += 1
        elif 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:
            cjk += 1; total += 1
        elif 0x3040 <= cp <= 0x309F:
            hiragana += 1; total += 1
        elif 0x30A0 <= cp <= 0x30FF:
            katakana += 1; total += 1
        elif 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF:
            hangul += 1; total += 1
        elif 0x0400 <= cp <= 0x04FF:
            cyrillic += 1; total += 1
        elif 0x0600 <= cp <= 0x06FF or 0x0750 <= cp <= 0x077F:
            arabic += 1; total += 1
        else:
            total += 1

    if total == 0:
        return "en"

    # Japanese: has hiragana/katakana
    if (hiragana + katakana) > total * 0.05:
        return "ja"
    # Korean
    if hangul > total * 0.1:
        return "ko"
    # Chinese (CJK without Japanese markers)
    if cjk > total * 0.1:
        return "zh"
    # Russian/Cyrillic
    if cyrillic > total * 0.15:
        return "ru"
    # Arabic
    if arabic > total * 0.15:
        return "ar"

    # For Latin-script languages, check for common words
    text_lower = text.lower()
    # Spanish
    if any(w in text_lower for w in [" está ", " también ", " pero ", " porque ", " gracias "]):
        return "es"
    # Portuguese
    if any(w in text_lower for w in [" também ", " então ", " obrigado ", " não ", " muito "]):
        return "pt"
    # German
    if any(w in text_lower for w in [" und ", " nicht ", " aber ", " danke ", " bitte "]):
        return "de"
    # French
    if any(w in text_lower for w in [" merci ", " mais ", " aussi ", " avec ", " très "]):
        return "fr"

    return "en"


# Pre-translated canned responses for top languages
_TRANSLATIONS = {
    "thank_review": {
        "en": "Thank you for the review! Glad it helps. 🙏",
        "zh": "感谢您的审查！很高兴能有所帮助。🙏",
        "ja": "レビューありがとうございます！お役に立てて嬉しいです。🙏",
        "ko": "리뷰해 주셔서 감사합니다! 도움이 되어 기쁩니다. 🙏",
        "ru": "Спасибо за ревью! Рад, что помогло. 🙏",
        "es": "¡Gracias por la revisión! Me alegra que ayude. 🙏",
        "pt": "Obrigado pela revisão! Fico feliz em ajudar. 🙏",
        "de": "Danke für das Review! Freut mich, dass es hilft. 🙏",
        "fr": "Merci pour la revue ! Content que ça aide. 🙏",
        "ar": "شكراً على المراجعة! سعيد أنها مفيدة. 🙏",
    },
    "polite_exit": {
        "en": POLITE_EXIT,
        "zh": "感谢您的反馈！感谢您抽出时间进行审查。我将撤回此 PR。祝项目一切顺利！",
        "ja": "フィードバックありがとうございます！レビューにお時間をいただき感謝します。このPRを取り下げます。プロジェクトの成功をお祈りしています！",
        "ko": "피드백 감사합니다! 리뷰에 시간을 내주셔서 감사합니다. 이 PR을 철회하겠습니다. 프로젝트의 성공을 기원합니다!",
        "ru": "Спасибо за обратную связь! Благодарю за время на ревью. Я отзову этот PR. Желаю проекту успехов!",
        "es": "¡Gracias por los comentarios! Agradezco que se haya tomado el tiempo de revisar. Retiraré este PR. ¡Les deseo mucho éxito!",
        "pt": "Obrigado pelo feedback! Agradeço o tempo dedicado à revisão. Vou retirar este PR. Desejo sucesso ao projeto!",
        "de": "Danke für das Feedback! Ich schätze die Zeit für das Review. Ich ziehe diesen PR zurück. Viel Erfolg weiterhin!",
        "fr": "Merci pour le retour ! J'apprécie le temps consacré à la revue. Je retire cette PR. Je souhaite beaucoup de succès au projet !",
        "ar": "شكراً على الملاحظات! أقدر وقتكم في المراجعة. سأسحب هذا الطلب. أتمنى النجاح المستمر للمشروع!",
    },
    "sponsor_thanks": {
        "en": "Thank you so much for the kind words and support! 🙏",
        "zh": "非常感谢您的支持和鼓励！🙏",
        "ja": "温かいお言葉とご支援、本当にありがとうございます！🙏",
        "ko": "따뜻한 말씀과 응원에 진심으로 감사드립니다! 🙏",
        "ru": "Большое спасибо за добрые слова и поддержку! 🙏",
        "es": "¡Muchas gracias por las amables palabras y el apoyo! 🙏",
        "pt": "Muito obrigado pelas palavras gentis e pelo apoio! 🙏",
        "de": "Vielen Dank für die freundlichen Worte und die Unterstützung! 🙏",
        "fr": "Merci beaucoup pour les mots gentils et le soutien ! 🙏",
        "ar": "شكراً جزيلاً على الكلمات الطيبة والدعم! 🙏",
    },
    "contact_reply": {
        "en": "Thanks for reaching out! The best way to contact me is daniel@batesai.org.",
        "zh": "感谢您的联系！联系我的最佳方式是 daniel@batesai.org。",
        "ja": "ご連絡ありがとうございます！最適な連絡先は daniel@batesai.org です。",
        "ko": "연락해 주셔서 감사합니다! 저에게 연락하는 가장 좋은 방법은 daniel@batesai.org 입니다.",
        "ru": "Спасибо за обращение! Лучший способ связаться со мной — daniel@batesai.org.",
        "es": "¡Gracias por comunicarse! La mejor forma de contactarme es daniel@batesai.org.",
        "pt": "Obrigado pelo contato! A melhor forma de me contactar é daniel@batesai.org.",
        "de": "Danke für die Kontaktaufnahme! Am besten erreichen Sie mich unter daniel@batesai.org.",
        "fr": "Merci de nous contacter ! Le meilleur moyen de me joindre est daniel@batesai.org.",
        "ar": "شكراً للتواصل! أفضل طريقة للاتصال بي هي daniel@batesai.org.",
    },
    "payment_reply": {
        "en": "Thanks for asking! For payment details, please email daniel@batesai.org and I'll get back to you promptly.",
        "zh": "感谢您的询问！有关付款详情，请发邮件至 daniel@batesai.org，我会尽快回复。",
        "ja": "お問い合わせありがとうございます！お支払いの詳細については daniel@batesai.org までメールをお送りください。速やかにお返事いたします。",
        "ko": "문의해 주셔서 감사합니다! 결제 관련 내용은 daniel@batesai.org 로 이메일 주시면 빠르게 답변드리겠습니다.",
        "ru": "Спасибо за вопрос! По вопросам оплаты, пожалуйста, напишите на daniel@batesai.org — отвечу оперативно.",
        "es": "¡Gracias por preguntar! Para detalles de pago, envíe un correo a daniel@batesai.org y le responderé pronto.",
        "pt": "Obrigado por perguntar! Para detalhes de pagamento, envie um email para daniel@batesai.org e responderei rapidamente.",
        "de": "Danke für die Anfrage! Für Zahlungsdetails schreiben Sie bitte an daniel@batesai.org — ich melde mich zeitnah.",
        "fr": "Merci de demander ! Pour les détails de paiement, envoyez un email à daniel@batesai.org et je vous répondrai rapidement.",
        "ar": "شكراً على السؤال! لتفاصيل الدفع، يرجى مراسلتي على daniel@batesai.org وسأرد عليك فوراً.",
    },
    "job_reply": {
        "en": "Thank you for the opportunity! Please reach out to daniel@batesai.org and I'd be happy to discuss further.",
        "zh": "感谢您提供的机会！请联系 daniel@batesai.org，我很乐意进一步讨论。",
        "ja": "このような機会をいただきありがとうございます！daniel@batesai.org までご連絡いただければ、詳しくお話しできれば幸いです。",
        "ko": "기회를 주셔서 감사합니다! daniel@batesai.org 로 연락해 주시면 자세히 논의하겠습니다.",
        "ru": "Спасибо за предложение! Напишите на daniel@batesai.org — буду рад обсудить подробнее.",
        "es": "¡Gracias por la oportunidad! Contacte a daniel@batesai.org y estaré encantado de discutirlo.",
        "pt": "Obrigado pela oportunidade! Entre em contato pelo daniel@batesai.org e terei prazer em discutir mais.",
        "de": "Danke für die Gelegenheit! Schreiben Sie an daniel@batesai.org — ich freue mich auf den Austausch.",
        "fr": "Merci pour l'opportunité ! Contactez daniel@batesai.org et je serai ravi d'en discuter.",
        "ar": "شكراً على الفرصة! يرجى التواصل على daniel@batesai.org وسأكون سعيداً بالمناقشة.",
    },
}


def _get_translated(key: str, lang: str) -> str:
    """Get a translated canned response, falling back to English."""
    translations = _TRANSLATIONS.get(key, {})
    return translations.get(lang, translations.get("en", ""))


class FeedbackLoop:
    """Monitors GitHub notifications and processes review feedback."""

    def __init__(self, pool: ConnectionPool = None):
        self.pool = pool or ConnectionPool()
        self.log_writer = LogWriter(LOG_FILE)
        self.username = GITHUB_USERNAME

    async def run_once(self) -> dict:
        """Run one feedback cycle. Returns stats dict."""
        stats = {"processed": 0, "positive": 0, "constructive": 0,
                 "hostile": 0, "anti_ai": 0, "sponsor": 0, "regretful": 0,
                 "payment_request": 0, "job_inquiry": 0, "contact_request": 0}

        notifications = self._fetch_notifications()
        if not notifications:
            return stats

        for notif in notifications:
            try:
                result = await self._process_notification(notif)
                if result:
                    stats["processed"] += 1
                    stats[result] = stats.get(result, 0) + 1
            except Exception as e:
                print(f"  Error processing notification: {e}")

        return stats

    async def run_continuous(self):
        """Run feedback loop continuously as a background task."""
        while True:
            try:
                stats = await self.run_once()
                if stats["processed"] > 0:
                    print(f"  Feedback cycle: {stats}")
            except Exception as e:
                print(f"  Feedback loop error: {e}")
            await asyncio.sleep(FEEDBACK_POLL_INTERVAL_SECONDS)

    def _fetch_notifications(self) -> list:
        """Fetch GitHub notifications, process PRs, and mark non-PR ones as read."""
        for attempt in range(2):
            try:
                result = subprocess.run(
                    ["gh", "api", "notifications"],
                    capture_output=True, text=True, timeout=60
                )
                if result.returncode != 0 or not result.stdout.strip():
                    return []

                all_notifs = json.loads(result.stdout)
                pr_notifs = []
                for notif in all_notifs:
                    if notif.get("subject", {}).get("type") == "PullRequest":
                        pr_notifs.append(notif)
                    else:
                        # Mark non-PR notifications (CheckSuite, Issue, etc.) as read
                        self._mark_notification_read(notif)

                return pr_notifs
            except subprocess.TimeoutExpired:
                if attempt == 0:
                    print("  gh api notifications timed out (60s), retrying...", flush=True)
                    continue
                print("  gh api notifications timed out on retry, skipping cycle", flush=True)
            except Exception as e:
                print(f"  Failed to fetch notifications: {e}")
                break
        return []

    async def _process_notification(self, notif: dict) -> str | None:
        """Process a single notification. Returns sentiment category or None."""
        subject = notif.get("subject", {})
        pr_url = subject.get("url", "")
        repo_full_name = notif.get("repository", {}).get("full_name", "")

        if not pr_url:
            # Still mark as read so it doesn't pile up
            self._mark_notification_read(notif)
            return None

        # Fetch PR reviews and comments
        reviews = self._fetch_pr_reviews(pr_url)
        comments = self._fetch_pr_comments(pr_url)

        all_feedback = reviews + comments
        if not all_feedback:
            self._mark_notification_read(notif)
            return None

        # Check DB for already-processed reviews to avoid duplicates
        conn = self.pool.get()
        processed_keys = set()
        existing = conn.execute(
            "SELECT pr_url, reviewer, body FROM pr_reviews WHERE pr_url = ?",
            (pr_url,)
        ).fetchall()
        for row in existing:
            processed_keys.add((row["pr_url"], row["reviewer"], row["body"][:200]))

        result_sentiment = None

        for item in all_feedback:
            body = item.get("body", "")
            reviewer = item.get("user", {}).get("login", "")
            if reviewer == self.username:
                continue  # Skip our own comments

            # Skip bot accounts — their comments are automated, not human feedback
            user_type = item.get("user", {}).get("type", "")
            reviewer_lower = reviewer.lower()
            reviewer_base = reviewer_lower.replace("[bot]", "").rstrip("-")
            known_bots = {"claassistant", "cla-assistant", "allcontributors", "dependabot",
                          "renovate", "codecov", "coderabbitai", "github-actions",
                          "autogpt-reviewer", "sonarcloud", "netlify", "vercel",
                          "stale", "lock", "gitguardian", "sympy-bot", "autofix-ci",
                          "codeclimate", "coveralls", "snyk-bot", "mergify",
                          "imgbot", "greenkeeper", "percy", "cypress",
                          "cloudflare-workers-and-pages", "linear", "sentry-io"}
            if (user_type == "Bot" or reviewer.endswith("[bot]")
                    or reviewer.endswith("-bot") or reviewer.endswith("-reviewer")
                    or reviewer_base in known_bots
                    or reviewer.startswith("github-actions")):
                continue

            # Skip already-processed reviews
            dedup_key = (pr_url, reviewer, body[:200])
            if dedup_key in processed_keys:
                continue

            sentiment = self._classify_sentiment(body)

            # Record in DB
            conn.execute(
                """INSERT INTO pr_reviews (pr_url, reviewer, review_type, body, sentiment)
                   VALUES (?, ?, ?, ?, ?)""",
                (pr_url, reviewer, item.get("state", "comment"),
                 body[:2000], sentiment),
            )
            conn.commit()

            # Take action based on sentiment
            comment_id = item.get("id")
            action = await self._take_action(sentiment, pr_url, repo_full_name,
                                             body, reviewer, comment_id=comment_id)
            if action:
                conn.execute(
                    """UPDATE pr_reviews SET action_taken = ?
                       WHERE rowid = (
                           SELECT rowid FROM pr_reviews
                           WHERE pr_url = ? AND reviewer = ?
                           ORDER BY created_at DESC LIMIT 1
                       )""",
                    (action, pr_url, reviewer),
                )
                conn.commit()

            result_sentiment = sentiment

        # Always mark notification as read — even if only bots commented
        self._mark_notification_read(notif)

        return result_sentiment

    def _mark_notification_read(self, notif: dict):
        """Mark a GitHub notification as read."""
        notif_id = notif.get("id")
        if notif_id:
            try:
                subprocess.run(
                    ["gh", "api", "-X", "PATCH", f"notifications/threads/{notif_id}"],
                    capture_output=True, text=True, timeout=10
                )
            except Exception:
                pass

    def _fetch_pr_reviews(self, pr_api_url: str) -> list:
        """Fetch reviews for a PR."""
        try:
            result = subprocess.run(
                ["gh", "api", f"{pr_api_url}/reviews"],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0:
                return json.loads(result.stdout)
        except Exception:
            pass
        return []

    def _fetch_pr_comments(self, pr_api_url: str) -> list:
        """Fetch issue comments on a PR."""
        try:
            # Convert pulls URL to issues comments URL
            comments_url = pr_api_url.replace("/pulls/", "/issues/") + "/comments"
            result = subprocess.run(
                ["gh", "api", comments_url],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0:
                return json.loads(result.stdout)
        except Exception:
            pass
        return []

    def _classify_sentiment(self, text: str) -> str:
        """Classify review text sentiment using keyword matching."""
        text_lower = text.lower()

        # Check anti-AI first (highest priority)
        if any(kw in text_lower for kw in ANTI_AI_KEYWORDS):
            return "hostile"  # anti-AI treated as hostile for action purposes

        # CLA/DCO-related text — needs Daniel's action (he must sign)
        cla_signals = {"cla", "contributor license agreement", "contributor agreement",
                       "generative ai agreement", "ai contribution agreement",
                       "sign the agreement", "signed-off-by", "dco"}
        if any(kw in text_lower for kw in cla_signals):
            return "cla_request"

        # Check for payment/job/contact requests (notify Daniel via Telegram)
        # Guard: if text looks like code review feedback, skip contact/payment/job checks
        _code_review_signals = {"commit", "format", "lint", "test", "fix", "refactor",
                                "nit", "typo", "change", "update", "address",
                                "suggestion", "review", "pr ", "pull request",
                                "merge", "rebase", "squash", "ci ", "pipeline"}
        is_code_review = sum(1 for s in _code_review_signals if s in text_lower) >= 2

        if any(kw in text_lower for kw in PAYMENT_KEYWORDS) and not is_code_review:
            return "payment_request"
        if any(kw in text_lower for kw in JOB_KEYWORDS) and not is_code_review:
            return "job_inquiry"
        if any(kw in text_lower for kw in CONTACT_KEYWORDS) and not is_code_review:
            return "contact_request"

        # Check for sponsor mentions
        if any(kw in text_lower for kw in SPONSOR_KEYWORDS):
            return "sponsor"

        # Check for regret/re-engagement
        if any(kw in text_lower for kw in REGRET_KEYWORDS):
            return "regretful"

        # Check hostile — require 3+ matches to avoid false positives
        hostile_count = sum(1 for kw in HOSTILE_KEYWORDS if kw in text_lower)
        if hostile_count >= 3:
            return "hostile"

        # Check positive
        positive_count = sum(1 for kw in POSITIVE_KEYWORDS if kw in text_lower)
        if positive_count >= 1:
            return "positive"

        # Check for constructive feedback (mentions fixes/changes needed)
        constructive_keywords = {"could you", "please", "instead", "should",
                                 "consider", "suggestion", "nit", "minor",
                                 "change", "fix", "update", "modify"}
        if any(kw in text_lower for kw in constructive_keywords):
            return "constructive"

        return "constructive"  # default to constructive

    def _api_url_to_html(self, pr_api_url: str) -> str:
        """Convert API URL like repos/owner/repo/pulls/123 to HTML URL."""
        return pr_api_url.replace("https://api.github.com/repos/", "https://github.com/").replace("/pulls/", "/pull/")

    def _react_to_comment(self, owner_repo: str, comment_id: int, reaction: str = "+1"):
        """Reactions are no longer posted: every GitHub post needs Daniel's approval,
        and asking about emoji reactions would be noise."""
        return
        if not comment_id:
            return
        time.sleep(10)
        try:
            subprocess.run(
                ["gh", "api", "-X", "POST",
                 f"repos/{owner_repo}/issues/comments/{comment_id}/reactions",
                 "-f", f"content={reaction}"],
                capture_output=True, text=True, timeout=10
            )
        except Exception:
            pass

    async def _take_action(self, sentiment: str, pr_url: str,
                           repo_full_name: str, body: str,
                           reviewer: str, comment_id: int = None) -> str | None:
        """Take action based on sentiment. Returns action name."""
        conn = self.pool.get()

        # Extract PR number from URL for gh commands
        pr_number = pr_url.split("/")[-1]
        owner_repo = "/".join(pr_url.split("/repos/")[1].split("/pulls/")[0:1]) if "/repos/" in pr_url else repo_full_name
        html_url = self._api_url_to_html(pr_url)

        # Detect reviewer's language for localized responses
        lang = _detect_language(body)

        # --- Only notify Daniel for actionable items (payment, CLA, contact, info requests) ---
        _actionable_sentiments = {"payment_request", "job_inquiry", "contact_request", "cla_request", "sponsor"}
        if sentiment in _actionable_sentiments:
            notify_github_attention(
                sentiment, repo_full_name, html_url,
                f"@{reviewer}: {body[:200]}"
            )

        # --- Generate AI response (falls back to canned if AI fails) ---
        ai_response = _generate_ai_response(
            sentiment, body, reviewer, repo_full_name, pr_number
        )

        # --- Take automated action based on sentiment ---
        if sentiment == "payment_request":
            reply = ai_response or _get_translated("payment_reply", lang)
            self._comment_on_pr(owner_repo, pr_number, reply)
            return "telegram_notified"

        if sentiment == "job_inquiry":
            reply = ai_response or _get_translated("job_reply", lang)
            self._comment_on_pr(owner_repo, pr_number, reply)
            return "telegram_notified"

        if sentiment == "contact_request":
            reply = ai_response or _get_translated("contact_reply", lang)
            self._comment_on_pr(owner_repo, pr_number, reply)
            return "telegram_notified"

        if sentiment == "sponsor":
            self._react_to_comment(owner_repo, comment_id, "+1")
            add_sponsor(conn, reviewer, repo_full_name, "donation",
                        json.dumps({"quote": body[:500]}))
            reply = ai_response or _get_translated("sponsor_thanks", lang)
            self._comment_on_pr(owner_repo, pr_number, reply)
            # Notify Daniel — donor repos get opus priority
            notify_plain(
                f"\U0001f4b0 DONOR DETECTED\n"
                f"User: @{reviewer}\n"
                f"Repo: {repo_full_name}\n"
                f"Quote: {body[:200]}\n\n"
                f"This repo is now opus-priority!"
            )
            return "thanked"

        if sentiment == "positive":
            self._react_to_comment(owner_repo, comment_id, "+1")
            reply = ai_response or _get_translated("thank_review", lang)
            self._comment_on_pr(owner_repo, pr_number, reply)
            return "thanked"

        elif sentiment == "constructive":
            # Check if this is CLA/DCO related — agent can't sign agreements
            body_lower = body.lower()
            cla_signals = {"cla", "contributor license agreement", "sign the agreement",
                           "signed-off-by", "dco", "contributor agreement",
                           "generative ai agreement", "ai contribution agreement"}
            is_cla = any(kw in body_lower for kw in cla_signals)

            if is_cla:
                action_note = "CLA/DCO request — requires manual signing by Daniel"
            else:
                # Queue for automated re-fix: find matching contribution and mark needs_revision
                contribution = get_contribution_by_pr_url(conn, html_url)
                if contribution:
                    update_feedback_status(
                        conn, contribution["id"],
                        status="needs_revision",
                        feedback_text=body[:5000],
                        feedback_pr_url=html_url,
                        feedback_reviewer=reviewer,
                        mandatory_model="opus-high",
                    )
                    action_note = "Queued for automated revision (priority #1)"
                else:
                    action_note = "No matching contribution found — manual review needed"

            # Post AI-generated acknowledgment
            if ai_response:
                self._comment_on_pr(owner_repo, pr_number, ai_response)

            # Log for tracking
            self.log_writer.append_entry(
                f"## {datetime.now().strftime('%Y-%m-%d %H:%M')} — REVIEW RECEIVED\n"
                f"**Repo:** {repo_full_name}\n"
                f"**PR:** #{pr_number}\n"
                f"**Reviewer:** {reviewer}\n"
                f"**Feedback:** {body[:300]}\n"
                f"**Action:** {action_note}\n"
                f"---"
            )

            # Check for learned patterns
            self._check_for_patterns(body, repo_full_name)

            return "fix_pushed"

        elif sentiment == "hostile":
            # Check for anti-AI policy
            body_lower = body.lower()
            is_anti_ai = any(kw in body_lower for kw in ANTI_AI_KEYWORDS)

            # AI-generated gracious exit
            reply = ai_response or _get_translated("polite_exit", lang)
            self._comment_on_pr(owner_repo, pr_number, reply)

            # Close PR
            self._close_pr(owner_repo, pr_number)

            # Blacklist repo
            reason = "anti_ai_policy" if is_anti_ai else "hostile_maintainer"
            add_to_blacklist(conn, repo_full_name, reason,
                             details=json.dumps({"reviewer": reviewer,
                                                 "quote": body[:500]}))

            self.log_writer.append_entry(
                f"## {datetime.now().strftime('%Y-%m-%d %H:%M')} — BLOCKED\n"
                f"**Repo:** {repo_full_name}\n"
                f"**Reason:** {reason}\n"
                f"**Reviewer:** {reviewer}\n"
                f"**Quote:** {body[:200]}\n"
                f"**Action needed:** No — repo blacklisted\n"
                f"---"
            )
            return "repo_blacklisted"

        elif sentiment == "regretful":
            # Un-blacklist and re-engage
            if is_blacklisted(conn, repo_full_name):
                remove_from_blacklist(conn, repo_full_name)
                reply = ai_response or COMPASSION_REENGAGEMENT
                self._comment_on_pr(owner_repo, pr_number, reply)

                self.log_writer.append_entry(
                    f"## {datetime.now().strftime('%Y-%m-%d %H:%M')} — RE-ENGAGED\n"
                    f"**Repo:** {repo_full_name}\n"
                    f"**User:** {reviewer}\n"
                    f"**Details:** Repo un-blacklisted after positive re-engagement\n"
                    f"**Action needed:** No\n"
                    f"---"
                )
                return "re_engaged"

        return None

    def _comment_on_pr(self, owner_repo: str, pr_number: str, comment: str):
        """Queue a PR comment for Daniel's approval."""
        from src import approvals
        approvals.request(
            "comment",
            f"Comment on https://github.com/{owner_repo}/pull/{pr_number}:\n\"{comment[:800]}\"",
            [["gh", "pr", "comment", pr_number, "--repo", owner_repo, "--body", comment]],
        )

    def _close_pr(self, owner_repo: str, pr_number: str):
        """Queue closing a PR for Daniel's approval."""
        from src import approvals
        approvals.request(
            "close PR",
            f"Close https://github.com/{owner_repo}/pull/{pr_number}",
            [["gh", "pr", "close", pr_number, "--repo", owner_repo]],
        )

    def _check_for_patterns(self, body: str, repo_full_name: str):
        """Extract learned patterns from review feedback."""
        conn = self.pool.get()
        body_lower = body.lower()

        # Check for commit format feedback
        commit_keywords = ["conventional commit", "commit message", "commit format",
                           "please use", "commit style"]
        if any(kw in body_lower for kw in commit_keywords):
            add_learned_pattern(conn, "commit_format", body[:500],
                                repo_full_name, 0.7, "review_feedback")

        # Check for DCO requirement
        if "signed-off-by" in body_lower or "dco" in body_lower:
            add_learned_pattern(conn, "dco_required", "true",
                                repo_full_name, 0.9, "review_feedback")

        # Check for global patterns (update CLAUDE.md if seen in 3+ repos)
        self._maybe_update_claude_md(conn)

    def _maybe_update_claude_md(self, conn):
        """If a pattern appears in 3+ repos, add it to CLAUDE.md as a global rule."""
        patterns = conn.execute("""
            SELECT pattern_type, pattern_value, COUNT(DISTINCT repo_full_name) as repo_count
            FROM learned_patterns
            WHERE repo_full_name IS NOT NULL
            GROUP BY pattern_type, pattern_value
            HAVING repo_count >= 3
        """).fetchall()

        if not patterns:
            return

        claude_md_path = PROJECT_ROOT / "CLAUDE.md"
        if not claude_md_path.exists():
            return

        content = claude_md_path.read_text()

        # Add learned rules section if not present
        if "## Learned Rules" not in content:
            content += "\n\n## Learned Rules\n"
            content += "*Auto-discovered patterns from maintainer feedback:*\n\n"

        for p in patterns:
            rule_text = f"- **{p['pattern_type']}** (seen in {p['repo_count']} repos): {p['pattern_value'][:100]}"
            if rule_text not in content:
                content = content.rstrip() + "\n" + rule_text + "\n"
                # Also record as global pattern
                add_learned_pattern(conn, p["pattern_type"], p["pattern_value"],
                                    None, 0.9, "auto_global")

        claude_md_path.write_text(content)
