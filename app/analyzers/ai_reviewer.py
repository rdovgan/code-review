import json
import logging
from datetime import date
from pathlib import Path
from typing import Optional

import anthropic
import redis

from app.config.settings import Settings
from app.models import Finding, PRContext, ReviewConfig, Severity

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"

_GENERIC_PROMPT = """You are a senior software engineer performing a security-focused code review.

Analyze the following git diff and classify each issue as:
- CRITICAL: Security vulnerabilities (injection, auth bypass, RCE, data leaks)
- BUG: Logic errors, null dereferences, resource leaks, race conditions
- PERFORMANCE: N+1 queries, blocking I/O, unnecessary allocations
- SUGGEST: Naming, readability, best practices

Rules:
- Only report issues VISIBLE in the diff.
- Return ONLY a valid JSON array. No markdown fences, no explanation.
- If no issues found, return: []

JSON schema: [{"severity": "...", "file": "...", "line": 42, "message": "...", "suggestion": "..."}]
"""


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
        return f"ai_tokens:{date.today().isoformat()}"

    def _check_and_record_tokens(self, input_tokens: int, output_tokens: int) -> bool:
        """Returns False if daily budget is exceeded. Always records usage."""
        budget = self._settings.AI_DAILY_TOKEN_BUDGET
        key = self._budget_key()
        total = input_tokens + output_tokens
        new_count = self._redis.incrby(key, total)
        self._redis.expire(key, 86400 * 2)  # keep 2 days for visibility
        if budget > 0 and (new_count - total) >= budget:
            logger.warning("Daily AI token budget (%d) exceeded: %d used today", budget, new_count)
            return False
        logger.info("AI tokens used: input=%d output=%d daily_total=%d", input_tokens, output_tokens, new_count)
        return True

    def _load_prompt(self, language: str) -> str:
        prompt_file = _PROMPTS_DIR / f"{language}_prompt.md"
        if prompt_file.exists():
            return prompt_file.read_text()
        return _GENERIC_PROMPT

    def _split_if_needed(self, diff: str) -> list[str]:
        approx_tokens = len(diff) // 4
        if approx_tokens <= self._settings.AI_MAX_DIFF_TOKENS:
            return [diff]
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

    def _parse_response(self, text: str) -> list[dict]:
        text = text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            # Remove opening fence (```json or ```)
            lines = lines[1:]
            # Remove closing fence
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Failed to parse AI response as JSON: %s", text[:200])
            return []

    def _validate_finding(self, item: dict) -> Optional[Finding]:
        required = {"severity", "file", "line", "message", "suggestion"}
        if not required.issubset(item.keys()):
            return None
        try:
            severity = Severity(item["severity"])
        except ValueError:
            return None
        return Finding(
            severity=severity,
            file=str(item["file"]),
            line=int(item["line"]),
            message=str(item["message"]),
            suggestion=str(item["suggestion"]),
            source="ai",
        )

    def _call_claude(self, prompt: str, chunk: str) -> tuple[str, int, int]:
        response = self._client.messages.create(
            model=self._settings.AI_MODEL,
            max_tokens=self._settings.AI_MAX_TOKENS,
            system=prompt,
            messages=[{"role": "user", "content": chunk}],
        )
        text = response.content[0].text
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
        return text, response.usage.prompt_tokens, response.usage.completion_tokens

    def review(self, pr_context: PRContext, config: ReviewConfig) -> list[Finding]:
        prompt = self._load_prompt(pr_context.language)
        chunks = self._split_if_needed(pr_context.diff)
        findings: list[Finding] = []
        for chunk in chunks:
            # Pre-check budget before sending
            if self._settings.AI_DAILY_TOKEN_BUDGET > 0:
                used = int(self._redis.get(self._budget_key()) or 0)
                if used >= self._settings.AI_DAILY_TOKEN_BUDGET:
                    logger.warning("Daily AI token budget reached (%d), skipping AI review", used)
                    break
            try:
                if self._provider == "glm":
                    text, input_tokens, output_tokens = self._call_glm(prompt, chunk)
                else:
                    text, input_tokens, output_tokens = self._call_claude(prompt, chunk)
                self._check_and_record_tokens(input_tokens, output_tokens)
                items = self._parse_response(text)
                for item in items:
                    f = self._validate_finding(item)
                    if f:
                        findings.append(f)
            except Exception as exc:
                logger.error("AI review chunk failed: %s", exc)
        return findings
