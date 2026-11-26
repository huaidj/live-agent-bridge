"""Moving a long loop later must not trip Live's per-write bound check
("Cannot set the Loopstart behind the Songlength", seen in Live 12.4.16 on 2026-11-16)."""

import pytest
from AgentBridge.lom import ordered_props


class StrictSong:
    song_length = 1024.0

    def __init__(self, start, length):
        object.__setattr__(self, "loop_start", start)
        object.__setattr__(self, "loop_length", length)

    def __setattr__(self, k, v):
        s = v if k == "loop_start" else self.loop_start
        n = v if k == "loop_length" else self.loop_length
        if s + n > self.song_length:
            raise RuntimeError("Cannot set the Loopstart behind the Songlength")
        object.__setattr__(self, k, v)


@pytest.mark.parametrize("start,length,new_start,new_length", [
    (0.0, 1024.0, 896.0, 64.0),    # long loop -> short late loop (the real failure)
    (896.0, 64.0, 0.0, 1024.0),    # and back (restore)
    (100.0, 200.0, 50.0, 900.0),
])
def test_loop_moves_never_leave_the_song(start, length, new_start, new_length):
    song = StrictSong(start, length)
    for k, v in ordered_props(song, {"loop_start": new_start, "loop_length": new_length, "loop": True}):
        if k != "loop":
            setattr(song, k, v)
    assert (song.loop_start, song.loop_length) == (new_start, new_length)


def test_naive_order_really_fails():
    with pytest.raises(RuntimeError):
        StrictSong(0.0, 1024.0).loop_start = 896.0
