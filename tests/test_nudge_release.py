"""song_action nudge_up/nudge_down must release the held nudge: left at True Live keeps the transport fast
(2026-11-16: both stuck True after test runs, Live played ~186 BPM while showing tempo 111)."""

import asyncio


async def test_nudges_are_momentary(client, live):
    for act in ("nudge_up", "nudge_down"):
        r = await client.request("song.action", {"action": act})
        assert r["released"] == "next tick"
        for _ in range(40):
            if getattr(live.song, act, None) is False:
                break
            await asyncio.sleep(0.02)
        assert getattr(live.song, act) is False, act
