"""Resilient call wrapper for Databricks API interactions.

Single place implementing throttling-aware retry with exponential backoff +
jitter. Every mutating/read call the uc_gateway makes (SQL Statement
Execution API, Jobs API, or any future REST usage) should go through
`with_retries()` rather than re-implementing retry logic ad hoc. See
DESIGN.md section 10.1 for the full rationale.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Callable, TypeVar

import requests

logger = logging.getLogger("abac_migration.retry")

T = TypeVar("T")


class NonRetryableError(Exception):
    """Raised by callers to force-fail without retry, e.g. on a Databricks
    error_code that is semantic (PRINCIPAL_DOES_NOT_EXIST, POLICY_NOT_FOUND)
    rather than transient."""


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 5
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    jitter: bool = True
    retryable_status_codes: frozenset = field(default_factory=lambda: frozenset({429, 503, 504}))
    retryable_exceptions: tuple = (
        requests.exceptions.Timeout,
        requests.exceptions.ConnectionError,
    )


@dataclass
class RetryStats:
    attempts: int = 0
    total_wait_s: float = 0.0
    retried_due_to_status: list = field(default_factory=list)
    exhausted: bool = False


def _compute_delay(attempt: int, policy: RetryPolicy, retry_after: float | None) -> float:
    if retry_after is not None:
        return max(0.0, retry_after)
    delay = min(policy.max_delay_s, policy.base_delay_s * (2 ** attempt))
    if policy.jitter:
        delay = delay * (1 + random.uniform(-0.2, 0.2))
    return max(0.0, delay)


def with_retries(
    fn: Callable[[], requests.Response],
    policy: RetryPolicy | None = None,
    stats: RetryStats | None = None,
    label: str = "call",
) -> requests.Response:
    """Executes fn() -> requests.Response, retrying only on throttling/
    transient failures. Non-retryable HTTP errors (4xx other than 429) are
    returned as-is (caller decides how to raise) rather than retried.
    """
    policy = policy or RetryPolicy()
    stats = stats if stats is not None else RetryStats()

    attempt = 0
    while True:
        stats.attempts += 1
        try:
            resp = fn()
        except policy.retryable_exceptions as exc:  # transient network errors
            if attempt >= policy.max_retries:
                stats.exhausted = True
                logger.warning("%s: exhausted retries after transient exception: %s", label, exc)
                raise
            delay = _compute_delay(attempt, policy, retry_after=None)
            logger.info("%s: transient exception (%s), retrying in %.2fs (attempt %d/%d)",
                        label, exc, delay, attempt + 1, policy.max_retries)
            time.sleep(delay)
            stats.total_wait_s += delay
            attempt += 1
            continue

        if resp.status_code not in policy.retryable_status_codes:
            return resp  # success OR a non-retryable error - caller handles

        if attempt >= policy.max_retries:
            stats.exhausted = True
            logger.warning("%s: exhausted retries at HTTP %d", label, resp.status_code)
            return resp

        retry_after = None
        header_val = resp.headers.get("Retry-After")
        if header_val is not None:
            try:
                retry_after = float(header_val)
            except ValueError:
                retry_after = None
        delay = _compute_delay(attempt, policy, retry_after)
        stats.retried_due_to_status.append(resp.status_code)
        logger.info("%s: HTTP %d, retrying in %.2fs (attempt %d/%d)%s",
                    label, resp.status_code, delay, attempt + 1, policy.max_retries,
                    " [Retry-After honored]" if retry_after is not None else "")
        time.sleep(delay)
        stats.total_wait_s += delay
        attempt += 1
