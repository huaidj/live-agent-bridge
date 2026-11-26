"""Live 12.4.15 per-clip scale: root_note / scale_name / scale_mode writable, scale_intervals read-only."""

import pytest

from live_agent_bridge.client import LiveError


async def test_clip_scale_read_and_write(client, live):
    await client.request("clip.create", {"track": 0, "slot": 0, "length": 4})
    info = await client.request("clip.info", {"track": 0, "slot": 0})
    assert info["root_note"] == 0 and info["scale_name"] == "Major" and info["scale_mode"] is False
    assert list(info["scale_intervals"]) == [0, 2, 4, 5, 7, 9, 11]

    r = await client.request("clip.set", {"track": 0, "slot": 0, "root_note": 9, "scale_name": "Minor",
                                          "scale_mode": True})
    assert r == {"root_note": 9, "scale_name": "Minor", "scale_mode": True}
    info = await client.request("clip.info", {"track": 0, "slot": 0})
    assert list(info["scale_intervals"]) == [0, 2, 3, 5, 7, 8, 10]


async def test_clip_scale_intervals_not_settable(client):
    await client.request("clip.create", {"track": 0, "slot": 0, "length": 4})
    with pytest.raises(LiveError, match="cannot be set"):
        await client.request("clip.set", {"track": 0, "slot": 0, "scale_intervals": [0, 1]})
