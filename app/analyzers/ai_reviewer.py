import json
import logging
import time
from datetime import date
from pathlib import Path
from typing import Optional

import anthropic
import redis

from app.config.settings import Settings
from app.metrics import Metrics
from app.models import Finding, PRContext, ReviewConfig, Severity
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

    @property
    def complete(self) -> bool:
        return self.stop_reason is None


class AIReviewer:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._redis = redis.from_url(settings.REDIS_URL)

        provider = settings.AI_PROVIDER.lower()
        if provider == "glm":
            from openai import OpenAI
            self._provider = "glm"
            self._client = OpenAI(api_key=settings.ZAI_API_KEY, base_url=settings.ZAI_BASE_URL)
        else:
            self._provider = "claude"
            self._client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    def _budget_key(self) -> str:
        return f"{Metrics.AI_TOKENS_TODAY_PREFIX}:{date.today().isoformat()}"

    def _check_and_record_tokens(self, input_tokens: int, output_tokens: int) -> bool:
        """Returns False if daily budget is exceeded. Always records usage."""
        budget = self._settings.AI_DAILY_TOKEN_BUDGET
        key = self._budget_key()
        total = input_tokens + output_tokens
        new_count = self._redis.incrby(key, total)
        self._redis.expire(key, 86400 * 2)  # keep 2 days for visibility
        self._redis.hincrby(Metrics.AI_TOKENS_BY_PROVIDER_KEY, f"{self._provider}:input", input_tokens)
        self._redis.hincrby(Metrics.AI_TOKENS_BY_PROVIDER_KEY, f"{self._provider}:output", output_tokens)
        if budget > 0 and (new_count - total) >= budget:
            logger.warning("Daily AI token budget (%d) exceeded: %d used today", budget, new_count)
            self._redis.incr(Metrics.AI_BUDGET_EXCEEDED_KEY)
            return False
        logger.info("AI tokens used: input=%d output=%d daily_total=%d", input_tokens, output_tokens, new_count)
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
        if text.startswith("```"):
            lines = text.splitlines()
            # Remove opening fence (```json or ```)
            lines = lines[1:]
            # Remove closing fence
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)
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

    def _call_claude(self, prompt: str, chunk: str) -> tuple[str, int, int]:
        # Security-review diffs can trip a model's safety classifier; "default" fallbacks
        # re-run a declined request on a fallback model inside the same call.
        response = self._client.beta.messages.create(
            model=self._settings.AI_MODEL,
            max_tokens=self._settings.AI_MAX_TOKENS,
            system=prompt,
            messages=[{"role": "user", "content": chunk}],
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None) if response.stop_details else None
            raise RuntimeError(f"model declined the request (category={category})")
        if response.stop_reason == "max_tokens":
            logger.warning("AI response hit max_tokens=%d — JSON is likely truncated", self._settings.AI_MAX_TOKENS)
        # Thinking blocks come before the answer — read only the text blocks
        text = "".join(block.text for block in response.content if block.type == "text")
        return text, response.usage.input_tokens, response.usage.output_tokens

    def _call_glm(self, prompt: str, chunk: str) -> tuple[str, int, int]:
        response = self._client.chat.completions.create(
            model=self._settings.GLM_MODEL,
            max_tokens=self._settings.AI_MAX_TOKENS,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": chunk},
            ],
        )
        text = response.choices[0].message.content
        if not text:
            finish_reason = response.choices[0].finish_reason
            logger.warning(
                "GLM returned empty content (finish_reason=%s, model=%s)",
                finish_reason, self._settings.GLM_MODEL,
            )
            text = ""
        return text, response.usage.prompt_tokens, response.usage.completion_tokens

    # Circuit breaker: stop AI review after N consecutive parse failures
    _MAX_CONSECUTIVE_PARSE_FAILURES = 2
    # Cap on number of AI chunks to process (prevents runaway costs on huge PRs)
    _MAX_CHUNKS = 15
    # Stop processing chunks this many seconds before the deadline so the task
    # can still merge findings, post comments and finalize the build status.
    _DEADLINE_RESERVE_SECONDS = 60

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
        """context: the PR_CONTEXT block (intent + repo guides), sent with every request."""
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
        consecutive_parse_failures = 0
        failed_chunks = 0
        for i, chunk in enumerate(chunks):
            # Wall-clock guard: stop before the task's Celery time limit kills
            # us mid-review — partial findings still get posted and the build
            # status gets finalized instead of being stuck on "in progress".
            if deadline is not None and time.monotonic() > deadline - self._DEADLINE_RESERVE_SECONDS:
                logger.warning(
                    "AI review deadline reached (%d/%d chunks) — returning %d partial finding(s)",
                    i, len(chunks), len(result),
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
                request = self._build_request(chunk, file_contents or {}, context)
                text, input_tokens, output_tokens = self._call(prompt, request)
                self._check_and_record_tokens(input_tokens, output_tokens)
                logger.debug("AI raw response (chunk %d/%d): %s", i + 1, len(chunks), text)
                items = self._parse_response(text)
                if items is None:
                    consecutive_parse_failures += 1
                    failed_chunks += 1
                    continue
                consecutive_parse_failures = 0
                result.chunks_reviewed += 1
                chunk_findings = [f for f in (self._validate_finding(item) for item in items if isinstance(item, dict)) if f]
                if chunk_findings and config.ai_verify:
                    kept = self._verify_ai_findings(chunk_findings, request, deadline)
                    result.dropped_by_verification += len(chunk_findings) - len(kept)
                    chunk_findings = kept
                result.extend(chunk_findings)
            except Exception as exc:
                logger.error("AI review chunk failed: %s", exc)
                consecutive_parse_failures += 1
                failed_chunks += 1
        if result.stop_reason is None and failed_chunks:
            result.stop_reason = "errors"
        return result

    def _call(self, prompt: str, request: str) -> tuple[str, int, int]:
        if self._provider == "glm":
            return self._call_glm(prompt, request)
        return self._call_claude(prompt, request)

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

    def _verify_ai_findings(self, findings: list[Finding], request: str, deadline: Optional[float]) -> list[Finding]:
        """Second pass over the same material that tries to refute each finding.

        Fails open (keeps the findings) when there is no time or budget left, or the
        call or its response fails — verification must never hide a finding by accident.
        """
        if deadline is not None and time.monotonic() > deadline - self._DEADLINE_RESERVE_SECONDS:
            logger.warning("No time left to verify %d AI finding(s), keeping them unverified", len(findings))
            self._record_ai_verification("fail_open")
            return findings
        if self._settings.AI_DAILY_TOKEN_BUDGET > 0:
            used = int(self._redis.get(self._budget_key()) or 0)
            if used >= self._settings.AI_DAILY_TOKEN_BUDGET:
                logger.warning("Daily AI token budget reached, keeping %d AI finding(s) unverified", len(findings))
                self._record_ai_verification("fail_open")
                return findings

        payload = [
            {"index": i, "severity": f.severity.value, "file": f.file, "line": f.line,
             "message": f.message, "suggestion": f.suggestion}
            for i, f in enumerate(findings)
        ]
        verify_request = f"{request}\n\n---\nREVIEW_FINDINGS_JSON:\n{json.dumps(payload)}"
        try:
            text, input_tokens, output_tokens = self._call(self._load_verify_prompt("verify_ai.md"), verify_request)
            self._check_and_record_tokens(input_tokens, output_tokens)
            confirmed = self._confirmed_indexes(self._parse_response(text), len(findings))
        except Exception as exc:
            logger.error("AI finding verification failed, keeping findings unverified: %s", exc)
            confirmed = None
        if confirmed is None:
            logger.warning("AI finding verification gave no usable verdicts, keeping %d finding(s)", len(findings))
            self._record_ai_verification("fail_open")
            return findings

        kept = [f for i, f in enumerate(findings) if i in confirmed]
        self._record_ai_verification("kept", len(kept))
        self._record_ai_verification("dropped", len(findings) - len(kept))
        logger.info("AI finding verification kept %d/%d finding(s)", len(kept), len(findings))
        return kept

    def verify_semgrep_findings(self, findings: list[Finding], pr_context: PRContext) -> list[Finding]:
        """Re-check Semgrep findings with AI and drop ones it judges to be false positives.

        Fails open (returns findings unfiltered) on budget exhaustion, AI errors, or an
        unparseable response — a verification hiccup should never silently suppress a real
        static-analysis finding.
        """
        if not findings:
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
            text, input_tokens, output_tokens = self._call(prompt, chunk)
            self._check_and_record_tokens(input_tokens, output_tokens)
            items = self._parse_response(text)
        except Exception as exc:
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
