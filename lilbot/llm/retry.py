"""Retry timing: jittered exponential backoff + Retry-After parsing.

Modeled on Hermes ``agent/retry_utils.py``. Jitter decorrelates concurrent
retries (main loop, recall prefetch, subagents) so they don't all hit a
rate-limited endpoint again at the same instant (thundering herd).
"""
from __future__ import annotations

import random
import time
from email.utils import parsedate_to_datetime

DEFAULT_BASE_DELAY = 2.0
DEFAULT_MAX_DELAY = 60.0

_rng = random.Random()


def jittered_backoff(
    attempt: int,
    *,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    jitter_ratio: float = 0.5,
) -> float:
    """``min(base * 2^(attempt-1), max) + uniform(0, jitter_ratio * that)``.

    ``attempt`` is 1-based: 2s, 4s, 8s, ... (+ up to 50% jitter), capped.
    """
    exponent = max(0, attempt - 1)
    delay = max_delay if exponent >= 30 else min(base_delay * (2 ** exponent), max_delay)
    return delay + _rng.uniform(0, jitter_ratio * delay)


def parse_retry_after(value: str | None) -> float | None:
    """Seconds to wait from a ``Retry-After`` header (delta-seconds or HTTP date)."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - time.time())


def retry_delay(attempt: int, retry_after: float | None = None, *, max_delay: float = DEFAULT_MAX_DELAY) -> float:
    """Honor the server's Retry-After when given (capped), else back off."""
    if retry_after is not None:
        return min(retry_after, max_delay)
    return jittered_backoff(attempt, max_delay=max_delay)
