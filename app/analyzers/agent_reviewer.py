"""Agentic review: the model reads the repository with tools before judging the change.

Same output as AIReviewer (a list of Findings), same provider, budget and prompts. The
difference is the loop: instead of one request with the diff, the model may call
read_file / grep / find_files / list_dir (app/context/code_tools.py) to check callers,
definitions and guards, and only then answers with the JSON findings.

Any failure raises AgentReviewFailed; the worker then falls back to the diff review.
"""
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from app.analyzers.ai_reviewer import (_PROMPTS_DIR, AIReviewer, AIReviewResult, claude_input_tokens,
                                       openai_input_tokens)
from app.context.code_tools import TOOL_SCHEMAS, CodeTools
from app.models import PRContext, ReviewConfig
from app.utils.diff_parser import split_diff_by_file

logger = logging.getLogger(__name__)

_AGENT_PROMPT_FILE = "agent_review.md"
# More chunks than this and the review is too big for the agent — use the diff review
_MAX_AGENT_CHUNKS = 3
# Seconds kept for the final "answer now" call after the loop stops
_FINAL_ANSWER_RESERVE_SECONDS = 90

# Sent once when the agent answers before it has looked at the code
_INVESTIGATE_FIRST = (
    "You answered after {calls} code lookup(s). This review is only useful if it checks the change "
    "against the code around it. Before your final answer: grep the callers of every method, endpoint "
    "or event whose signature or behavior this diff changes (in this repository and the related ones), "
    "and read the definitions of the types and methods the change relies on. Keep only findings the "
    "code confirms. Then reply with the JSON array."
)

_FINAL_ANSWER_REQUEST = (
    "Your tool budget for this review is used up. Do not call any more tools. "
    "Reply now with the final JSON array of findings only (or [] if none are confirmed)."
)


class AgentReviewFailed(Exception):
    """The agentic review produced no usable result; reason is a short, user-facing phrase."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _ToolCall:
    def __init__(self, call_id: str, name: str, args: Any) -> None:
        self.id, self.name, self.args = call_id, name, args


class _Turn:
    def __init__(self, text: str, tool_calls: list[_ToolCall], input_tokens: int, output_tokens: int,
                 cached_tokens: int = 0) -> None:
        self.text, self.tool_calls = text, tool_calls
        self.input_tokens, self.output_tokens, self.cached_tokens = input_tokens, output_tokens, cached_tokens


class _OpenAIChat:
    """OpenAI-compatible chat with function calling (GLM via Z.AI)."""

    def __init__(self, client, model: str, max_tokens: int) -> None:
        self._client, self._model, self._max_tokens = client, model, max_tokens
        self._tools = [{"type": "function", "function": s} for s in TOOL_SCHEMAS]

    def start(self, user_text: str) -> list[dict]:
        return [{"role": "user", "content": user_text}]

    def call(self, system: str, messages: list[dict]) -> _Turn:
        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=[{"role": "system", "content": system}, *messages],
            tools=self._tools,
        )
        message = response.choices[0].message
        calls = []
        for tc in message.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = None  # CodeTools answers "arguments must be a JSON object"
            calls.append(_ToolCall(tc.id, tc.function.name, args))
        if calls:
            messages.append({
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
                    for tc in message.tool_calls
                ],
            })
        input_tokens, cached = openai_input_tokens(response.usage)  # Z.AI caches repeated prefixes itself
        return _Turn(message.content or "", calls, input_tokens, response.usage.completion_tokens, cached)

    def add_results(self, messages: list[dict], results: list[tuple[_ToolCall, str]]) -> None:
        for call, output in results:
            messages.append({"role": "tool", "tool_call_id": call.id, "content": output})

    def add_user(self, messages: list[dict], text: str) -> None:
        messages.append({"role": "user", "content": text})

    def add_assistant(self, messages: list[dict], text: str) -> None:
        messages.append({"role": "assistant", "content": text})


class _ClaudeChat:
    """Anthropic Messages API with tool use."""

    def __init__(self, client, model: str, max_tokens: int) -> None:
        self._client, self._model, self._max_tokens = client, model, max_tokens
        self._tools = [{"name": s["name"], "description": s["description"], "input_schema": s["parameters"]}
                       for s in TOOL_SCHEMAS]

    def start(self, user_text: str) -> list[dict]:
        return [{"role": "user", "content": user_text}]

    @staticmethod
    def _with_cache_breakpoint(messages: list[dict]) -> list[dict]:
        """The conversation with a cache breakpoint on its last block.

        Each agent step re-sends the whole conversation; with the breakpoint moving to the
        newest message, every step reads everything before it from the prompt cache
        (~0.1× the price) instead of paying for it again. Only a copy is marked, so at most
        two breakpoints (system + last message) are ever sent.
        """
        marked = list(messages)
        last = dict(marked[-1])
        content = last["content"]
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else [
            dict(b) if isinstance(b, dict) else b for b in content]
        if blocks and isinstance(blocks[-1], dict):
            blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
        last["content"] = blocks
        marked[-1] = last
        return marked

    def call(self, system: str, messages: list[dict]) -> _Turn:
        response = self._client.beta.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=self._with_cache_breakpoint(messages),
            tools=self._tools,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            raise AgentReviewFailed("the model declined the request")
        calls = [_ToolCall(b.id, b.name, b.input) for b in response.content if b.type == "tool_use"]
        if calls:
            messages.append({"role": "assistant", "content": response.content})
        text = "".join(b.text for b in response.content if b.type == "text")
        input_tokens, cached = claude_input_tokens(response.usage)
        return _Turn(text, calls, input_tokens, response.usage.output_tokens, cached)

    def add_results(self, messages: list[dict], results: list[tuple[_ToolCall, str]]) -> None:
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call.id, "content": output} for call, output in results
        ]})

    def add_user(self, messages: list[dict], text: str) -> None:
        messages.append({"role": "user", "content": text})

    def add_assistant(self, messages: list[dict], text: str) -> None:
        messages.append({"role": "assistant", "content": text or "[]"})


class AgentReviewer(AIReviewer):
    def _chat(self):
        if self._provider == "glm":
            return _OpenAIChat(self._client, self._settings.GLM_MODEL, self._settings.AI_MAX_TOKENS)
        return _ClaudeChat(self._client, self._settings.AI_MODEL, self._settings.AI_MAX_TOKENS)

    def _agent_prompt(self, language: str, focus: Optional[list[str]], max_steps: int) -> str:
        agent_part = (Path(_PROMPTS_DIR) / _AGENT_PROMPT_FILE).read_text().replace("{max_steps}", str(max_steps))
        return self._load_prompt(language, focus) + "\n\n" + agent_part

    def _budget_left(self) -> bool:
        budget = self._settings.AI_DAILY_TOKEN_BUDGET
        return budget <= 0 or int(self._redis.get(self._budget_key()) or 0) < budget

    def review_agentic(self, pr_context: PRContext, config: ReviewConfig, tools: CodeTools,
                       context: str = "", deadline: Optional[float] = None) -> AIReviewResult:
        """deadline: time.monotonic() value by which the review must be finished."""
        max_chars = self._settings.AGENT_MAX_DIFF_TOKENS * 4
        chunks = self._pack(pr_context.diff, max_chars)
        if len(chunks) > _MAX_AGENT_CHUNKS:
            raise AgentReviewFailed(f"diff too large for agentic review ({len(chunks)} parts)")
        if not self._budget_left():
            raise AgentReviewFailed("daily AI token budget exhausted")

        max_steps = max(1, int(config.agent_max_steps))
        system = self._agent_prompt(pr_context.language, config.ai_focus, max_steps)
        result = AIReviewResult(chunks_total=len(chunks))
        result.tool_calls = 0
        token_budget = {"left": self._settings.AGENT_MAX_TOKENS_PER_REVIEW}

        for i, chunk in enumerate(chunks):
            header = f"{context}\n\n---\n" if context else ""
            part = f" (part {i + 1} of {len(chunks)})" if len(chunks) > 1 else ""
            user_text = f"{header}{tools.describe()}\n\n---\nDIFF{part}:\n{chunk}"
            findings = self._converse(system, user_text, tools, max_steps, token_budget, deadline)
            result.extend(self._only_diff_files(findings, chunk))
            result.chunks_reviewed += 1
        result.tool_calls = tools.calls
        return result

    @staticmethod
    def _only_diff_files(findings: list, chunk: str) -> list:
        """Findings can only be posted on files of this diff — not on code the agent read elsewhere."""
        files = {fd.path for fd in split_diff_by_file(chunk)}
        kept = [f for f in findings if f.file in files]
        if len(kept) < len(findings):
            logger.info("Dropped %d agent finding(s) on files outside the diff", len(findings) - len(kept))
        return kept

    def _pack(self, diff: str, max_chars: int) -> list[str]:
        if len(diff) <= max_chars:
            return [diff]
        chunks, current = [], ""
        for file_diff in self._split_by_file(diff):
            if current and len(current) + len(file_diff) > max_chars:
                chunks.append(current)
                current = ""
            current += file_diff
        return chunks + ([current] if current else [])

    def _converse(self, system: str, user_text: str, tools: CodeTools, max_steps: int,
                  token_budget: dict, deadline: Optional[float]) -> list:
        chat = self._chat()
        messages = chat.start(user_text)
        steps = 0
        forced = False
        nudged = False
        min_calls = max(0, self._settings.AGENT_MIN_TOOL_CALLS)
        while True:
            out_of_time = deadline is not None and time.monotonic() > deadline - _FINAL_ANSWER_RESERVE_SECONDS
            if not forced and (steps >= max_steps or token_budget["left"] <= 0 or out_of_time):
                logger.info("Agent asked for its final answer (steps=%d, tokens_left=%d, out_of_time=%s)",
                            steps, token_budget["left"], out_of_time)
                chat.add_user(messages, _FINAL_ANSWER_REQUEST)
                forced = True
            if deadline is not None and time.monotonic() > deadline:
                raise AgentReviewFailed("agent ran out of time")
            if not self._budget_left():
                raise AgentReviewFailed("daily AI token budget exhausted")

            try:
                turn = chat.call(system, messages)
            except AgentReviewFailed:
                raise
            except Exception as exc:
                raise AgentReviewFailed(f"AI request failed: {type(exc).__name__}") from exc
            self._check_and_record_tokens(turn.input_tokens, turn.output_tokens, turn.cached_tokens)
            token_budget["left"] -= self._weighted(turn.input_tokens, turn.output_tokens, turn.cached_tokens)

            if turn.tool_calls:
                if forced:
                    raise AgentReviewFailed("agent kept calling tools instead of answering")
                results = []
                for call in turn.tool_calls:
                    steps += 1
                    output = tools.run(call.name, call.args)
                    logger.debug("Agent tool %s(%s) → %d chars", call.name, call.args, len(output))
                    results.append((call, output))
                chat.add_results(messages, results)
                continue

            if steps < min_calls and not nudged and not forced:
                # It answered from the diff alone — that is the diff review, not what agentic is for
                logger.info("Agent answered after %d code lookup(s); asking it to check the code first", steps)
                chat.add_assistant(messages, turn.text)
                chat.add_user(messages, _INVESTIGATE_FIRST.format(calls=steps))
                nudged = True
                continue
            items = self._parse_response(turn.text)
            if items is None:
                raise AgentReviewFailed("agent's answer was not valid JSON")
            logger.info("Agent answered after %d tool call(s)", steps)
            return [f for f in (self._validate_finding(it) for it in items if isinstance(it, dict)) if f]
