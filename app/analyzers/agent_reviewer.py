"""Agentic review: the model reads the repository with tools before judging the change.

Same output as AIReviewer (a list of Findings), same provider, budget and prompts. The
difference is the loop: instead of one request with the diff, the model may call
read_file / grep / find_files / list_dir (app/context/code_tools.py) to check callers,
definitions and guards, and only then answers with the JSON findings.

A diff too big for one conversation is split into parts. The parts run in parallel
within one time window; a part that fails keeps the findings of the others, and its
files are returned in `unreviewed_diff` for the worker's diff review. Only when no part
succeeds does it raise AgentReviewFailed (the worker then falls back to the diff review).

Every request is bounded by the part's deadline (app/analyzers/ai_calls.py): tool turns
stop in time to leave the final answer its reserve, and the final answer itself cannot
run past the deadline.
"""
import json
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from celery.exceptions import SoftTimeLimitExceeded

from app.analyzers.ai_calls import RequestTimedOut, call_bounded, classify_error, timeout_for
from app.analyzers.ai_reviewer import (_PROMPTS_DIR, AIReviewer, AIReviewResult, _count, claude_input_tokens,
                                       openai_input_tokens, openai_reasoning_tokens)
from app.context.code_tools import TOOL_SCHEMAS, CodeTools
from app.models import PRContext, ReviewConfig
from app.notifiers.alerts import send_alert, today
from app.utils.diff_parser import split_diff_by_file

logger = logging.getLogger(__name__)

_AGENT_PROMPT_FILE = "agent_review.md"
# More chunks than this and the review is too big for the agent — use the diff review
_MAX_AGENT_CHUNKS = 3
# Final-answer reserve per provider (AGENT_FINAL_ANSWER_RESERVE_SECONDS=0). GLM with
# thinking on took ~250 s for its final answer (PR #52); with thinking off it is a short reply.
_CLAUDE_ANSWER_RESERVE = 90
_GLM_ANSWER_RESERVE_THINKING_OFF = 120
_GLM_ANSWER_RESERVE_THINKING_ON = 270
# Shortest timeout worth sending a final answer with (thinking off / on)
_ANSWER_FLOOR_FAST = 60
_ANSWER_FLOOR_SLOW = 180
# Shortest timeout worth sending a tool turn with (raised to the slowest turn so far)
_TOOL_FLOOR = 60
# Reasoning on a thinking-off answer above this is logged. Z.AI honours the switch (checked
# by hand) yet still reasons ~1k tokens on long agent conversations — a few seconds, so it
# changes nothing; the reserve follows how long answers actually take.
_THINKING_OFF_NOTE_REASONING = 1000
# Extra wait for parallel parts after the deadline before they are abandoned
_PARTS_JOIN_GRACE_SECONDS = 15

# Per process; a restart re-checks both.
# Z.AI answered 400 to the thinking parameter and a resend without it worked: stop sending it.
_glm_thinking_param_rejected = False
# Slowest thinking-off final answer seen (seconds); the GLM reserve grows to cover it.
_glm_slowest_answer = 0.0

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

# Sent once when the final answer is not a JSON array (models drift into prose after many tool calls)
_JSON_ONLY = (
    "Your last reply was not a JSON array, so it cannot be used. Do not call any more tools and do not "
    "explain. Reply with the findings from your review as a JSON array only, in the schema from the "
    "instructions, starting with `[` and ending with `]` (or [] if none are confirmed)."
)

# Sent once when the answer was cut off at max_tokens with nothing in it
_CUT_OFF = (
    "Your reply was cut off before it contained the answer. Do not call any more tools and do not "
    "deliberate at length. Reply with the findings from your review as a JSON array only, keeping each "
    "message and suggestion short (or [] if none are confirmed)."
)

# Results for tool calls that are refused
_NO_MORE_LOOKUPS = "ERROR: no more lookups, answer now"
_LOOKUP_BUDGET_USED = "ERROR: lookup budget used, answer now"


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
                 cached_tokens: int = 0, finish_reason: Optional[str] = None, reasoning_tokens: int = 0) -> None:
        self.text, self.tool_calls = text, tool_calls
        self.input_tokens, self.output_tokens, self.cached_tokens = input_tokens, output_tokens, cached_tokens
        self.finish_reason, self.reasoning_tokens = finish_reason, reasoning_tokens

    @property
    def cut_off(self) -> bool:
        return not self.text.strip() and self.finish_reason in ("length", "max_tokens")


@dataclass
class _Bounds:
    """How one request is bounded — passed to call_bounded."""
    deadline: Optional[float]
    reserve: float
    cap: float
    floor: float
    kind: str
    on_timeout: Optional[Callable[[str], None]] = None

    def call(self, fn):
        return call_bounded(fn, deadline=self.deadline, reserve=self.reserve, cap=self.cap, floor=self.floor,
                            kind=self.kind, on_timeout=self.on_timeout)


def _glm_thinking_off(mode: str) -> bool:
    """Whether a GLM final answer is sent with thinking off."""
    return mode in ("disabled", "auto") and not _glm_thinking_param_rejected


def _final_reserve(settings, provider: str) -> float:
    """Seconds kept for the final answer after the tool turns stop."""
    if settings.AGENT_FINAL_ANSWER_RESERVE_SECONDS > 0:
        return float(settings.AGENT_FINAL_ANSWER_RESERVE_SECONDS)
    if provider != "glm":
        return float(_CLAUDE_ANSWER_RESERVE)
    if _glm_thinking_off(settings.GLM_ANSWER_THINKING.lower()):
        # Room for the slowest answer seen so far plus a margin, between the two fixed values
        observed = _glm_slowest_answer * 1.3 + 15 if _glm_slowest_answer else 0
        return float(min(_GLM_ANSWER_RESERVE_THINKING_ON, max(_GLM_ANSWER_RESERVE_THINKING_OFF, observed)))
    return float(_GLM_ANSWER_RESERVE_THINKING_ON)


def _answer_floor(reserve: float) -> float:
    """Shortest timeout worth sending the final answer with: 60 s, up to 180 s as the reserve grows."""
    if reserve <= _GLM_ANSWER_RESERVE_THINKING_OFF:
        return float(_ANSWER_FLOOR_FAST)
    return float(min(_ANSWER_FLOOR_SLOW, max(_ANSWER_FLOOR_FAST, reserve * 2 / 3)))


class _OpenAIChat:
    """OpenAI-compatible chat with function calling (GLM via Z.AI)."""

    def __init__(self, client, model: str, max_tokens: int, thinking_mode: str = "auto",
                 on_thinking_unsupported: Optional[Callable[[str], None]] = None,
                 on_slow_answer: Optional[Callable[[str], None]] = None, turns_thinking_off: bool = False) -> None:
        self._client, self._model, self._max_tokens = client, model, max_tokens
        self._thinking_mode = thinking_mode.lower()
        self._on_thinking_unsupported = on_thinking_unsupported
        self._on_slow_answer = on_slow_answer
        self._turns_thinking_off = turns_thinking_off
        self._tools = [{"type": "function", "function": s} for s in TOOL_SCHEMAS]

    def start(self, user_text: str) -> list[dict]:
        return [{"role": "user", "content": user_text}]

    def _send(self, system: str, messages: list[dict], timeout: float, thinking_off: bool):
        kwargs: dict[str, Any] = dict(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=[{"role": "system", "content": system}, *messages],
            tools=self._tools,
            timeout=timeout,
        )
        if thinking_off:
            # The forced answer needs no deliberation: the lookups are done. With thinking on,
            # glm-5.3-flash spent ~4 minutes reasoning before it wrote the JSON (PR #52).
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        return self._client.chat.completions.create(**kwargs)

    def call(self, system: str, messages: list[dict], bounds: _Bounds, answer: bool = False) -> _Turn:
        global _glm_thinking_param_rejected, _glm_slowest_answer
        if answer:
            thinking_off = _glm_thinking_off(self._thinking_mode)
        else:  # tool turns: GLM_THINKING (thinking took 60-150 s per turn, without it 2-5 s)
            thinking_off = self._turns_thinking_off and not _glm_thinking_param_rejected
        started = time.monotonic()
        try:
            response = bounds.call(lambda t: self._send(system, messages, t, thinking_off))
        except RequestTimedOut as exc:
            # A final answer that did not arrive: keep sending thinking-off (Z.AI honours it),
            # but give later answers the longest reserve
            if answer and thinking_off and exc.sent and _glm_slowest_answer < _GLM_ANSWER_RESERVE_THINKING_ON:
                _glm_slowest_answer = float(_GLM_ANSWER_RESERVE_THINKING_ON)
                logger.warning("GLM final answer did not arrive within %.0fs; the final-answer reserve is %ds "
                               "from now on", exc.timeout, _GLM_ANSWER_RESERVE_THINKING_ON)
                if self._on_slow_answer is not None:
                    self._on_slow_answer(f"A final answer did not arrive within {exc.timeout:.0f}s.")
            raise
        except Exception as exc:
            if not (thinking_off and classify_error(exc) == "bad_request"):
                raise
            # Resent without the parameter. The flag changes only if that succeeds: Z.AI also
            # answers 400 for other reasons (content filter, context length), which say
            # nothing about the thinking switch.
            response = bounds.call(lambda t: self._send(system, messages, t, False))
            _glm_thinking_param_rejected = True
            logger.warning("Z.AI rejected thinking-off (%s); requests think from now on",
                           type(exc).__name__)
            if self._on_thinking_unsupported is not None:
                self._on_thinking_unsupported(f"rejected: {type(exc).__name__}")
            thinking_off = False

        choice = response.choices[0]
        message = choice.message
        reasoning = openai_reasoning_tokens(response.usage)
        if answer and thinking_off:
            elapsed = time.monotonic() - started
            if elapsed > _glm_slowest_answer:
                _glm_slowest_answer = elapsed
            if reasoning > _THINKING_OFF_NOTE_REASONING:
                logger.info("GLM final answer with thinking off: %d reasoning tokens, %.0fs", reasoning, elapsed)
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
        return _Turn(message.content or "", calls, input_tokens, _count(response.usage, "completion_tokens"), cached,
                     getattr(choice, "finish_reason", None), reasoning)

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

    def call(self, system: str, messages: list[dict], bounds: _Bounds, answer: bool = False) -> _Turn:
        kwargs: dict[str, Any] = dict(
            model=self._model,
            max_tokens=self._max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=self._with_cache_breakpoint(messages),
            tools=self._tools,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if answer:
            kwargs["tool_choice"] = {"type": "none"}  # the tools stay defined (cache), but cannot be called
        response = bounds.call(lambda t: self._client.beta.messages.create(**kwargs, timeout=t))
        if response.stop_reason == "refusal":
            raise AgentReviewFailed("the model declined the request")
        calls = [_ToolCall(b.id, b.name, b.input) for b in response.content if b.type == "tool_use"]
        if calls:
            messages.append({"role": "assistant", "content": response.content})
        text = "".join(b.text for b in response.content if b.type == "text")
        input_tokens, cached = claude_input_tokens(response.usage)
        stop = response.stop_reason if isinstance(response.stop_reason, str) else None
        return _Turn(text, calls, input_tokens, _count(response.usage, "output_tokens"), cached, stop)

    def add_results(self, messages: list[dict], results: list[tuple[_ToolCall, str]]) -> None:
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call.id, "content": output} for call, output in results
        ]})

    def add_user(self, messages: list[dict], text: str) -> None:
        messages.append({"role": "user", "content": text})

    def add_assistant(self, messages: list[dict], text: str) -> None:
        messages.append({"role": "assistant", "content": text or "[]"})


class _SharedBudget:
    """AGENT_MAX_TOKENS_PER_REVIEW shared by the parts of one review, which run in threads."""

    def __init__(self, total: int) -> None:
        self._left = total
        self._lock = threading.Lock()

    def take(self, n: int) -> int:
        with self._lock:
            self._left -= n
            return self._left

    @property
    def left(self) -> int:
        with self._lock:
            return self._left


@dataclass
class _PartOutcome:
    index: int
    findings: list = field(default_factory=list)
    lookups: int = 0
    limits: set = field(default_factory=set)
    failed: Optional[str] = None


class AgentReviewer(AIReviewer):
    def _chat(self):
        max_tokens = self._settings.AGENT_MAX_TOKENS or self._settings.AI_MAX_TOKENS
        if self._provider == "glm":
            return _OpenAIChat(self._client, self._settings.GLM_MODEL, max_tokens,
                               self._settings.GLM_ANSWER_THINKING, self._alert_thinking_unsupported,
                               self._alert_slow_answer,
                               turns_thinking_off=self._settings.GLM_THINKING.strip().lower() == "disabled")
        return _ClaudeChat(self._client, self._settings.AI_MODEL, max_tokens)

    def _alert_thinking_unsupported(self, how: str) -> None:
        send_alert(self._settings, f"glm_thinking_toggle_unsupported:{today()}",
                   "GLM final answers cannot turn thinking off",
                   f"Z.AI {how}. Agent final answers now think and get a {_GLM_ANSWER_RESERVE_THINKING_ON}s reserve, "
                   "which leaves less time for code lookups. Set `GLM_ANSWER_THINKING=enabled` to stop retrying "
                   "after a restart.", self._redis)

    def _alert_slow_answer(self, what: str) -> None:
        send_alert(self._settings, f"glm_slow_final_answer:{today()}", "GLM final answer too slow",
                   f"{what} Until the worker restarts, agent final answers get a {_GLM_ANSWER_RESERVE_THINKING_ON}s "
                   "reserve, which leaves less time for code lookups. That part's files got the diff review.",
                   self._redis)

    def _final_reserve(self) -> float:
        return _final_reserve(self._settings, self._provider)

    def _agent_prompt(self, language: str, focus: Optional[list[str]], max_steps: int) -> str:
        agent_part = (Path(_PROMPTS_DIR) / _AGENT_PROMPT_FILE).read_text().replace("{max_steps}", str(max_steps))
        return self._load_prompt(language, focus) + "\n\n" + agent_part

    def _budget_left(self) -> bool:
        budget = self._settings.AI_DAILY_TOKEN_BUDGET
        return budget <= 0 or int(self._redis.get(self._budget_key()) or 0) < budget

    def review_agentic(self, pr_context: PRContext, config: ReviewConfig, tools: CodeTools,
                       context: str = "", deadline: Optional[float] = None) -> AIReviewResult:
        """deadline: time.monotonic() value by which the review must be finished.

        Returns the findings of the parts that finished; `unreviewed_diff` holds the parts
        that did not. Raises AgentReviewFailed when no part finished.
        """
        max_chars = self._settings.AGENT_MAX_DIFF_TOKENS * 4
        chunks = self._pack(pr_context.diff, max_chars)
        if len(chunks) > _MAX_AGENT_CHUNKS:
            raise AgentReviewFailed(f"diff too large for agentic review ({len(chunks)} parts)")
        if not self._budget_left():
            raise AgentReviewFailed("daily AI token budget exhausted")
        min_window = self._final_reserve() + self._settings.AGENT_MIN_TOOL_WINDOW_SECONDS
        if deadline is not None and deadline - time.monotonic() < min_window:
            raise AgentReviewFailed("not enough time left for a deep review")

        max_steps = max(1, int(config.agent_max_steps))
        system = self._agent_prompt(pr_context.language, config.ai_focus, max_steps)
        result = AIReviewResult(chunks_total=len(chunks))
        budget = _SharedBudget(self._settings.AGENT_MAX_TOKENS_PER_REVIEW)
        stop = threading.Event()
        n = len(chunks)
        parallel = max(1, min(n, self._settings.AGENT_PARALLEL_PARTS))
        started = {"count": 0}
        start_lock = threading.Lock()

        def run_part(i: int, chunk: str) -> _PartOutcome:
            with start_lock:
                not_started = n - started["count"]
                started["count"] += 1
            now = time.monotonic()
            if deadline is None or parallel >= n:
                part_deadline = deadline
            else:
                # Queued parts: each round of `parallel` parts gets its share of what is left
                part_deadline = now + (deadline - now) / math.ceil(not_started / parallel)
            if part_deadline is not None and part_deadline - now < self._final_reserve() + \
                    self._settings.AGENT_MIN_TOOL_WINDOW_SECONDS:
                logger.info("Agent part %d of %d not started: %.0fs left", i + 1, n, part_deadline - now)
                return _PartOutcome(i, failed="no time left for this part")
            header = f"{context}\n\n---\n" if context else ""
            part = f" (part {i + 1} of {n})" if n > 1 else ""
            user_text = f"{header}{tools.describe()}\n\n---\nDIFF{part}:\n{chunk}"
            limits: set[str] = set()
            try:
                findings, lookups = self._converse(system, user_text, tools, max_steps, budget, part_deadline,
                                                   limits, stop, part=i + 1)
            except AgentReviewFailed as exc:
                logger.warning("Agent part %d of %d failed: %s", i + 1, n, exc.reason)
                return _PartOutcome(i, limits=limits, failed=exc.reason)
            return _PartOutcome(i, self._only_diff_files(findings, chunk), lookups, limits)

        if n == 1:
            # Inline: the soft-limit signal is delivered to this (the main) thread directly
            outcomes = [run_part(0, chunks[0])]
        else:
            outcomes = self._run_parts(run_part, chunks, parallel, deadline, stop)

        failed = []
        for outcome in outcomes:  # submission order: deterministic ties in merger.py
            if outcome.failed is None:
                result.extend(outcome.findings)
                result.chunks_reviewed += 1
                result.tool_calls += outcome.lookups
                result.limits_hit |= outcome.limits
            else:
                failed.append(outcome)
        if not result.chunks_reviewed:
            raise AgentReviewFailed(failed[0].failed)
        if failed:
            result.unreviewed_diff = "".join(chunks[o.index] for o in failed)
            result.failed_parts = len(failed)
            result.failed_part_numbers = [o.index + 1 for o in failed]
            result.failure_reason = failed[0].failed
        return result

    @staticmethod
    def _run_parts(run_part, chunks: list[str], parallel: int, deadline: Optional[float],
                   stop: threading.Event) -> list[_PartOutcome]:
        # No `with`: its exit waits for every thread, which would hold the task past the
        # deadline when a part hangs. Abandoned parts see `stop` and end at their next turn.
        ex = ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="agent-part")
        futures = [ex.submit(run_part, i, chunk) for i, chunk in enumerate(chunks)]
        try:
            timeout = None if deadline is None else max(0.0, deadline - time.monotonic()) + _PARTS_JOIN_GRACE_SECONDS
            futures_wait(futures, timeout=timeout)
        finally:
            stop.set()
            ex.shutdown(wait=False, cancel_futures=True)
        outcomes = []
        for i, future in enumerate(futures):
            if future.cancelled() or not future.done():
                outcomes.append(_PartOutcome(i, failed="part did not finish in time"))
                continue
            exc = future.exception()
            if isinstance(exc, SoftTimeLimitExceeded):
                raise exc
            if exc is not None:
                logger.warning("Agent part %d failed with %s: %s", i + 1, type(exc).__name__, exc)
                outcomes.append(_PartOutcome(i, failed=f"error: {type(exc).__name__}"))
            else:
                outcomes.append(future.result())
        return outcomes

    @staticmethod
    def _only_diff_files(findings: list, chunk: str) -> list:
        """Findings can only be posted on files of this diff — not on code the agent read elsewhere."""
        files = {fd.path for fd in split_diff_by_file(chunk)}
        kept = [f for f in findings if f.file in files]
        if len(kept) < len(findings):
            logger.info("Dropped %d agent finding(s) on files outside the diff", len(findings) - len(kept))
        return kept

    def _pack(self, diff: str, max_chars: int) -> list[str]:
        """Whole files in diff order, in parts of about equal size (at most max_chars each).

        Equal parts finish at about the same time when they run in parallel. If balancing
        would need more parts than greedy packing, the greedy packing is used.
        """
        if len(diff) <= max_chars:
            return [diff]
        files = self._split_by_file(diff)
        greedy = self._pack_to(files, max_chars)
        target = math.ceil(len(diff) / math.ceil(len(diff) / max_chars))
        balanced = self._pack_to(files, target)
        return greedy if len(balanced) > len(greedy) else balanced

    @staticmethod
    def _pack_to(files: list[str], limit: int) -> list[str]:
        chunks, current = [], ""
        for file_diff in files:
            if current and len(current) + len(file_diff) > limit:
                chunks.append(current)
                current = ""
            current += file_diff
        return chunks + ([current] if current else [])

    def _converse(self, system: str, user_text: str, tools: CodeTools, max_steps: int,
                  budget: _SharedBudget, deadline: Optional[float], limits_hit: Optional[set] = None,
                  stop: Optional[threading.Event] = None, part: int = 1) -> tuple[list, int]:
        """One conversation over one part of the diff. Returns (findings, code lookups)."""
        chat = self._chat()
        messages = chat.start(user_text)
        settings = self._settings
        steps = 0
        turns = 0
        forced = False
        nudged = False
        extra_turn_used = False   # in forced mode: one more turn, for the JSON re-ask or stray tool calls
        tool_turn_timed_out = False
        was_cut_off = False
        slowest_turn = 0.0
        answer_elapsed = 0.0
        asked_at = None
        min_calls = max(0, settings.AGENT_MIN_TOOL_CALLS)
        while True:
            if stop is not None and stop.is_set():
                raise AgentReviewFailed("part abandoned at the deadline")
            reserve = max(self._final_reserve(), answer_elapsed)
            tool_floor = max(_TOOL_FLOOR, slowest_turn)
            if not forced:
                out_of_time = tool_turn_timed_out or (
                    deadline is not None
                    and timeout_for(deadline, reserve, settings.AGENT_STEP_TIMEOUT_SECONDS, tool_floor) is None)
                if steps >= max_steps or budget.left <= 0 or out_of_time:
                    limit = "time" if out_of_time else "steps" if steps >= max_steps else "tokens"
                    logger.info("Agent asked for its final answer (part=%d, lookups=%d, tokens_left=%d, limit=%s)",
                                part, steps, budget.left, limit)
                    chat.add_user(messages, _FINAL_ANSWER_REQUEST)
                    forced = True
                    asked_at = time.monotonic()
                    if limits_hit is not None:
                        limits_hit.add(limit)
            if not self._budget_left():
                raise AgentReviewFailed("daily AI token budget exhausted")

            if forced:
                bounds = _Bounds(deadline, 0, settings.AI_REQUEST_TIMEOUT_SECONDS,
                                 _answer_floor(self._final_reserve()), "agent_answer", self._record_timeout)
            else:
                bounds = _Bounds(deadline, reserve, settings.AGENT_STEP_TIMEOUT_SECONDS, tool_floor, "agent_turn",
                                 self._record_timeout)
            timeout = timeout_for(bounds.deadline, bounds.reserve, bounds.cap, bounds.floor)
            if timeout is None:
                if forced:  # a request that cannot finish in time is not sent
                    raise AgentReviewFailed("agent's final answer did not arrive in time")
                tool_turn_timed_out = True  # the window closed since the check above
                continue
            sent = time.monotonic()
            try:
                turn = chat.call(system, messages, bounds, answer=forced)
            except RequestTimedOut:
                if forced:
                    raise AgentReviewFailed("agent's final answer did not arrive in time")
                logger.warning("Agent tool turn timed out (part=%d, turn=%d) — asking for the final answer",
                               part, turns + 1)
                tool_turn_timed_out = True
                continue
            except (AgentReviewFailed, SoftTimeLimitExceeded):
                raise
            except Exception as exc:
                raise AgentReviewFailed(f"AI request failed: {type(exc).__name__}") from exc
            elapsed = time.monotonic() - sent
            turns += 1
            self._check_and_record_tokens(turn.input_tokens, turn.output_tokens, turn.cached_tokens)
            budget.take(self._weighted(turn.input_tokens, turn.output_tokens, turn.cached_tokens))
            logger.info("Agent turn: part=%d turn=%d lookups=%d seconds=%.1f in=%d cached=%d out=%d reasoning=%d "
                        "finish=%s timeout=%.0f", part, turns, steps, elapsed, turn.input_tokens, turn.cached_tokens,
                        turn.output_tokens, turn.reasoning_tokens, turn.finish_reason, timeout)

            if turn.tool_calls:
                if forced:
                    if extra_turn_used:
                        raise AgentReviewFailed("agent kept calling tools instead of answering")
                    extra_turn_used = True
                    chat.add_results(messages, [(call, _NO_MORE_LOOKUPS) for call in turn.tool_calls])
                    continue
                slowest_turn = max(slowest_turn, elapsed)
                results = []
                for call in turn.tool_calls:
                    if steps >= max_steps:  # one reply may ask for more lookups than are left
                        results.append((call, _LOOKUP_BUDGET_USED))
                        continue
                    steps += 1
                    output = tools.run(call.name, call.args)
                    logger.debug("Agent tool %s(%s) → %d chars", call.name, call.args, len(output))
                    results.append((call, output))
                chat.add_results(messages, results)
                continue

            answer_elapsed = max(answer_elapsed, elapsed)
            if turn.cut_off:
                was_cut_off = True
                logger.warning("Agent's answer cut off at max_tokens (part=%d, out=%d, reasoning=%d)",
                               part, turn.output_tokens, turn.reasoning_tokens)
            elif steps < min_calls and not nudged and not forced:
                # It answered from the diff alone — that is the diff review, not what agentic is for
                logger.info("Agent answered after %d code lookup(s); asking it to check the code first", steps)
                chat.add_assistant(messages, turn.text)
                chat.add_user(messages, _INVESTIGATE_FIRST.format(calls=steps))
                nudged = True
                continue
            items = None if turn.cut_off else self._parse_response(turn.text)
            if items is None:
                if extra_turn_used:
                    raise AgentReviewFailed("agent's answer was cut off (AGENT_MAX_TOKENS)" if was_cut_off
                                            else "agent's answer was not valid JSON")
                # One more cheap turn (the conversation is cached) beats losing all the lookups
                if turn.cut_off:
                    chat.add_user(messages, _CUT_OFF)
                else:
                    logger.info("Agent's answer was not a JSON array (%d chars); asking for JSON only", len(turn.text))
                    chat.add_assistant(messages, turn.text)
                    chat.add_user(messages, _JSON_ONLY)
                extra_turn_used = forced = True
                asked_at = asked_at or time.monotonic()
                continue
            waited = f", {time.monotonic() - asked_at:.0f}s after it was asked" if asked_at else ""
            logger.info("Agent answered (part=%d) after %d code lookup(s)%s", part, steps, waited)
            return self._validate_findings(items), steps
