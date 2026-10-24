"""Rule Judge 证据读取批次缓存的边界测试。"""

import asyncio

import pytest

from services.api.app.infrastructure.rule_judge_evidence_read_batch import (
    EvidenceReadBatchCache,
)

pytestmark = pytest.mark.asyncio


KEY = ("/evidence/page.png", "sha256:page", 7)


async def test_read_without_scope_loads_and_validates_every_time() -> None:
    cache = EvidenceReadBatchCache()
    load_calls = 0
    validate_calls = 0

    async def load() -> bytes:
        nonlocal load_calls
        load_calls += 1
        return b"payload"

    def validate(payload: bytes) -> None:
        nonlocal validate_calls
        validate_calls += 1
        assert payload == b"payload"

    assert await cache.read(key=KEY, load=load, validate=validate) == b"payload"
    assert await cache.read(key=KEY, load=load, validate=validate) == b"payload"
    assert load_calls == 2
    assert validate_calls == 2


async def test_scope_reuses_successful_bytes_but_validates_each_read() -> None:
    cache = EvidenceReadBatchCache()
    load_calls = 0
    validate_payloads: list[bytes] = []

    async def load() -> bytes:
        nonlocal load_calls
        load_calls += 1
        return b"payload"

    def validate(payload: bytes) -> None:
        validate_payloads.append(payload)

    with cache.scope():
        assert await cache.read(key=KEY, load=load, validate=validate) == b"payload"
        assert await cache.read(key=KEY, load=load, validate=validate) == b"payload"

    assert load_calls == 1
    assert validate_payloads == [b"payload", b"payload"]


async def test_scope_exit_destroys_cached_entries() -> None:
    cache = EvidenceReadBatchCache()
    load_calls = 0

    async def load() -> bytes:
        nonlocal load_calls
        load_calls += 1
        return b"payload"

    with cache.scope():
        await cache.read(key=KEY, load=load, validate=lambda _: None)
    with cache.scope():
        await cache.read(key=KEY, load=load, validate=lambda _: None)

    assert load_calls == 2


async def test_scope_exception_destroys_cached_entries() -> None:
    cache = EvidenceReadBatchCache()
    load_calls = 0

    async def load() -> bytes:
        nonlocal load_calls
        load_calls += 1
        return b"payload"

    with pytest.raises(RuntimeError, match="scope failed"):
        with cache.scope():
            await cache.read(key=KEY, load=load, validate=lambda _: None)
            raise RuntimeError("scope failed")

    with cache.scope():
        await cache.read(key=KEY, load=load, validate=lambda _: None)

    assert load_calls == 2


async def test_nested_scopes_are_independent_and_restore_outer_scope() -> None:
    cache = EvidenceReadBatchCache()
    load_calls = 0
    validate_calls = 0

    async def load() -> bytes:
        nonlocal load_calls
        load_calls += 1
        return b"payload"

    def validate(_: bytes) -> None:
        nonlocal validate_calls
        validate_calls += 1

    with cache.scope():
        await cache.read(key=KEY, load=load, validate=validate)
        with cache.scope():
            await cache.read(key=KEY, load=load, validate=validate)
            await cache.read(key=KEY, load=load, validate=validate)
        await cache.read(key=KEY, load=load, validate=validate)

    assert load_calls == 2
    assert validate_calls == 4


async def test_concurrent_scopes_do_not_share_entries() -> None:
    cache = EvidenceReadBatchCache()
    load_calls = 0
    first_scope_loaded = asyncio.Event()
    release_first_scope = asyncio.Event()

    async def load() -> bytes:
        nonlocal load_calls
        load_calls += 1
        return b"payload"

    async def first_worker() -> bytes:
        with cache.scope():
            result = await cache.read(key=KEY, load=load, validate=lambda _: None)
            first_scope_loaded.set()
            await release_first_scope.wait()
            return result

    async def second_worker() -> bytes:
        await first_scope_loaded.wait()
        with cache.scope():
            result = await cache.read(key=KEY, load=load, validate=lambda _: None)
        release_first_scope.set()
        return result

    assert await asyncio.gather(first_worker(), second_worker()) == [b"payload", b"payload"]
    assert load_calls == 2


@pytest.mark.parametrize(
    ("first_key", "second_key"),
    [
        (KEY, ("/evidence/other.png", "sha256:page", 7)),
        (KEY, ("/evidence/page.png", "sha256:other", 7)),
        (KEY, ("/evidence/page.png", "sha256:page", 8)),
    ],
)
async def test_cache_key_includes_path_checksum_and_size(first_key, second_key) -> None:
    cache = EvidenceReadBatchCache()
    load_calls = 0

    async def load() -> bytes:
        nonlocal load_calls
        load_calls += 1
        return b"payload"

    with cache.scope():
        await cache.read(key=first_key, load=load, validate=lambda _: None)
        await cache.read(key=second_key, load=load, validate=lambda _: None)

    assert load_calls == 2


@pytest.mark.parametrize("failure", ["load", "validate"])
async def test_load_or_validation_failure_is_not_cached(failure) -> None:
    cache = EvidenceReadBatchCache()
    load_calls = 0
    validate_calls = 0

    async def load() -> bytes:
        nonlocal load_calls
        load_calls += 1
        if failure == "load" and load_calls == 1:
            raise RuntimeError("load failed")
        return b"payload"

    def validate(_: bytes) -> None:
        nonlocal validate_calls
        validate_calls += 1
        if failure == "validate" and validate_calls == 1:
            raise ValueError("validation failed")

    with cache.scope():
        with pytest.raises((RuntimeError, ValueError)):
            await cache.read(key=KEY, load=load, validate=validate)
        assert await cache.read(key=KEY, load=load, validate=validate) == b"payload"

    assert load_calls == 2
    assert validate_calls == (1 if failure == "load" else 2)


async def test_cached_entry_is_removed_when_revalidation_fails() -> None:
    cache = EvidenceReadBatchCache()
    load_calls = 0
    validate_calls = 0

    async def load() -> bytes:
        nonlocal load_calls
        load_calls += 1
        return b"payload"

    def validate(_: bytes) -> None:
        nonlocal validate_calls
        validate_calls += 1
        if validate_calls == 2:
            raise ValueError("revalidation failed")

    with cache.scope():
        await cache.read(key=KEY, load=load, validate=validate)
        with pytest.raises(ValueError, match="revalidation failed"):
            await cache.read(key=KEY, load=load, validate=validate)
        assert await cache.read(key=KEY, load=load, validate=validate) == b"payload"

    assert load_calls == 2
    assert validate_calls == 3


async def test_cancelled_load_is_not_cached_and_can_be_retried() -> None:
    cache = EvidenceReadBatchCache()
    load_calls = 0

    async def load() -> bytes:
        nonlocal load_calls
        load_calls += 1
        if load_calls == 1:
            raise asyncio.CancelledError
        return b"payload"

    with cache.scope():
        with pytest.raises(asyncio.CancelledError):
            await cache.read(key=KEY, load=load, validate=lambda _: None)
        assert await cache.read(key=KEY, load=load, validate=lambda _: None) == b"payload"

    assert load_calls == 2


async def test_entry_over_cap_is_not_cached() -> None:
    cache = EvidenceReadBatchCache(max_bytes=4)
    load_calls = 0

    async def load() -> bytes:
        nonlocal load_calls
        load_calls += 1
        return b"abc"

    first_key = ("/evidence/first.png", "sha256:first", 3)
    second_key = ("/evidence/second.png", "sha256:second", 3)
    with cache.scope():
        await cache.read(key=first_key, load=load, validate=lambda _: None)
        await cache.read(key=second_key, load=load, validate=lambda _: None)
        await cache.read(key=second_key, load=load, validate=lambda _: None)
        await cache.read(key=first_key, load=load, validate=lambda _: None)

    assert load_calls == 3
