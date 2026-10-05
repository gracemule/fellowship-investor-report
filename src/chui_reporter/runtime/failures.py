"""What kind of failure is this, and what should happen next?

A run can fail for reasons that need opposite responses. A rate limit or a dropped
connection will pass: wait and try again from the last checkpoint. An expired key or an
exhausted balance will not: say so plainly and stop, with the work saved. A conversation that
has grown too long needs trimming, and a malformed history needs repairing. Retrying a hard
failure wastes time and money; giving up on a transient one throws away a run that was fine.

Classification is by status code and class name rather than by importing every provider's
exception types, so it works for DeepSeek, Anthropic and the database alike.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Failure:
    kind: str           # rate_limit | transient | database | context | history | auth | quota | bad_request | step_limit | unknown
    retry: bool
    message: str        # one plain sentence for the person watching
    base: float = 3.0   # first backoff, seconds
    attempts: int = 6


def _status(exc: BaseException) -> int | None:
    for attr in ("status_code", "http_status"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    resp = getattr(exc, "response", None)
    v = getattr(resp, "status_code", None)
    return v if isinstance(v, int) else None


def _chain(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def classify(exc: BaseException) -> Failure:
    for e in _chain(exc):
        name = type(e).__name__
        text = str(e).lower()
        status = _status(e)

        if name == "GraphRecursionError":
            return Failure("step_limit", False, "The agent used its step allowance for one stretch of work.")
        if status == 402 or "insufficient balance" in text or "insufficient_quota" in text or "credit balance" in text:
            return Failure("quota", False, "The model provider reports the account is out of credit. "
                           "Top it up, then continue; the work so far is saved.")
        if status in (401, 403) or name in ("AuthenticationError", "PermissionDeniedError"):
            return Failure("auth", False, "The model provider rejected the API key. Check the key in the "
                           "server settings, then continue; the work so far is saved.")
        if status == 429 or name == "RateLimitError" or "rate limit" in text:
            return Failure("rate_limit", True, "The model provider is rate-limiting requests.", base=8.0, attempts=8)
        if status == 400 or name == "BadRequestError":
            if any(k in text for k in ("context length", "maximum context", "too many tokens", "prompt is too long",
                                       "context_length")):
                return Failure("context", False, "The conversation had grown too long for the model.")
            if any(k in text for k in ("tool_call", "tool call", "tool_use", "tool_result", "must be followed",
                                       "reasoning_content")):
                return Failure("history", False, "The saved conversation was left mid-step.")
            return Failure("bad_request", False, "The model provider rejected the request: "
                           + str(e).strip().splitlines()[0][:160])
        if status in (500, 502, 503, 504, 529) or name in ("InternalServerError", "APIConnectionError",
                                                           "APITimeoutError", "ServiceUnavailableError",
                                                           "OverloadedError"):
            return Failure("transient", True, "The model provider is temporarily unavailable.")
        if name in ("ConnectError", "ReadTimeout", "ConnectTimeout", "ReadError", "WriteError", "PoolTimeout",
                    "RemoteProtocolError", "TimeoutException", "TransportError", "ConnectionError",
                    "ConnectionResetError", "TimeoutError", "ProtocolError", "IncompleteRead"):
            return Failure("transient", True, "The connection to the model provider dropped.")
        if name in ("OperationalError", "InterfaceError", "AdminShutdown", "ConnectionTimeout") or \
                ("connection" in text and ("closed" in text or "refused" in text or "reset" in text)
                 and "psycopg" in type(e).__module__):
            return Failure("database", True, "The database connection dropped.", base=2.0)
    return Failure("unknown", False, f"{type(exc).__name__}: {str(exc).strip().splitlines()[0][:200] if str(exc).strip() else 'unexpected error'}")


def retry_after(exc: BaseException) -> float | None:
    """A provider's own instruction beats our guess."""
    for e in _chain(exc):
        resp = getattr(e, "response", None)
        headers = getattr(resp, "headers", None)
        if headers is not None:
            v = headers.get("retry-after") or headers.get("Retry-After")
            try:
                if v:
                    return min(float(v), 120.0)
            except ValueError:
                pass
    return None


def backoff(failure: Failure, attempt: int, exc: BaseException | None = None, *, rng=random.random) -> float:
    """Exponential with jitter, capped, honouring Retry-After. `attempt` starts at 0."""
    hinted = retry_after(exc) if exc is not None else None
    delay = min(90.0, failure.base * (2 ** attempt))
    delay *= 0.75 + 0.5 * rng()
    return max(hinted or 0.0, delay)
