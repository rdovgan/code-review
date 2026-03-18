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
"""
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

    def __init__(self, redis_url: str) -> None:
        self._r = redis.from_url(redis_url, decode_responses=True)

    # --- write side (called from celery worker) ---

    def inc_webhook(self, status: str) -> None:
        """status: queued | ignored | already_queued | error"""
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

            # dimensional: by author
            pipe.hincrby(self._BY_AUTHOR_KEY, f"{author}:{status}", 1)
            pipe.hincrby(self._FINDINGS_BY_AUTHOR_KEY, f"{author}:critical", critical)
            pipe.hincrby(self._FINDINGS_BY_AUTHOR_KEY, f"{author}:bug", bugs)
            pipe.hincrby(self._FINDINGS_BY_AUTHOR_KEY, f"{author}:total", total_findings)

            pipe.execute()
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
        except Exception:
            return "# Redis unavailable\n"

        lines: list[str] = []

        # --- webhooks ---
        lines += [
            "# HELP code_review_webhooks_total Webhook requests received by status",
            "# TYPE code_review_webhooks_total counter",
        ]
        for status in ("queued", "ignored", "already_queued", "error"):
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

        return "\n".join(lines) + "\n"
