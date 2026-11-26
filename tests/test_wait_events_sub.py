"""poll_events(wait, sub=X) must keep waiting while only other subscriptions have events queued."""

import asyncio
import time

from live_agent_bridge.client import LiveClient


def _push(c: LiveClient, msg: dict) -> None:
    c.events.append(msg)
    waiters, c._event_waiters = c._event_waiters, []
    for w in waiters:
        if not w.done():
            w.set_result(None)


def test_other_subscription_does_not_end_wait():
    async def run():
        c = LiveClient()
        _push(c, {"event": "change", "sub": 1, "value": 0})
        t0 = time.perf_counter()
        await c.wait_events(0.3, sub=2)
        waited = time.perf_counter() - t0
        assert waited >= 0.25  # did not return early on sub 1's event
        assert c.take_events(sub=2) == []
        assert len(c.take_events(sub=1)) == 1

    asyncio.run(run())


def test_matching_event_wakes_waiter():
    async def run():
        c = LiveClient()
        _push(c, {"event": "change", "sub": 1, "value": 0})
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, _push, c, {"event": "change", "sub": 1, "value": 1})
        loop.call_later(0.02, _push, c, {"event": "change", "sub": 3, "value": 9})
        c.take_events(sub=1)  # drain the old sub-1 event
        t0 = time.perf_counter()
        await c.wait_events(2.0, sub=1)
        assert time.perf_counter() - t0 < 1.0
        assert [e["value"] for e in c.take_events(sub=1)] == [1]

    asyncio.run(run())


def test_unfiltered_wait_returns_on_any_event():
    async def run():
        c = LiveClient()
        _push(c, {"event": "change", "sub": 5})
        t0 = time.perf_counter()
        await c.wait_events(1.0)
        assert time.perf_counter() - t0 < 0.1

    asyncio.run(run())
