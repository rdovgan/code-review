import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

import anthropic
import redis
from celery.exceptions import SoftTimeLimitExceeded

from app.analyzers.ai_calls import RequestTimedOut, call_bounded, timeout_for
from app.config.settings import Settings
from app.metrics import Metrics
from app.models import Finding, PRContext, ReviewConfig, Severity
from app.notifiers.alerts import send_alert, today
from app.utils.diff_parser import split_diff_by_file

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"

# Shared rules + output schema; <language>_prompt.md adds language pitfalls on top
_BASE_PROMPT_FILE = "review_base.md"

_AI_FOCUS_HINTS = {
    "security": "security issues the change introduces",
    "bugs": "incorrect behavior and broken business logic",
    "performance": "real performance costs on realistic paths",
}


def _count(obj, name: str) -> int:
    value = getattr(obj, name, 0) if obj is not None else 0
    return value if isinstance(value, int) else 0


def claude_input_tokens(usage) -> tuple[int, int]:
    """(input incl. cache writes, cache reads) — Anthropic reports the three separately."""
    return (_count(usage, "input_tokens") + _count(usage, "cache_creation_input_tokens"),
            _count(usage, "cache_read_input_tokens"))


def openai_input_tokens(usage) -> tuple[int, int]:
    """(uncached input, cached input) — OpenAI-compatible APIs (GLM) count cached inside prompt_tokens."""
    prompt = _count(usage, "prompt_tokens")
    cached = _count(getattr(usage, "prompt_tokens_details", None), "cached_tokens")
    return prompt - cached, cached


def openai_reasoning_tokens(usage) -> int:
    """Reasoning tokens of a reply (GLM reports them inside completion_tokens)."""
    return _count(getattr(usage, "completion_tokens_details", None), "reasoning_tokens")


# glm-5.3-flash produced ~45 output tokens/s on Z.AI (PR #52): sizes the startup warnings
_GLM_TOKENS_PER_SECOND = 45
_GLM_MAX_DIFF_TOKENS_ADVICE = 20_000
_warned_sizes = False


@dataclass
class CallResult:
    """One AI request: the answer text plus what it cost and how it ended."""
    text: str
    input_tokens: int
    output_tokens: int
    cached: int = 0
    truncated: bool = False
    finish_reason: Optional[str] = None
    reasoning_tokens: int = 0
    elapsed: float = 0.0


class AIReviewResult(list):
    """Findings of one AI review plus how much of the diff was actually reviewed.

    A list subclass so callers that only need the findings can treat it as one.
    stop_reason is None when every chunk was reviewed, otherwise one of
    "budget", "deadline", "chunk_cap", "parse_failures", "errors".
    """

    def __init__(self, findings=(), chunks_total: int = 0, chunks_reviewed: int = 0,
                 stop_reason: Optional[str] = None) -> None:
        super().__init__(findings)
        self.chunks_total = chunks_total
        self.chunks_reviewed = chunks_reviewed
        self.stop_reason = stop_reason
        # Findings the verification pass judged false positives and dropped
        self.dropped_by_verification = 0
        # Findings kept without the verification pass because no time was left for it
        self.unverified = 0
        # Agentic review: code lookups, limits that stopped the agent early ("steps" /
        # "tokens" / "time"), and the parts that failed (their diff goes to the diff review)
        self.tool_calls = 0
        self.limits_hit: set[str] = set()
        self.unreviewed_diff = ""
        self.failed_parts = 0
        self.failed_part_numbers: list[int] = []
        self.failure_reason: Optional[str] = None

    def merge_diff_review(self, other) -> None:
        """Add the diff review of the parts the agent did not finish; agent metadata is kept."""
        self.extend(other)
        self.stop_reason = getattr(other, "stop_reason", None)
        self.dropped_by_verification += getattr(other, "dropped_by_verification", 0)
        self.unverified += getattr(other, "unverified", 0)

    @property
    def complete(self) -> bool:
        return self.stop_reason is None


class AIReviewer:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._redis = redis.from_url(settings.REDIS_URL)

        # Every request is bounded: the SDK defaults (600 s, 2 silent retries) let one
        # request outlive the task's time limit. Retries are done by call_bounded instead.
        provider = settings.AI_PROVIDER.lower()
        if provider == "glm":
            import openai
            self._provider = "glm"
            self._client = openai.OpenAI(api_key=settings.ZAI_API_KEY, base_url=settings.ZAI_BASE_URL,
                                         timeout=openai.Timeout(settings.AI_REQUEST_TIMEOUT_SECONDS, connect=10),
                                         max_retries=0)
            self._warn_about_glm_sizes()
        else:
            self._provider = "claude"
            self._client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY,
                                               timeout=anthropic.Timeout(settings.AI_REQUEST_TIMEOUT_SECONDS, connect=10),
                                               max_retries=0)

    def _warn_about_glm_sizes(self) -> None:
        """Once per process: settings that make a GLM request slower than its timeout."""
        global _warned_sizes
        if _warned_sizes:
            return
        _warned_sizes = True
        s = self._settings
        if s.AI_MAX_TOKENS / _GLM_TOKENS_PER_SECOND > s.AI_REQUEST_TIMEOUT_SECONDS:
            logger.warning("AI_MAX_TOKENS=%d takes ~%ds to generate on GLM, longer than AI_REQUEST_TIMEOUT_SECONDS=%d; "
                           "long answers will time out", s.AI_MAX_TOKENS, s.AI_MAX_TOKENS // _GLM_TOKENS_PER_SECOND,
                           s.AI_REQUEST_TIMEOUT_SECONDS)
        if s.AI_MAX_DIFF_TOKENS > _GLM_MAX_DIFF_TOKENS_ADVICE:
            logger.warning("AI_MAX_DIFF_TOKENS=%d is large for a reasoning model on GLM; about 15000 keeps one "
                           "diff request within minutes", s.AI_MAX_DIFF_TOKENS)

    def _record_timeout(self, kind: str) -> None:
        try:
            self._redis.hincrby(Metrics.AI_TIMEOUTS_KEY, kind, 1)
        except Exception:
            pass

    def _budget_key(self) -> str:
        return f"{Metrics.AI_TOKENS_TODAY_PREFIX}:{date.today().isoformat()}"

    def _weighted(self, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> int:
        """Tokens as the budget counts them: cache reads at AI_CACHED_TOKEN_WEIGHT."""
        return input_tokens + output_tokens + round(cached_tokens * self._settings.AI_CACHED_TOKEN_WEIGHT)

    def _alert_budget(self, budget: int, before: int, after: int) -> None:
        """Alert when today's usage crosses the warning share of the budget, and the budget itself."""
        warn_at = budget * max(0, min(100, self._settings.AI_BUDGET_ALERT_PERCENT)) // 100
        if before < budget <= after:
            send_alert(self._settings, f"budget_exceeded:{today()}", "daily AI token budget used up",
                       f"{after:,} of {budget:,} tokens used today. Until the counter resets at midnight "
                       "(server time), reviews run Semgrep only and get the `stopped` status. Raise `AI_DAILY_TOKEN_BUDGET` in .env "
                       "or move repositories from `review_mode: agentic` back to `diff`.", self._redis)
        elif 0 < warn_at and before < warn_at <= after:
            send_alert(self._settings, f"budget_warning:{today()}",
                       f"{after * 100 // budget}% of the daily AI token budget used",
                       f"{after:,} of {budget:,} tokens used today. When it runs out, reviews run Semgrep only "
                       "until midnight (server time).", self._redis)

    def _check_and_record_tokens(self, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> bool:
        """Returns False if daily budget is exceeded. Always records usage.

        input_tokens excludes cache reads; cached_tokens are recorded separately (metric kind
        "cached") and count toward the budget at AI_CACHED_TOKEN_WEIGHT.
        """
        budget = self._settings.AI_DAILY_TOKEN_BUDGET
        key = self._budget_key()
        total = self._weighted(input_tokens, output_tokens, cached_tokens)
        new_count = self._redis.incrby(key, total)
        self._redis.expire(key, 86400 * 2)  # keep 2 days for visibility
        self._redis.hincrby(Metrics.AI_TOKENS_BY_PROVIDER_KEY, f"{self._provider}:input", input_tokens)
        self._redis.hincrby(Metrics.AI_TOKENS_BY_PROVIDER_KEY, f"{self._provider}:output", output_tokens)
        if cached_tokens:
            self._redis.hincrby(Metrics.AI_TOKENS_BY_PROVIDER_KEY, f"{self._provider}:cached", cached_tokens)
        if budget > 0:
            self._alert_budget(budget, new_count - total, new_count)
        if budget > 0 and (new_count - total) >= budget:
            logger.warning("Daily AI token budget (%d) exceeded: %d used today", budget, new_count)
            self._redis.incr(Metrics.AI_BUDGET_EXCEEDED_KEY)
            return False
        logger.info("AI tokens used: input=%d cached=%d output=%d daily_total=%d",
                    input_tokens, cached_tokens, output_tokens, new_count)
        return True

    def _record_verify_fail_open(self) -> None:
        try:
            self._redis.hincrby(Metrics.SEMGREP_VERIFY_KEY, "fail_open", 1)
        except Exception:
            pass

    def _record_ai_verification(self, field: str, amount: int = 1) -> None:
        try:
            self._redis.hincrby(Metrics.AI_VERIFY_KEY, field, amount)
        except Exception:
            pass

    def _load_prompt(self, language: str, focus: Optional[list[str]] = None) -> str:
        """Read on every review so prompt tuning needs no restart."""
        parts = [(_PROMPTS_DIR / _BASE_PROMPT_FILE).read_text()]
        language_file = _PROMPTS_DIR / f"{language}_prompt.md"
        if language_file.exists():
            parts.append(language_file.read_text())
        if focus:
            hints = [_AI_FOCUS_HINTS.get(str(f).lower(), str(f)) for f in focus]
            parts.append("## Team focus\n\nThis team asked for the review to concentrate on: " + "; ".join(hints) + ".")
        return "\n\n".join(parts)

    def _load_verify_prompt(self, name: str = "verify_semgrep.md") -> str:
        return (_PROMPTS_DIR / name).read_text()

    def _split_by_file(self, diff: str) -> list[str]:
        chunks = []
        current = []
        for line in diff.splitlines(keepends=True):
            if line.startswith("diff --git a/") and current:
                chunks.append("".join(current))
                current = []
            current.append(line)
        if current:
            chunks.append("".join(current))
        return chunks if chunks else [diff]

    def _halve(self, chunk: str) -> Optional[list[str]]:
        """Split a multi-file chunk into two by size; None for a single file."""
        files = self._split_by_file(chunk)
        if len(files) < 2:
            return None
        half, size = len(chunk) / 2, 0
        for k, f in enumerate(files[:-1], 1):
            size += len(f)
            if size >= half:
                break
        return ["".join(files[:k]), "".join(files[k:])]

    def _split_if_needed(self, diff: str) -> list[str]:
        """Whole files packed into requests of up to AI_MAX_DIFF_TOKENS, in diff order.

        Diff order is path order, so files of one package/feature tend to land in the
        same request — the model sees a service and its DTO/repository together instead
        of judging each file blind. A file bigger than the limit gets a request of its own.
        """
        max_chars = self._settings.AI_MAX_DIFF_TOKENS * 4
        if len(diff) <= max_chars:
            return [diff]
        chunks: list[str] = []
        current = ""
        for file_diff in self._split_by_file(diff):
            if current and len(current) + len(file_diff) > max_chars:
                chunks.append(current)
                current = ""
            current += file_diff
        if current:
            chunks.append(current)
        return chunks

    def _parse_response(self, text: str) -> Optional[list]:
        """Parsed JSON array, or None if the response isn't one ([] is a valid "no issues")."""
        text = text.strip()
        if not text:
            logger.warning("AI returned empty response")
            return None
        # A fenced block anywhere: "Summary of the review ...\n```json\n[...]\n```"
        fenced = re.search(r"```(?:json)?\s*\n(.*?)\n\s*```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1).strip()
        elif text.startswith("```"):  # opening fence without a closing one (cut off)
            text = "\n".join(text.splitlines()[1:])
        # Try to extract JSON array from mixed text (e.g. "Here is the result:\n[...")
        if text.startswith("[") is False:
            start = text.find("[")
            if start != -1:
                end = text.rfind("]")
                if end > start:
                    text = text[start : end + 1]
                    logger.info("Extracted JSON array from mixed AI response")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Failed to parse AI response as JSON (first 500 chars): %s", text[:500])
            return None
        if isinstance(parsed, dict):  # {"findings": [...]}
            lists = [v for v in parsed.values() if isinstance(v, list)]
            if len(lists) == 1:
                return lists[0]
        if not isinstance(parsed, list):
            logger.warning("AI response is JSON but not an array: %s", text[:200])
            return None
        return parsed

    def _validate_finding(self, item: dict) -> Optional[Finding]:
        required = {"severity", "file", "line", "message", "suggestion"}
        if not required.issubset(item.keys()):
            return None
        try:
            severity = Severity(item["severity"])
            line = int(item["line"])
        except (ValueError, TypeError):
            return None
        return Finding(
            severity=severity,
            file=str(item["file"]),
            line=line,
            message=str(item["message"]),
            suggestion=str(item["suggestion"]),
            source="ai",
        )

    def _call_claude(self, prompt: str, chunk: str, timeout: float) -> CallResult:
        # Security-review diffs can trip a model's safety classifier; "default" fallbacks
        # re-run a declined request on a fallback model inside the same call.
        response = self._client.beta.messages.create(
            model=self._settings.AI_MODEL,
            max_tokens=self._settings.AI_MAX_TOKENS,
            system=prompt,
            messages=[{"role": "user", "content": chunk}],
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            timeout=timeout,
        )
        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None) if response.stop_details else None
            raise RuntimeError(f"model declined the request (category={category})")
        truncated = response.stop_reason == "max_tokens"
        if truncated:
            logger.warning("AI response hit max_tokens=%d — JSON is likely truncated", self._settings.AI_MAX_TOKENS)
        # Thinking blocks come before the answer — read only the text blocks
        text = "".join(block.text for block in response.content if block.type == "text")
        input_tokens, cached = claude_input_tokens(response.usage)
        return CallResult(text, input_tokens, _count(response.usage, "output_tokens"), cached, truncated,
                          response.stop_reason if isinstance(response.stop_reason, str) else None)

    def _call_glm(self, prompt: str, chunk: str, timeout: float) -> CallResult:
        response = self._client.chat.completions.create(
            model=self._settings.GLM_MODEL,
            max_tokens=self._settings.AI_MAX_TOKENS,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": chunk},
            ],
            timeout=timeout,
        )
        choice = response.choices[0]
        text = choice.message.content
        finish_reason = getattr(choice, "finish_reason", None)
        if not text:
            logger.warning(
                "GLM returned empty content (finish_reason=%s, model=%s)",
                finish_reason, self._settings.GLM_MODEL,
            )
            text = ""
        input_tokens, cached = openai_input_tokens(response.usage)
        # Reasoning models can spend the whole budget thinking and return no answer at all
        return CallResult(text, input_tokens, _count(response.usage, "completion_tokens"), cached,
                          finish_reason == "length", finish_reason, openai_reasoning_tokens(response.usage))

    # Circuit breaker: stop AI review after N consecutive parse failures
    _MAX_CONSECUTIVE_PARSE_FAILURES = 2
    # Cap on number of AI chunks to process (prevents runaway costs on huge PRs)
    _MAX_CHUNKS = 15
    # Requests end this many seconds before the deadline so the task can still merge
    # findings, post comments and finalize the build status.
    _DEADLINE_RESERVE_SECONDS = 90
    # A request is not sent with less time than this left — it would only time out
    _MIN_REQUEST_SECONDS = 60

    def _build_request(self, chunk: str, file_contents: dict[str, Optional[str]], context: str) -> str:
        request = self._with_file_context(chunk, file_contents)
        return f"{context}\n\n---\n{request}" if context else request

    def _with_file_context(self, chunk: str, file_contents: dict[str, Optional[str]]) -> str:
        """Prepend the full new version of the chunk's files (line-numbered) to the diff.

        The diff alone has 3 lines of context, which makes "missing null check"-style
        false positives likely; the numbered file also gives the model exact line numbers.
        """
        budget = self._settings.AI_FILE_CONTEXT_MAX_CHARS
        if budget <= 0 or not file_contents:
            return chunk
        sections = []
        for fd in split_diff_by_file(chunk):
            content = file_contents.get(fd.path)
            if not content:
                continue
            numbered = "\n".join(f"{n:>5}| {line}" for n, line in enumerate(content.splitlines(), 1))
            if len(numbered) > budget:
                logger.info("File context for %s skipped: %d chars over remaining budget %d", fd.path, len(numbered), budget)
                continue
            budget -= len(numbered)
            sections.append(f"### {fd.path}\n{numbered}")
        if not sections:
            return chunk
        return (
            "FULL_FILE_CONTEXT — the new version of the changed files, line-numbered. Use it to understand "
            "the code around the change and for exact line numbers; report issues only on lines changed in the DIFF.\n\n"
            + "\n\n".join(sections)
            + "\n\n---\nDIFF:\n"
            + chunk
        )

    def review(self, pr_context: PRContext, config: ReviewConfig, deadline: Optional[float] = None,
               file_contents: Optional[dict[str, Optional[str]]] = None, context: str = "") -> AIReviewResult:
        """context: the PR_CONTEXT block (intent + repo guides), sent with every request.

        Coverage first: every chunk is reviewed, then the findings are verified in the time
        left (chunks with CRITICAL first, then BUG). Findings that could not be verified in
        time are kept (fail open) and counted in result.unverified.
        """
        prompt = self._load_prompt(pr_context.language, config.ai_focus)
        chunks = self._split_if_needed(pr_context.diff)
        result = AIReviewResult(chunks_total=len(chunks))
        if len(chunks) > self._MAX_CHUNKS:
            logger.warning(
                "Diff produced %d chunks, capping at %d to avoid runaway AI calls",
                len(chunks), self._MAX_CHUNKS,
            )
            chunks = chunks[:self._MAX_CHUNKS]
            result.stop_reason = "chunk_cap"
        pending: list[tuple[int, str, list[Finding], float]] = []  # (chunk index, request, findings, seconds)
        soft_limit = False
        try:
            self._review_chunks(prompt, chunks, result, pending, deadline, file_contents or {}, context)
        except SoftTimeLimitExceeded:
            # The soft limit fired inside a request: keep what is done, the task has the
            # soft-to-hard gap left to post it
            logger.warning("Soft time limit reached during the AI review — returning %d chunk(s) of findings",
                           len(pending))
            result.stop_reason = "deadline"
            soft_limit = True

        verified: dict[int, list[Finding]] = {}
        if config.ai_verify and pending:
            verified = self._verify_pending(pending, result, deadline, skip=soft_limit)
        for index, _, findings, _ in pending:
            result.extend(verified.get(index, findings))
        return result

    def _review_chunks(self, prompt: str, chunks: list[str], result: AIReviewResult, pending: list,
                       deadline: Optional[float], file_contents: dict, context: str) -> None:
        cap = self._settings.AI_REQUEST_TIMEOUT_SECONDS
        consecutive_parse_failures = 0
        failed_chunks = 0
        i = -1
        while i + 1 < len(chunks):  # a while loop: a cut-off or too slow answer splits its chunk in place
            i += 1
            chunk = chunks[i]
            # Wall-clock guard: a chunk is sent only if its request can finish before the
            # deadline — partial findings still get posted and the build status gets
            # finalized instead of being stuck on "in progress".
            if timeout_for(deadline, self._DEADLINE_RESERVE_SECONDS, cap, self._MIN_REQUEST_SECONDS) is None:
                logger.warning(
                    "AI review deadline reached (%d/%d chunks) — returning %d chunk(s) of findings",
                    i, len(chunks), len(pending),
                )
                result.stop_reason = "deadline"
                break
            # Pre-check budget before sending
            if self._settings.AI_DAILY_TOKEN_BUDGET > 0:
                used = int(self._redis.get(self._budget_key()) or 0)
                if used >= self._settings.AI_DAILY_TOKEN_BUDGET:
                    logger.warning("Daily AI token budget reached (%d), skipping AI review", used)
                    result.stop_reason = "budget"
                    break
            # Circuit breaker: too many consecutive bad responses → stop
            if consecutive_parse_failures >= self._MAX_CONSECUTIVE_PARSE_FAILURES:
                logger.error(
                    "AI parse failure circuit breaker tripped (%d consecutive), aborting AI review",
                    consecutive_parse_failures,
                )
                result.stop_reason = "parse_failures"
                break
            try:
                request = self._build_request(chunk, file_contents, context)
                res = self._call(prompt, request, deadline=deadline, reserve=self._DEADLINE_RESERVE_SECONDS,
                                 cap=cap, floor=self._MIN_REQUEST_SECONDS, kind="chunk")
                self._check_and_record_tokens(res.input_tokens, res.output_tokens, res.cached)
                logger.debug("AI raw response (chunk %d/%d): %s", i + 1, len(chunks), res.text)
                items = self._parse_response(res.text)
                if items is None and res.truncated:
                    halves = self._halve(chunk) if len(chunks) < self._MAX_CHUNKS * 2 else None
                    if halves:
                        logger.warning("AI answer cut off at max_tokens=%d — splitting this part (%d files) "
                                       "in two and retrying", self._settings.AI_MAX_TOKENS,
                                       len(self._split_by_file(chunk)))
                        chunks[i + 1:i + 1] = halves
                        result.chunks_total += 1
                        continue
                    logger.warning("AI answer cut off at max_tokens=%d on a single file — raise AI_MAX_TOKENS",
                                   self._settings.AI_MAX_TOKENS)
                if items is None:
                    consecutive_parse_failures += 1
                    failed_chunks += 1
                    continue
                consecutive_parse_failures = 0
                result.chunks_reviewed += 1
                chunk_findings = [f for f in (self._validate_finding(item) for item in items if isinstance(item, dict)) if f]
                if chunk_findings:
                    pending.append((i, request, chunk_findings, res.elapsed))
            except RequestTimedOut as exc:
                if exc.by_deadline:
                    logger.warning("AI review deadline reached in chunk %d/%d — returning %d chunk(s) of findings",
                                   i + 1, len(chunks), len(pending))
                    result.stop_reason = "deadline"
                    break
                # The full per-request cap ran out: a smaller request answers faster. Not a
                # parse failure — the model did not misbehave, the request was too big.
                halves = self._halve(chunk) if len(chunks) < self._MAX_CHUNKS * 2 else None
                if halves:
                    logger.warning("AI request timed out after %ds — splitting this part (%d files) in two",
                                   cap, len(self._split_by_file(chunk)))
                    chunks[i + 1:i + 1] = halves
                    result.chunks_total += 1
                    continue
                logger.warning("AI request timed out after %ds on a single file — single file too slow", cap)
                failed_chunks += 1
            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:
                logger.error("AI review chunk failed: %s", exc)
                consecutive_parse_failures += 1
                failed_chunks += 1
        if result.stop_reason is None and failed_chunks:
            result.stop_reason = "errors"

    def _verify_pending(self, pending: list, result: AIReviewResult, deadline: Optional[float],
                        skip: bool = False) -> dict[int, list[Finding]]:
        """Verify the chunks' findings, CRITICAL chunks first, then BUG; the rest stays unverified."""
        def priority(item):
            index, _, findings, _ = item
            severities = {f.severity for f in findings}
            return (Severity.CRITICAL not in severities, Severity.BUG not in severities, index)

        verified: dict[int, list[Finding]] = {}
        stopped = skip
        for index, request, findings, elapsed in sorted(pending, key=priority):
            if stopped:
                self._record_ai_verification("fail_open")
                result.unverified += len(findings)
                continue
            try:
                kept, no_time = self._verify_ai_findings(findings, request, deadline, est=elapsed)
            except SoftTimeLimitExceeded:
                logger.warning("Soft time limit reached during verification — keeping the rest unverified")
                self._record_ai_verification("fail_open")
                result.unverified += len(findings)
                stopped = True
                continue
            if no_time:
                result.unverified += len(findings)
            result.dropped_by_verification += len(findings) - len(kept)
            verified[index] = kept
        return verified

    def _call(self, prompt: str, request: str, *, deadline: Optional[float] = None, reserve: float = 0,
              cap: Optional[float] = None, floor: float = 0, kind: str = "chunk") -> CallResult:
        """One bounded request; raises RequestTimedOut when it cannot finish in time."""
        cap = cap or self._settings.AI_REQUEST_TIMEOUT_SECONDS
        send = self._call_glm if self._provider == "glm" else self._call_claude
        used = {"timeout": 0.0}

        def attempt(timeout: float) -> CallResult:
            used["timeout"] = timeout
            return send(prompt, request, timeout)

        started = time.monotonic()
        res = call_bounded(attempt, deadline=deadline, reserve=reserve, cap=cap, floor=floor, kind=kind,
                           on_timeout=self._record_timeout)
        res.elapsed = time.monotonic() - started
        logger.info("AI call: kind=%s chars=%d timeout=%.0f elapsed=%.1f in=%d cached=%d out=%d reasoning=%d finish=%s",
                    kind, len(prompt) + len(request), used["timeout"], res.elapsed, res.input_tokens, res.cached,
                    res.output_tokens, res.reasoning_tokens, res.finish_reason)
        return res

    @staticmethod
    def _confirmed_indexes(items: Optional[list], count: int) -> Optional[set[int]]:
        """Indexes with a CONFIRMED verdict, or None when the response holds no usable verdicts."""
        if not items:
            return None
        confirmed: set[int] = set()
        judged = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            verdict = str(item.get("verdict", "")).upper()
            if not isinstance(idx, int) or not 0 <= idx < count or verdict not in ("CONFIRMED", "FALSE_POSITIVE"):
                continue
            judged += 1
            if verdict == "CONFIRMED":
                confirmed.add(idx)
        return confirmed if judged else None

    def _verify_ai_findings(self, findings: list[Finding], request: str, deadline: Optional[float],
                            est: float = 0.0) -> tuple[list[Finding], bool]:
        """Second pass over the same material that tries to refute each finding.

        Returns (kept findings, skipped for lack of time). Fails open (keeps the findings)
        when there is no time or budget left, or the call or its response fails —
        verification must never hide a finding by accident. est: how long the review of
        this chunk took; the verification is sent only if about that much time is left.
        """
        cap = self._settings.AI_REQUEST_TIMEOUT_SECONDS
        floor = max(self._MIN_REQUEST_SECONDS, est)
        if timeout_for(deadline, self._DEADLINE_RESERVE_SECONDS, cap, floor) is None:
            logger.warning("No time left to verify %d AI finding(s), keeping them unverified", len(findings))
            self._record_ai_verification("fail_open")
            return findings, True
        if self._settings.AI_DAILY_TOKEN_BUDGET > 0:
            used = int(self._redis.get(self._budget_key()) or 0)
            if used >= self._settings.AI_DAILY_TOKEN_BUDGET:
                logger.warning("Daily AI token budget reached, keeping %d AI finding(s) unverified", len(findings))
                self._record_ai_verification("fail_open")
                return findings, False

        payload = [
            {"index": i, "severity": f.severity.value, "file": f.file, "line": f.line,
             "message": f.message, "suggestion": f.suggestion}
            for i, f in enumerate(findings)
        ]
        verify_request = f"{request}\n\n---\nREVIEW_FINDINGS_JSON:\n{json.dumps(payload)}"
        try:
            res = self._call(self._load_verify_prompt("verify_ai.md"), verify_request, deadline=deadline,
                             reserve=self._DEADLINE_RESERVE_SECONDS, cap=cap, floor=floor, kind="verify")
            self._check_and_record_tokens(res.input_tokens, res.output_tokens, res.cached)
            confirmed = self._confirmed_indexes(self._parse_response(res.text), len(findings))
        except RequestTimedOut:
            logger.warning("AI finding verification timed out, keeping %d finding(s) unverified", len(findings))
            self._record_ai_verification("fail_open")
            return findings, True
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:
            logger.error("AI finding verification failed, keeping findings unverified: %s", exc)
            confirmed = None
        if confirmed is None:
            logger.warning("AI finding verification gave no usable verdicts, keeping %d finding(s)", len(findings))
            self._record_ai_verification("fail_open")
            return findings, False

        kept = [f for i, f in enumerate(findings) if i in confirmed]
        self._record_ai_verification("kept", len(kept))
        self._record_ai_verification("dropped", len(findings) - len(kept))
        logger.info("AI finding verification kept %d/%d finding(s)", len(kept), len(findings))
        return kept, False

    # Semgrep verification: at most this long, and only with this much time left
    _SEMGREP_VERIFY_TIMEOUT_SECONDS = 120
    _SEMGREP_VERIFY_MIN_SECONDS = 30

    def verify_semgrep_findings(self, findings: list[Finding], pr_context: PRContext,
                                deadline: Optional[float] = None) -> list[Finding]:
        """Re-check Semgrep findings with AI and drop ones it judges to be false positives.

        Fails open (returns findings unfiltered) on budget exhaustion, AI errors, a
        timeout, no time left, or an unparseable response — a verification hiccup should
        never silently suppress a real static-analysis finding.
        """
        if not findings:
            return findings

        if timeout_for(deadline, 0, self._SEMGREP_VERIFY_TIMEOUT_SECONDS, self._SEMGREP_VERIFY_MIN_SECONDS) is None:
            logger.warning("No time left for semgrep verification (failing open)")
            self._record_verify_fail_open()
            return findings

        if self._settings.AI_DAILY_TOKEN_BUDGET > 0:
            used = int(self._redis.get(self._budget_key()) or 0)
            if used >= self._settings.AI_DAILY_TOKEN_BUDGET:
                logger.warning("Daily AI token budget reached, skipping semgrep verification (failing open)")
                self._record_verify_fail_open()
                return findings

        files = {f.file for f in findings}
        relevant_diff = "".join(
            chunk for chunk in self._split_by_file(pr_context.diff)
            if any(f in chunk for f in files)
        ) or pr_context.diff

        payload = [
            {"index": i, "rule_id": f.rule_id or "", "file": f.file, "line": f.line, "message": f.message}
            for i, f in enumerate(findings)
        ]
        chunk = f"{relevant_diff}\n\n---\nSTATIC_ANALYSIS_FINDINGS_JSON:\n{json.dumps(payload)}"
        prompt = self._load_verify_prompt()

        try:
            res = self._call(prompt, chunk, deadline=deadline, reserve=0, cap=self._SEMGREP_VERIFY_TIMEOUT_SECONDS,
                             floor=self._SEMGREP_VERIFY_MIN_SECONDS, kind="semgrep_verify")
            self._check_and_record_tokens(res.input_tokens, res.output_tokens, res.cached)
            items = self._parse_response(res.text)
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:  # RequestTimedOut included
            logger.error("Semgrep AI verification call failed, keeping findings unverified: %s", exc)
            self._record_verify_fail_open()
            return findings

        if not items:
            logger.warning("Semgrep AI verification returned no parseable verdicts, keeping findings unverified")
            self._record_verify_fail_open()
            return findings

        confirmed_indexes: set[int] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            verdict = str(item.get("verdict", "")).upper()
            if isinstance(idx, int) and verdict == "CONFIRMED":
                confirmed_indexes.add(idx)

        return [f for i, f in enumerate(findings) if i in confirmed_indexes]
