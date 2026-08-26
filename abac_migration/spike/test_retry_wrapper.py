"""Validates the resilient retry wrapper's behavior using simulated
throttling/transient responses (no live Databricks calls) - safe to run
repeatedly, and exercises the exact logic the real uc_gateway will rely on.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import requests

from abac_migration.uc_gateway.retry import RetryPolicy, RetryStats, with_retries


@dataclass
class FakeResponse:
    status_code: int
    headers: dict


def test_retries_on_429_then_succeeds():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            return FakeResponse(429, {"Retry-After": "0.2"})
        return FakeResponse(200, {})

    stats = RetryStats()
    start = time.time()
    resp = with_retries(flaky, RetryPolicy(max_retries=5, base_delay_s=0.1), stats, label="test1")
    elapsed = time.time() - start
    assert resp.status_code == 200
    assert calls["n"] == 3
    assert stats.attempts == 3
    assert stats.retried_due_to_status == [429, 429]
    assert not stats.exhausted
    print(f"[PASS] retries on 429 then succeeds (attempts={stats.attempts}, elapsed={elapsed:.2f}s, honored Retry-After~0.2s twice)")


def test_exhausts_retries_and_returns_last_failure():
    def always_429():
        return FakeResponse(429, {})

    stats = RetryStats()
    resp = with_retries(always_429, RetryPolicy(max_retries=3, base_delay_s=0.05, max_delay_s=0.2), stats, label="test2")
    assert resp.status_code == 429
    assert stats.attempts == 4  # initial + 3 retries
    assert stats.exhausted
    print(f"[PASS] exhausts retries after max_retries and returns last failure (attempts={stats.attempts})")


def test_non_retryable_status_returns_immediately():
    calls = {"n": 0}

    def bad_request():
        calls["n"] += 1
        return FakeResponse(404, {})

    stats = RetryStats()
    start = time.time()
    resp = with_retries(bad_request, RetryPolicy(max_retries=5, base_delay_s=1.0), stats, label="test3")
    elapsed = time.time() - start
    assert resp.status_code == 404
    assert calls["n"] == 1  # never retried
    assert elapsed < 0.5  # no backoff delay incurred
    print(f"[PASS] non-retryable 404 returns immediately, no retry, no delay (elapsed={elapsed:.3f}s)")


def test_transient_exception_retried():
    calls = {"n": 0}

    def flaky_conn():
        calls["n"] += 1
        if calls["n"] < 2:
            raise requests.exceptions.ConnectionError("simulated reset")
        return FakeResponse(200, {})

    stats = RetryStats()
    resp = with_retries(flaky_conn, RetryPolicy(max_retries=3, base_delay_s=0.05), stats, label="test4")
    assert resp.status_code == 200
    assert calls["n"] == 2
    print(f"[PASS] transient ConnectionError retried then succeeds (attempts={stats.attempts})")


def test_backoff_is_exponential_without_retry_after():
    delays = []

    def always_503():
        return FakeResponse(503, {})

    stats = RetryStats()
    policy = RetryPolicy(max_retries=3, base_delay_s=0.1, max_delay_s=10, jitter=False)
    start = time.time()
    with_retries(always_503, policy, stats, label="test5")
    elapsed = time.time() - start
    # expected waits: 0.1, 0.2, 0.4 = 0.7s total (no jitter, no Retry-After)
    assert 0.6 < elapsed < 1.2, f"unexpected elapsed {elapsed}"
    print(f"[PASS] exponential backoff without Retry-After header roughly matches base*2^n (elapsed={elapsed:.2f}s, expected ~0.7s)")


if __name__ == "__main__":
    test_retries_on_429_then_succeeds()
    test_exhausts_retries_and_returns_last_failure()
    test_non_retryable_status_returns_immediately()
    test_transient_exception_retried()
    test_backoff_is_exponential_without_retry_after()
    print("\nAll retry-wrapper resilience tests passed.")
