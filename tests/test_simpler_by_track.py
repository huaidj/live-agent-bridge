"""simpler with only `track` uses that track's first Simpler (seen 2026-11-16: the tool refused a track-only call)."""

import pytest
from fake_live import Obj

from live_agent_bridge.client import LiveError


class FakeSimpler(Obj):
    class_name = "OriginalSimpler"

    def __init__(self, parent):
        Obj.__init__(self, parent)
        self.name = "Tribal Loop"
        self.playback_mode = 2
        self.sample = None
        self.parameters = []


class Other(Obj):
    class_name = "Reverb"

    def __init__(self, parent):
        Obj.__init__(self, parent)
        self.name = "Reverb"
        self.parameters = []


async def test_simpler_track_only_finds_the_simpler(client, live):
    t = live.song.tracks[0]
    t.devices.extend([Other(t), FakeSimpler(t)])
    r = await client.request("simpler", {"action": "info", "track": 0})
    assert r["name"] == "Tribal Loop" and r["playback_mode"] == 2


async def test_simpler_track_without_simpler_says_so(client, live):
    with pytest.raises(LiveError, match="no Simpler on track"):
        await client.request("simpler", {"action": "info", "track": 1})
