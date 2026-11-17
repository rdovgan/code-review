"""Bounded AI requests, shared by the diff review and the agent.

Every request gets a read timeout derived from the deadline of whatever it belongs to
(a diff chunk, an agent turn, a verification pass), so no single request can run past
it. The Celery soft limit stays the backstop: when it fires inside a request the SDK may
wrap it in its own connection error, and it is turned back into SoftTimeLimitExceeded
here so the `except SoftTimeLimitExceeded` lines further up still match.

Errors are classified by duck typing (status_code, class name), so the same code works
for the openai and the anthropic SDK, whatever HTTP library they are built on.
"""
import logging
import time
from typing import Callable, Optional

from celery.exceptions import SoftTimeLimitExceeded

logger = logging.getLogger(__name__)

_TIMEOUT_CLASS_NAMES = {"APITimeoutError", "TimeoutException", "ReadTimeout", "ConnectTimeout",
                        "WriteTimeout", "PoolTimeout"}
# A transient error is retried only when this much time (plus the backoff) is left
_RETRY_MIN_SECONDS = 60
_DEFAULT_BACKOFF = 3.0
_MAX_BACKOFF = 10.0


class RequestTimedOut(Exception):
    """The request got no answer within its timeout, or was not sent because no usable time was left.

    by_deadline: the timeout was cut short by the deadline (or nothing was sent), so
    retrying the same work makes no sense; False means the full per-request cap ran out.
    """

    def __init__(self, by_deadline: bool, timeout: float = 0.0, sent: bool = True) -> None:
        super().__init__(f"AI request timed out after {timeout:.0f}s" if sent else "no time left for an AI request")
        self.by_deadline, self.timeout, self.sent = by_deadline, timeout, sent


def is_soft_limit(exc: Optional[BaseException]) -> bool:
    """True when exc is the Celery soft limit, or wraps it (SDKs re-raise it as a connection error)."""
    for _ in range(6):
        if exc is None:
            return False
        if isinstance(exc, SoftTimeLimitExceeded):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def classify_error(exc: BaseException) -> str:
    """soft_limit | timeout | transient | bad_request | fatal."""
    if is_soft_limit(exc):
        return "soft_limit"
    name = type(exc).__name__
    if name in _TIMEOUT_CLASS_NAMES or name.endswith("APITimeoutError"):
        return "timeout"
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status in (408, 409, 429) or status >= 500:  # 529 = Anthropic overloaded
            return "transient"
        if status == 400:
            return "bad_request"
    if name == "APIConnectionError":
        return "transient"
    return "fatal"


def timeout_for(deadline: Optional[float], reserve: float, cap: float, floor: float) -> Optional[float]:
    """Read timeout for a request that must end `reserve` seconds before `deadline`.

    None when less than `floor` is left: a request that cannot finish in that time is not sent.
    """
    if deadline is None:
        return cap
    t = min(cap, deadline - reserve - time.monotonic())
    return t if t >= floor else None


def _backoff(exc: BaseException) -> float:
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    try:
        return min(float(headers.get("retry-after")), _MAX_BACKOFF)
    except (TypeError, ValueError):
        return _DEFAULT_BACKOFF


def call_bounded(fn: Callable[[float], object], *, deadline: Optional[float], reserve: float, cap: float,
                 floor: float, kind: str, on_timeout: Optional[Callable[[str], None]] = None):
    """fn(timeout) with a timeout from the deadline; one retry on a transient error if time allows.

    Raises RequestTimedOut (no answer in time, or no time to send), SoftTimeLimitExceeded
    (unwrapped), or the original error.
    """
    retried = False
    while True:
        timeout = timeout_for(deadline, reserve, cap, floor)
        if timeout is None:
            raise RequestTimedOut(by_deadline=True, sent=False)
        try:
            return fn(timeout)
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:
            kind_of_error = classify_error(exc)
            if kind_of_error == "soft_limit":
                raise SoftTimeLimitExceeded() from exc
            if kind_of_error == "timeout":
                if on_timeout is not None:
                    on_timeout(kind)
                logger.warning("AI request (%s) got no answer within %.0fs", kind, timeout)
                raise RequestTimedOut(by_deadline=timeout < cap, timeout=timeout) from exc
            if kind_of_error == "transient" and not retried:
                backoff = _backoff(exc)
                left = None if deadline is None else deadline - time.monotonic() - reserve
                if left is None or left >= _RETRY_MIN_SECONDS + backoff:
                    logger.warning("AI request (%s) failed with %s (status %s), retrying once in %.0fs", kind,
                                   type(exc).__name__, getattr(exc, "status_code", "-"), backoff)
                    retried = True
                    time.sleep(backoff)
                    continue
            raise
