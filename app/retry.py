# app/retry.py
"""Retry policy for broker calls.

The distinction that matters: a TRANSIENT error (timeout, 503, socket reset, rate
limit) means the request may succeed if repeated, so back off and retry. A REJECT
(order rejected, insufficient buying power, bad symbol, auth failure) is a decision,
not a hiccup: repeating it just gets rejected again, so it is terminal and must NOT
be retried. Retrying a reject is how a system spams a venue and gets throttled.

Backoff is exponential with jitter, capped, and bounded by max attempts.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, Tuple, Type, TypeVar

T = TypeVar("T")


class BrokerError(Exception):
    """Base for adapter-raised errors."""


class TransientBrokerError(BrokerError):
    """Retryable: the call might succeed if tried again (timeout, 5xx, rate limit)."""


class RejectError(BrokerError):
    """Terminal: the broker refused the request on its merits. Never retried."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 0.2      # seconds
    max_delay: float = 5.0
    jitter: float = 0.1          # +/- fraction of the computed delay

    def delay_for(self, attempt: int) -> float:
        """attempt is 1-based. Exponential: base * 2^(attempt-1), capped, jittered."""
        raw = self.base_delay * (2 ** (attempt - 1))
        raw = min(raw, self.max_delay)
        spread = raw * self.jitter
        return max(0.0, raw + random.uniform(-spread, spread))


# Only these are retried. RejectError is deliberately excluded.
RETRYABLE: Tuple[Type[Exception], ...] = (TransientBrokerError,)


def call_with_retry(fn: Callable[[], T], policy: RetryPolicy,
                    sleep: Callable[[float], None] = time.sleep) -> T:
    """Run fn, retrying only transient failures. A RejectError propagates immediately.

    sleep is injectable so tests (and the async path) can drive it without real waits.
    """
    last: Exception
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return fn()
        except RejectError:
            # Terminal by definition. Do not retry, do not swallow.
            raise
        except RETRYABLE as exc:
            last = exc
            if attempt == policy.max_attempts:
                break
            sleep(policy.delay_for(attempt))
    raise last


async def call_with_retry_async(fn, policy: RetryPolicy, sleep=None) -> object:
    """Async twin of call_with_retry. Same reject-vs-transient rule."""
    import asyncio

    _sleep = sleep or asyncio.sleep
    last: Exception
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await fn()
        except RejectError:
            raise
        except RETRYABLE as exc:
            last = exc
            if attempt == policy.max_attempts:
                break
            await _sleep(policy.delay_for(attempt))
    raise last
