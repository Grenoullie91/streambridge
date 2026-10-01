"""Resolver: per-id locking, rate limiting, cache reuse, re-resolution.

The concurrency tests are the important ones: they assert that N concurrent
requests for one track cause exactly one upstream call, while requests for
different tracks do not serialise behind each other.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from conftest import FAKE_EXECUTABLE, INFO_PAYLOAD, FakeRunner
from streambridge.config import config_from_mapping
from streambridge.errors import RateLimitError, SourceUnavailableError, ValidationError
from streambridge.proc import CompletedRun
from streambridge.resolver import StreamResolver, TokenBucket
from streambridge.youtube import ExtractorClient

pytestmark = pytest.mark.unit

VALID_ID = "5NV6Rdv1a3I"
OTHER_ID = "CCHdMIEGaaM"


def _run(stdout: str = "", returncode: int = 0, stderr: str = "") -> CompletedRun:
    return CompletedRun((), returncode, stdout, stderr)


def build(overrides: dict | None = None) -> tuple[StreamResolver, FakeRunner]:
    config = config_from_mapping(
        {
            "timeouts": {"request": 5.0, "resolve": 5.0, "info": 5.0},
            "search": {"resolve_rate_per_min": 1000},
            **(overrides or {}),
        }
    )
    runner = FakeRunner()
    runner.add_json("watch?v=", INFO_PAYLOAD)
    client = ExtractorClient(config, runner=runner, executable=FAKE_EXECUTABLE)
    return StreamResolver(config, client), runner


class TestTokenBucket:
    def test_allows_up_to_limit(self) -> None:
        bucket = TokenBucket(3)
        for _ in range(3):
            bucket.check("test")

    def test_blocks_after_limit(self) -> None:
        bucket = TokenBucket(2)
        bucket.check("t")
        bucket.check("t")
        with pytest.raises(RateLimitError):
            bucket.check("t")

    def test_error_mentions_limit_and_hint(self) -> None:
        bucket = TokenBucket(1)
        bucket.check("search")
        with pytest.raises(RateLimitError) as exc:
            bucket.check("search")
        assert "1" in exc.value.message
        assert exc.value.hint is not None

    def test_window_slides(self) -> None:
        bucket = TokenBucket(2)
        bucket.check("t")
        bucket.check("t")
        with pytest.raises(RateLimitError):
            bucket.check("t")
        # Rewind the internal window instead of waiting a full minute.
        bucket._events.clear()
        bucket.check("t")

    def test_zero_limit_still_allows_one(self) -> None:
        # A misconfigured limit of 0 must not deadlock the service.
        assert TokenBucket(0).check("t") is None

    def test_thread_safe_under_contention(self) -> None:
        bucket = TokenBucket(50)
        unexpected: list[Exception] = []

        def worker() -> None:
            for _ in range(10):
                try:
                    bucket.check("t")
                except RateLimitError:
                    return
                except Exception as exc:  # pragma: no cover - failure path
                    unexpected.append(exc)
                    return

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not unexpected


class TestResolve:
    def test_resolves_via_client(self) -> None:
        resolver, _runner = build()
        info = resolver.resolve(VALID_ID)
        assert info.video_id == VALID_ID
        assert info.format_id == "140"

    def test_invalid_id_never_reaches_client(self) -> None:
        resolver, runner = build()
        with pytest.raises(ValidationError):
            resolver.resolve("../../etc/passwd")
        assert runner.calls == []

    def test_cached_second_call_skips_client(self) -> None:
        resolver, runner = build()
        resolver.resolve(VALID_ID)
        before = len(runner.calls)
        resolver.resolve(VALID_ID)
        assert len(runner.calls) == before

    def test_invalidate_forces_reresolve(self) -> None:
        resolver, runner = build()
        resolver.resolve(VALID_ID)
        before = len(runner.calls)
        resolver.invalidate(VALID_ID)
        resolver.resolve(VALID_ID)
        assert len(runner.calls) == before + 1

    def test_rate_limit_applies(self) -> None:
        resolver, _runner = build({"search": {"resolve_rate_per_min": 1}})
        resolver.resolve(VALID_ID, rate_limit=True)
        resolver.invalidate(VALID_ID)
        with pytest.raises(RateLimitError):
            resolver.resolve(OTHER_ID, rate_limit=True)

    def test_playback_is_not_rate_limited(self) -> None:
        # The continuous audio stream must never be throttled. Only the
        # explicit resolve step is limited, and callers can opt out.
        resolver, _runner = build({"search": {"resolve_rate_per_min": 1}})
        resolver.resolve(VALID_ID, rate_limit=True)
        resolver.invalidate(VALID_ID)
        resolver.resolve(VALID_ID, rate_limit=False)

    def test_upstream_failure_propagates(self) -> None:
        resolver, runner = build()
        # The failure must take precedence over the success rule set in build().
        runner.add_first("watch?v=", _run("", 1, "Video unavailable"))
        with pytest.raises(SourceUnavailableError):
            resolver.resolve(VALID_ID)

    def test_failure_does_not_poison_cache(self) -> None:
        resolver, runner = build()
        runner.add_first("watch?v=", _run("", 1, "Video unavailable"))
        with pytest.raises(SourceUnavailableError):
            resolver.resolve(VALID_ID)
        assert resolver._cache.get(VALID_ID) is None
        runner.clear_rules()
        runner.add_json("watch?v=", INFO_PAYLOAD)
        # A later attempt must succeed rather than serve a poisoned entry.
        assert resolver.resolve(VALID_ID).format_id == "140"

    def test_non_dict_json_rejected(self) -> None:
        resolver, runner = build()
        runner.add_first("watch?v=", _run('["not", "a", "dict"]'))
        with pytest.raises(SourceUnavailableError):
            resolver.resolve(VALID_ID)


class TestConcurrency:
    def test_parallel_same_id_calls_client_once(self) -> None:
        """The core guarantee: 12 concurrent requests, 1 upstream call."""
        resolver, runner = build()
        start = threading.Event()
        errors: list[Exception] = []
        results: list[str] = []

        def worker() -> None:
            start.wait()
            try:
                results.append(resolver.resolve(VALID_ID).format_id or "")
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for thread in threads:
            thread.start()
        start.set()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors
        assert len(results) == 12
        assert runner.count("watch?v=") == 1

    def test_different_ids_resolve_independently(self) -> None:
        resolver, _runner = build()
        results: list[str] = []
        lock = threading.Lock()
        errors: list[Exception] = []

        def worker(video_id: str) -> None:
            try:
                info = resolver.resolve(video_id, rate_limit=False)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)
                return
            with lock:
                results.append(info.video_id)

        ids = [f"{i:011d}" for i in range(6)]
        threads = [threading.Thread(target=worker, args=(v,)) for v in ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not errors
        assert sorted(results) == sorted(ids)

    def test_lock_table_stays_bounded(self) -> None:
        resolver, _runner = build()
        for i in range(600):
            resolver._lock_for(f"{i % 900 + 100:011d}")
        assert len(resolver._locks) <= 512

    def test_independent_ids_are_not_serialised(self) -> None:
        # A single global lock would serialise these 4 x 0.25s resolutions.
        resolver, runner = build()

        def slow_runner(args, *, timeout, stdin_data=None, env=None):  # type: ignore[no-untyped-def]
            runner.calls.append(list(args))
            if "watch?v=" in " ".join(args):
                time.sleep(0.25)
                return CompletedRun((), 0, json.dumps(INFO_PAYLOAD), "")
            return runner.default

        resolver._client._runner.run = slow_runner  # type: ignore[method-assign]
        ids = [f"{i:011d}" for i in range(4)]
        threads = [
            threading.Thread(target=lambda v=v: resolver.resolve(v, rate_limit=False)) for v in ids
        ]
        begin = time.monotonic()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        elapsed = time.monotonic() - begin
        # 4 x 0.25s serialised would be >= 1.0s; parallel is well under.
        assert elapsed < 0.8, f"resolution appears serialised ({elapsed:.2f}s)"


class TestStats:
    def test_stats_shape(self) -> None:
        resolver, _runner = build()
        resolver.resolve(VALID_ID)
        stats = resolver.stats()
        assert "entries" in stats
        assert stats["entries"] >= 1
