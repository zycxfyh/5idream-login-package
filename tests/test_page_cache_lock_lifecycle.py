import asyncio

from app.cache import PageCache


def test_cleanup_does_not_replace_key_lock_while_waiter_exists() -> None:
    async def scenario() -> None:
        cache = PageCache(ttl=60)
        key = cache.key_for('user-1', 'activity', 'join', 1, 10)
        owner_lock = cache.lock_for(key)
        await owner_lock.acquire()

        waiter_started = asyncio.Event()

        async def waiter() -> None:
            lock = cache.lock_for(key)
            assert lock is owner_lock
            waiter_started.set()
            async with lock:
                pass

        waiter_task = asyncio.create_task(waiter())
        await waiter_started.wait()
        await asyncio.sleep(0)

        owner_lock.release()
        assert not owner_lock.locked()

        cache.cleanup()
        same_key_lock = cache.lock_for(key)
        assert same_key_lock is owner_lock

        await waiter_task

    asyncio.run(scenario())


def test_idle_key_lock_is_released_when_no_request_references_it() -> None:
    import gc
    import weakref

    cache = PageCache(ttl=60)
    key = cache.key_for('user-1', 'activity', 'join', 1, 10)
    lock = cache.lock_for(key)
    ref = weakref.ref(lock)
    assert key in cache._locks

    del lock
    gc.collect()

    assert ref() is None
    assert key not in cache._locks
