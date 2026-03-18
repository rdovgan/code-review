"""
Redis-backed metrics exposed in Prometheus text format via /metrics endpoint.

Counters survive restarts because they live in Redis.
Prometheus scrapes /metrics periodically and stores time series.
Grafana queries Prometheus for dashboards.
"""
import redis


class Metrics:
    _WEBHOOKS_KEY = "metrics:webhooks"
    _REVIEWS_KEY = "metrics:reviews"
    _FINDINGS_SEVERITY_KEY = "metrics:findings:severity"
    _FINDINGS_SOURCE_KEY = "metrics:findings:source"
    _DURATION_SUM_KEY = "metrics:duration:sum_ms"
    _DURATION_COUNT_KEY = "metrics:duration:count"

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
    ) -> None:
        """status: success | failure | skipped"""
        try:
            pipe = self._r.pipeline()
            pipe.hincrby(self._REVIEWS_KEY, status, 1)
            pipe.hincrby(self._FINDINGS_SEVERITY_KEY, "critical", critical)
            pipe.hincrby(self._FINDINGS_SEVERITY_KEY, "bug", bugs)
            pipe.hincrby(self._FINDINGS_SEVERITY_KEY, "performance", perf)
            pipe.hincrby(self._FINDINGS_SEVERITY_KEY, "suggestion", suggestions)
            pipe.hincrby(self._FINDINGS_SOURCE_KEY, "semgrep", semgrep_count)
            pipe.hincrby(self._FINDINGS_SOURCE_KEY, "ai", ai_count)
            pipe.incrby(self._DURATION_SUM_KEY, duration_ms)
            pipe.incr(self._DURATION_COUNT_KEY)
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
        except Exception:
            return "# Redis unavailable\n"

        lines: list[str] = []

        lines += [
            "# HELP code_review_webhooks_total Webhook requests received by status",
            "# TYPE code_review_webhooks_total counter",
        ]
        for status in ("queued", "ignored", "already_queued", "error"):
            lines.append(f'code_review_webhooks_total{{status="{status}"}} {webhooks.get(status, 0)}')

        lines += [
            "# HELP code_review_reviews_total Completed reviews by outcome",
            "# TYPE code_review_reviews_total counter",
        ]
        for status in ("success", "failure", "skipped"):
            lines.append(f'code_review_reviews_total{{status="{status}"}} {reviews.get(status, 0)}')

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

        lines += [
            "# HELP code_review_duration_milliseconds_sum Total review duration sum in ms",
            "# TYPE code_review_duration_milliseconds_sum counter",
            f"code_review_duration_milliseconds_sum {dur_sum}",
            "# HELP code_review_duration_milliseconds_count Total number of timed reviews",
            "# TYPE code_review_duration_milliseconds_count counter",
            f"code_review_duration_milliseconds_count {dur_count}",
        ]

        return "\n".join(lines) + "\n"
