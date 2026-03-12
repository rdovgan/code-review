import json
import logging
from pathlib import Path
from typing import Optional

import anthropic

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
        self._client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

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

    def review(self, pr_context: PRContext, config: ReviewConfig) -> list[Finding]:
        prompt = self._load_prompt(pr_context.language)
        chunks = self._split_if_needed(pr_context.diff)
        findings: list[Finding] = []
        for chunk in chunks:
            try:
                response = self._client.messages.create(
                    model=self._settings.AI_MODEL,
                    max_tokens=self._settings.AI_MAX_TOKENS,
                    system=prompt,
                    messages=[{"role": "user", "content": chunk}],
                )
                text = response.content[0].text
                items = self._parse_response(text)
                for item in items:
                    f = self._validate_finding(item)
                    if f:
                        findings.append(f)
            except Exception as exc:
                logger.error("AI review chunk failed: %s", exc)
        return findings
