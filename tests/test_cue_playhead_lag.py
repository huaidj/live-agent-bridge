"""cue toggle with an explicit time must never add/remove a cue at a stale playhead.

Real Live applies a current_song_time write only on a later tick; set_or_delete_cue() in the
same tick then acts at the old position (seen on a real set: a stray cue at 594.5 and a cue
that was not removed). The op must refuse instead, and the server tool must retry.
"""

import pytest
from fake_live import CuePoint

from live_agent_bridge.client import LiveError


class LaggingSong:
    """Patch Song.current_song_time so writes land one 'tick' later (on the next read after a write)."""


@pytest.fixture
def lagging(live, monkeypatch):
    song = live.song
    cls = type(song)
    state = {"pending": None, "value": song.__dict__.get("current_song_time", 0.0), "reads_since_write": 0}

    def get(self):
        if state["pending"] is not None:
            state["reads_since_write"] += 1
            if state["reads_since_write"] > 2:   # applied only after the op returned
                state["value"], state["pending"] = state["pending"], None
        return state["value"]

    def set_(self, v):
        state["pending"], state["reads_since_write"] = float(v), 0

    monkeypatch.setattr(cls, "current_song_time", property(get, set_), raising=False)
    return state


async def test_toggle_with_time_refuses_when_playhead_has_not_moved(client, live, lagging):
    lagging["value"] = 100.0
    with pytest.raises(LiveError, match="playhead"):
        await client.request("cue", {"action": "toggle", "time": 16.0})
    assert [c.time for c in live.song.cue_points] == []      # nothing added at the stale 100.0


async def test_toggle_cue_landing_elsewhere_is_undone_and_retryable(client, live, monkeypatch):
    """Seen in Live 12.4.16 (2026-11-16): current_song_time read back 1.0, yet every toggle landed at 0.0537, the
    arrangement insert marker / selection start used while stopped. The op must remove the stray cue and say why
    (not a retryable playhead lag)."""
    song = live.song
    cls = type(song)
    engine = {"pos": 0.0537}

    def stale_toggle(self):
        for c in list(self.cue_points):
            if abs(c.time - engine["pos"]) < 1e-9:
                self.cue_points.remove(c)
                return
        self.cue_points.append(CuePoint(self, "", engine["pos"]))

    monkeypatch.setattr(cls, "set_or_delete_cue", stale_toggle, raising=False)
    song.current_song_time = 1.0                              # readback already says 1.0
    with pytest.raises(LiveError, match="insert marker"):
        await client.request("cue", {"action": "toggle", "time": 1.0})
    assert [c.time for c in live.song.cue_points] == []      # stray cue removed again


async def test_toggle_with_time_works_once_playhead_is_there(client, live):
    # a call that has to move the playhead only moves it (Live applies the write next tick) ...
    with pytest.raises(LiveError, match="playhead has not reached"):
        await client.request("cue", {"action": "toggle", "time": 32.0})
    assert live.song.cue_points == []
    # ... the retry, with the playhead already there, does not write again and toggles
    r = await client.request("cue", {"action": "toggle", "time": 32.0})
    assert r["added"] == [{"time": 32.0, "name": ""}]
    r = await client.request("cue", {"action": "toggle", "time": 32.0})
    assert r["removed"] == [{"time": 32.0, "name": ""}]
