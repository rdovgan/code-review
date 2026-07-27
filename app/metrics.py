"""
Redis-backed metrics exposed in Prometheus text format via /metrics endpoint.

Counters survive restarts because they live in Redis.
Prometheus scrapes /metrics periodically and stores time series.
Grafana queries Prometheus for dashboards.

Dimensional data is stored as hashes with composite field "label1:label2":
  metrics:by_lang    → {"java:success": N, "java:failure": N, ...}
  metrics:by_project → {"rdovgan/dungeon:success": N, ...}
  metrics:by_author  → {"john:success": N, ...}
  metrics:findings_by_lang    → {"java:critical": N, ...}
  metrics:findings_by_project → {"rdovgan/dungeon:bug": N, ...}
  metrics:findings_by_author  → {"john:bug": N, ...}
  metrics:duration_by_project → {"rdovgan/dungeon:sum_ms": N, "rdovgan/dungeon:count": N, ...}
  metrics:ai_tokens_by_provider → {"claude:input": N, "claude:output": N, ...}
  metrics:semgrep_verify → {"kept": N, "dropped": N, "fail_open": N}
  metrics:semgrep_errors → {"timeout": N, "bad_exit": N, "invalid_json": N}

The `ai_tokens:<date>` key (no "metrics:" prefix, one per day) is owned and written by
AIReviewer for daily-budget enforcement — this module only reads it back for exposition,
so the key name must stay in sync with app/analyzers/ai_reviewer.py.
"""
from datetime import date

import redis


class Metrics:
    _WEBHOOKS_KEY = "metrics:webhooks"
    _REVIEWS_KEY = "metrics:reviews"
    _FINDINGS_SEVERITY_KEY = "metrics:findings:severity"
    _FINDINGS_SOURCE_KEY = "metrics:findings:source"
    _DURATION_SUM_KEY = "metrics:duration:sum_ms"
    _DURATION_COUNT_KEY = "metrics:duration:count"
    _BY_LANG_KEY = "metrics:by_lang"
    _BY_PROJECT_KEY = "metrics:by_project"
    _BY_AUTHOR_KEY = "metrics:by_author"
    _FINDINGS_BY_LANG_KEY = "metrics:findings_by_lang"
    _FINDINGS_BY_PROJECT_KEY = "metrics:findings_by_project"
    _FINDINGS_BY_AUTHOR_KEY = "metrics:findings_by_author"
    _EXTRA_FINDINGS_KEY = "metrics:extra_findings"
    _DURATION_BY_PROJECT_KEY = "metrics:duration_by_project"

    # AI cost — shared with app/analyzers/ai_reviewer.py, which owns the writes for
    # AI_TOKENS_TODAY_PREFIX (daily budget enforcement key).
    AI_TOKENS_TODAY_PREFIX = "ai_tokens"
    AI_TOKENS_BY_PROVIDER_KEY = "metrics:ai_tokens_by_provider"
    AI_BUDGET_EXCEEDED_KEY = "metrics:ai_budget_exceeded"

    # Semgrep AI verification — shared with app/analyzers/ai_reviewer.py (fail-open events)
    # and app/workers/celery_app.py (kept/dropped totals).
    SEMGREP_VERIFY_KEY = "metrics:semgrep_verify"

    # Semgrep runner errors — shared with app/analyzers/semgrep_runner.py.
    SEMGREP_ERRORS_KEY = "metrics:semgrep_errors"

    _TASK_RETRIES_KEY = "metrics:task_retries"

    def __init__(self, redis_url: str, ai_daily_token_budget: int = 0) -> None:
        self._r = redis.from_url(redis_url, decode_responses=True)
        self._ai_daily_token_budget = ai_daily_token_budget

    # --- write side (called from celery worker) ---

    def inc_webhook(self, status: str) -> None:
        """status: queued | ignored | already_queued | error | auth_failed"""
        try:
            self._r.hincrby(self._WEBHOOKS_KEY, status, 1)
        except Exception:
            pass

    def record_review(
        self,
        *,
        status: str,
        duration_ms: int,
        critical: int,
        bugs: int,
        perf: int,
        suggestions: int,
        semgrep_count: int,
        ai_count: int,
        language: str = "unknown",
        project: str = "unknown",
        author: str = "unknown",
    ) -> None:
        """status: success | failure | skipped"""
        total_findings = critical + bugs + perf + suggestions
        try:
            pipe = self._r.pipeline()

            # global counters
            pipe.hincrby(self._REVIEWS_KEY, status, 1)
            pipe.hincrby(self._FINDINGS_SEVERITY_KEY, "critical", critical)
            pipe.hincrby(self._FINDINGS_SEVERITY_KEY, "bug", bugs)
            pipe.hincrby(self._FINDINGS_SEVERITY_KEY, "performance", perf)
            pipe.hincrby(self._FINDINGS_SEVERITY_KEY, "suggestion", suggestions)
            pipe.hincrby(self._FINDINGS_SOURCE_KEY, "semgrep", semgrep_count)
            pipe.hincrby(self._FINDINGS_SOURCE_KEY, "ai", ai_count)
            pipe.incrby(self._DURATION_SUM_KEY, duration_ms)
            pipe.incr(self._DURATION_COUNT_KEY)

            # dimensional: by language
            pipe.hincrby(self._BY_LANG_KEY, f"{language}:{status}", 1)
            pipe.hincrby(self._FINDINGS_BY_LANG_KEY, f"{language}:critical", critical)
            pipe.hincrby(self._FINDINGS_BY_LANG_KEY, f"{language}:bug", bugs)
            pipe.hincrby(self._FINDINGS_BY_LANG_KEY, f"{language}:total", total_findings)

            # dimensional: by project
            pipe.hincrby(self._BY_PROJECT_KEY, f"{project}:{status}", 1)
            pipe.hincrby(self._FINDINGS_BY_PROJECT_KEY, f"{project}:critical", critical)
            pipe.hincrby(self._FINDINGS_BY_PROJECT_KEY, f"{project}:bug", bugs)
            pipe.hincrby(self._FINDINGS_BY_PROJECT_KEY, f"{project}:total", total_findings)
            if status != "skipped":
                pipe.hincrby(self._DURATION_BY_PROJECT_KEY, f"{project}:sum_ms", duration_ms)
                pipe.hincrby(self._DURATION_BY_PROJECT_KEY, f"{project}:count", 1)

            # dimensional: by author
            pipe.hincrby(self._BY_AUTHOR_KEY, f"{author}:{status}", 1)
            pipe.hincrby(self._FINDINGS_BY_AUTHOR_KEY, f"{author}:critical", critical)
            pipe.hincrby(self._FINDINGS_BY_AUTHOR_KEY, f"{author}:bug", bugs)
            pipe.hincrby(self._FINDINGS_BY_AUTHOR_KEY, f"{author}:total", total_findings)

            # extra findings beyond 5 per PR (for time-saved calculation)
            pipe.incrby(self._EXTRA_FINDINGS_KEY, max(0, total_findings - 5))

            pipe.execute()
        except Exception:
            pass

    def record_ai_tokens(self, provider: str, input_tokens: int, output_tokens: int) -> None:
        try:
            pipe = self._r.pipeline()
            pipe.hincrby(self.AI_TOKENS_BY_PROVIDER_KEY, f"{provider}:input", input_tokens)
            pipe.hincrby(self.AI_TOKENS_BY_PROVIDER_KEY, f"{provider}:output", output_tokens)
            pipe.execute()
        except Exception:
            pass

    def inc_ai_budget_exceeded(self) -> None:
        try:
            self._r.incr(self.AI_BUDGET_EXCEEDED_KEY)
        except Exception:
            pass

    def record_semgrep_verification(self, kept: int, total: int) -> None:
        dropped = max(0, total - kept)
        try:
            pipe = self._r.pipeline()
            pipe.hincrby(self.SEMGREP_VERIFY_KEY, "kept", kept)
            pipe.hincrby(self.SEMGREP_VERIFY_KEY, "dropped", dropped)
            pipe.execute()
        except Exception:
            pass

    def inc_semgrep_verify_fail_open(self) -> None:
        try:
            self._r.hincrby(self.SEMGREP_VERIFY_KEY, "fail_open", 1)
        except Exception:
            pass

    def inc_semgrep_error(self, reason: str) -> None:
        """reason: timeout | bad_exit | invalid_json"""
        try:
            self._r.hincrby(self.SEMGREP_ERRORS_KEY, reason, 1)
        except Exception:
            pass

    def inc_task_retry(self) -> None:
        try:
            self._r.incr(self._TASK_RETRIES_KEY)
        except Exception:
            pass

    # --- read side (called from FastAPI /metrics) ---

    def prometheus_text(self) -> str:
        try:
            webhooks = self._r.hgetall(self._WEBHOOKS_KEY)
            reviews = self._r.hgetall(self._REVIEWS_KEY)
            sev = self._r.hgetall(self._FINDINGS_SEVERITY_KEY)
            src = self._r.hgetall(self._FINDINGS_SOURCE_KEY)
            dur_sum = int(self._r.get(self._DURATION_SUM_KEY) or 0)
            dur_count = int(self._r.get(self._DURATION_COUNT_KEY) or 0)
            by_lang = self._r.hgetall(self._BY_LANG_KEY)
            by_project = self._r.hgetall(self._BY_PROJECT_KEY)
            by_author = self._r.hgetall(self._BY_AUTHOR_KEY)
            findings_by_lang = self._r.hgetall(self._FINDINGS_BY_LANG_KEY)
            findings_by_project = self._r.hgetall(self._FINDINGS_BY_PROJECT_KEY)
            findings_by_author = self._r.hgetall(self._FINDINGS_BY_AUTHOR_KEY)
            extra_findings = int(self._r.get(self._EXTRA_FINDINGS_KEY) or 0)
            duration_by_project = self._r.hgetall(self._DURATION_BY_PROJECT_KEY)
            ai_tokens_by_provider = self._r.hgetall(self.AI_TOKENS_BY_PROVIDER_KEY)
            ai_tokens_today = int(self._r.get(f"{self.AI_TOKENS_TODAY_PREFIX}:{date.today().isoformat()}") or 0)
            ai_budget_exceeded = int(self._r.get(self.AI_BUDGET_EXCEEDED_KEY) or 0)
            semgrep_verify = self._r.hgetall(self.SEMGREP_VERIFY_KEY)
            semgrep_errors = self._r.hgetall(self.SEMGREP_ERRORS_KEY)
            task_retries = int(self._r.get(self._TASK_RETRIES_KEY) or 0)
        except Exception:
            return "# Redis unavailable\n"

        lines: list[str] = []

        # --- webhooks ---
        lines += [
            "# HELP code_review_webhooks_total Webhook requests received by status",
            "# TYPE code_review_webhooks_total counter",
        ]
        for status in ("queued", "ignored", "already_queued", "error", "auth_failed"):
            lines.append(f'code_review_webhooks_total{{status="{status}"}} {webhooks.get(status, 0)}')

        # --- global reviews ---
        lines += [
            "# HELP code_review_reviews_total Completed reviews by outcome",
            "# TYPE code_review_reviews_total counter",
        ]
        for status in ("success", "failure", "skipped"):
            lines.append(f'code_review_reviews_total{{status="{status}"}} {reviews.get(status, 0)}')

        # --- global findings ---
        lines += [
            "# HELP code_review_findings_total Findings reported by severity",
            "# TYPE code_review_findings_total counter",
        ]
        for severity in ("critical", "bug", "performance", "suggestion"):
            lines.append(f'code_review_findings_total{{severity="{severity}"}} {sev.get(severity, 0)}')

        lines += [
            "# HELP code_review_findings_by_source_total Findings reported by analyzer source",
            "# TYPE code_review_findings_by_source_total counter",
        ]
        for source in ("semgrep", "ai"):
            lines.append(f'code_review_findings_by_source_total{{source="{source}"}} {src.get(source, 0)}')

        # --- duration ---
        lines += [
            "# HELP code_review_duration_milliseconds_sum Total review duration sum in ms",
            "# TYPE code_review_duration_milliseconds_sum counter",
            f"code_review_duration_milliseconds_sum {dur_sum}",
            "# HELP code_review_duration_milliseconds_count Total number of timed reviews",
            "# TYPE code_review_duration_milliseconds_count counter",
            f"code_review_duration_milliseconds_count {dur_count}",
        ]

        # --- by language ---
        lines += [
            "# HELP code_review_reviews_by_language_total Reviews grouped by language and outcome",
            "# TYPE code_review_reviews_by_language_total counter",
        ]
        for field, val in by_lang.items():
            lang, status = field.rsplit(":", 1)
            lines.append(f'code_review_reviews_by_language_total{{language="{lang}",status="{status}"}} {val}')

        lines += [
            "# HELP code_review_findings_by_language_total Findings grouped by language and severity",
            "# TYPE code_review_findings_by_language_total counter",
        ]
        for field, val in findings_by_lang.items():
            lang, severity = field.rsplit(":", 1)
            lines.append(f'code_review_findings_by_language_total{{language="{lang}",severity="{severity}"}} {val}')

        # --- by project ---
        lines += [
            "# HELP code_review_reviews_by_project_total Reviews grouped by project and outcome",
            "# TYPE code_review_reviews_by_project_total counter",
        ]
        for field, val in by_project.items():
            project, status = field.rsplit(":", 1)
            lines.append(f'code_review_reviews_by_project_total{{project="{project}",status="{status}"}} {val}')

        lines += [
            "# HELP code_review_findings_by_project_total Findings grouped by project and severity",
            "# TYPE code_review_findings_by_project_total counter",
        ]
        for field, val in findings_by_project.items():
            project, severity = field.rsplit(":", 1)
            lines.append(f'code_review_findings_by_project_total{{project="{project}",severity="{severity}"}} {val}')

        lines += [
            "# HELP code_review_duration_by_project_milliseconds_sum Review duration sum in ms, by project",
            "# TYPE code_review_duration_by_project_milliseconds_sum counter",
        ]
        for field, val in duration_by_project.items():
            if not field.endswith(":sum_ms"):
                continue
            project = field[: -len(":sum_ms")]
            lines.append(f'code_review_duration_by_project_milliseconds_sum{{project="{project}"}} {val}')

        lines += [
            "# HELP code_review_duration_by_project_milliseconds_count Number of timed reviews, by project",
            "# TYPE code_review_duration_by_project_milliseconds_count counter",
        ]
        for field, val in duration_by_project.items():
            if not field.endswith(":count"):
                continue
            project = field[: -len(":count")]
            lines.append(f'code_review_duration_by_project_milliseconds_count{{project="{project}"}} {val}')

        # --- extra findings (beyond 5 per PR) ---
        lines += [
            "# HELP code_review_extra_findings_total Cumulative findings beyond the first 5 per reviewed PR",
            "# TYPE code_review_extra_findings_total counter",
            f"code_review_extra_findings_total {extra_findings}",
        ]

        # --- by author ---
        lines += [
            "# HELP code_review_reviews_by_author_total Reviews grouped by PR author and outcome",
            "# TYPE code_review_reviews_by_author_total counter",
        ]
        for field, val in by_author.items():
            author, status = field.rsplit(":", 1)
            lines.append(f'code_review_reviews_by_author_total{{author="{author}",status="{status}"}} {val}')

        lines += [
            "# HELP code_review_findings_by_author_total Findings grouped by PR author and severity",
            "# TYPE code_review_findings_by_author_total counter",
        ]
        for field, val in findings_by_author.items():
            author, severity = field.rsplit(":", 1)
            lines.append(f'code_review_findings_by_author_total{{author="{author}",severity="{severity}"}} {val}')

        # --- AI cost ---
        lines += [
            "# HELP code_review_ai_tokens_total Cumulative AI tokens consumed, by provider and kind",
            "# TYPE code_review_ai_tokens_total counter",
        ]
        for field, val in ai_tokens_by_provider.items():
            provider, kind = field.rsplit(":", 1)
            lines.append(f'code_review_ai_tokens_total{{provider="{provider}",kind="{kind}"}} {val}')

        lines += [
            "# HELP code_review_ai_tokens_today AI tokens consumed today (input+output, resets daily)",
            "# TYPE code_review_ai_tokens_today gauge",
            f"code_review_ai_tokens_today {ai_tokens_today}",
            "# HELP code_review_ai_token_budget Configured daily AI token budget (0 = unlimited)",
            "# TYPE code_review_ai_token_budget gauge",
            f"code_review_ai_token_budget {self._ai_daily_token_budget}",
            "# HELP code_review_ai_budget_exceeded_total Times the daily AI token budget was hit",
            "# TYPE code_review_ai_budget_exceeded_total counter",
            f"code_review_ai_budget_exceeded_total {ai_budget_exceeded}",
        ]

        # --- semgrep AI verification ---
        lines += [
            "# HELP code_review_semgrep_verify_total Semgrep findings kept/dropped by AI verification",
            "# TYPE code_review_semgrep_verify_total counter",
        ]
        for result in ("kept", "dropped"):
            lines.append(f'code_review_semgrep_verify_total{{result="{result}"}} {semgrep_verify.get(result, 0)}')

        lines += [
            "# HELP code_review_semgrep_verify_fail_open_total Times semgrep AI verification failed open (kept findings unfiltered)",
            "# TYPE code_review_semgrep_verify_fail_open_total counter",
            f"code_review_semgrep_verify_fail_open_total {semgrep_verify.get('fail_open', 0)}",
        ]

        # --- semgrep errors ---
        lines += [
            "# HELP code_review_semgrep_errors_total Semgrep runner failures by reason",
            "# TYPE code_review_semgrep_errors_total counter",
        ]
        for reason in ("timeout", "bad_exit", "invalid_json"):
            lines.append(f'code_review_semgrep_errors_total{{reason="{reason}"}} {semgrep_errors.get(reason, 0)}')

        # --- celery retries ---
        lines += [
            "# HELP code_review_task_retries_total Celery task retries (transient connection/timeout errors)",
            "# TYPE code_review_task_retries_total counter",
            f"code_review_task_retries_total {task_retries}",
        ]

        return "\n".join(lines) + "\n"
