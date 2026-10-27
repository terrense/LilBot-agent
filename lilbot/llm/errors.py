"""Classify provider failures into a reason + recovery hints.

Modeled on Hermes ``agent/error_classifier.py``: the retry loop asks
``classify(exc)`` what to do instead of matching status codes / strings
itself. Structured signals (``ProviderError.status_code`` / ``is_overflow`` /
``reason``, httpx exception types) win; message text is only a fallback.
"""
from __future__ import annotations

import enum
import re
from dataclasses import dataclass


class FailoverReason(str, enum.Enum):
    auth = "auth"                      # 401/403 — bad key; retrying won't help
    billing = "billing"                # 402 / quota exhausted — try the fallback model
    rate_limit = "rate_limit"          # 429 — back off, then fall back
    overloaded = "overloaded"          # 503/529 — back off, then fall back
    server_error = "server_error"      # 500/502/other 5xx — retry
    timeout = "timeout"                # 408/504 / read or connect timeout — retry
    network = "network"                # connection refused/reset, DNS — retry
    context_overflow = "context_overflow"  # prompt too long / 413 — compact, don't retry as-is
    model_not_found = "model_not_found"    # 404 / unknown model — fall back
    format_error = "format_error"      # other 4xx — the request itself is wrong
    unknown = "unknown"                # not a provider failure (a bug) — surface it


@dataclass(frozen=True)
class ClassifiedError:
    reason: FailoverReason
    retryable: bool = False        # same request may succeed after a wait
    should_compress: bool = False  # shrink the context, then retry
    should_fallback: bool = False  # worth trying the fallback model
    retry_after: float | None = None  # server-requested wait (Retry-After), seconds
    status_code: int | None = None


_BILLING_HINTS = re.compile(
    r"insufficient[_ ]quota|insufficient[_ ]balance|billing|credit|余额|欠费|quota exceeded",
    re.IGNORECASE,
)
_MODEL_HINTS = re.compile(
    r"model[_ ]not[_ ]found|does not exist|unknown model|invalid model|no such model|模型不存在",
    re.IGNORECASE,
)


def _from_reason(reason: FailoverReason, *, status: int | None, retry_after: float | None) -> ClassifiedError:
    if reason is FailoverReason.context_overflow:
        return ClassifiedError(reason, should_compress=True, status_code=status)
    if reason in {FailoverReason.rate_limit, FailoverReason.overloaded}:
        return ClassifiedError(reason, retryable=True, should_fallback=True, retry_after=retry_after, status_code=status)
    if reason in {FailoverReason.server_error, FailoverReason.timeout, FailoverReason.network}:
        return ClassifiedError(reason, retryable=True, should_fallback=True, retry_after=retry_after, status_code=status)
    if reason in {FailoverReason.billing, FailoverReason.model_not_found}:
        return ClassifiedError(reason, should_fallback=True, status_code=status)
    return ClassifiedError(reason, status_code=status)


def _reason_for_status(status: int, text: str) -> FailoverReason:
    if status in {401, 403}:
        return FailoverReason.auth
    if status == 402 or _BILLING_HINTS.search(text):
        return FailoverReason.billing
    if status == 429:
        return FailoverReason.rate_limit
    if status == 413:
        return FailoverReason.context_overflow
    if status in {503, 529}:
        return FailoverReason.overloaded
    if status in {408, 504}:
        return FailoverReason.timeout
    if status >= 500:
        return FailoverReason.server_error
    if status == 404 or _MODEL_HINTS.search(text):
        return FailoverReason.model_not_found
    return FailoverReason.format_error


def classify(exc: BaseException) -> ClassifiedError:
    status = getattr(exc, "status_code", None)
    retry_after = getattr(exc, "retry_after", None)
    text = str(exc)

    if getattr(exc, "is_overflow", False):
        return _from_reason(FailoverReason.context_overflow, status=status, retry_after=None)
    explicit = getattr(exc, "reason", None)
    if explicit:
        try:
            return _from_reason(FailoverReason(explicit), status=status, retry_after=retry_after)
        except ValueError:
            pass
    if isinstance(status, int):
        return _from_reason(_reason_for_status(status, text), status=status, retry_after=retry_after)

    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is a hard dependency
        httpx = None  # type: ignore[assignment]
    if httpx is not None:
        if isinstance(exc, httpx.TimeoutException):
            return _from_reason(FailoverReason.timeout, status=None, retry_after=None)
        if isinstance(exc, httpx.TransportError):
            return _from_reason(FailoverReason.network, status=None, retry_after=None)
    if isinstance(exc, (TimeoutError,)):
        return _from_reason(FailoverReason.timeout, status=None, retry_after=None)
    if isinstance(exc, (ConnectionError,)):
        return _from_reason(FailoverReason.network, status=None, retry_after=None)
    return ClassifiedError(FailoverReason.unknown)
