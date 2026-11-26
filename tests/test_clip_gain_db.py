"""Audio clip level in dB. Raw Clip.gain is 0.4 = 0 dB, 1.0 = +24 dB: writing 1.0 'for unity' boosted ~140 clips by
24 dB on 2026-11-16 (accidental, by a mix script)."""

import pytest

from live_agent_bridge.client import LiveError


async def _audio_clip(client, live):
    r = await client.request("arrangement.create_audio_clip", {"track": 0, "file_path": "x.wav", "start_time": 0.0})
    return r["$ref"], next(c for c in live.song.tracks[0].arrangement_clips if c.file_path == "x.wav")


@pytest.mark.parametrize("db", [0.0, -3.1, -12.0, 6.0, 24.0])
async def test_gain_db_hits_the_requested_level(client, live, db):
    ref, clip = await _audio_clip(client, live)
    r = await client.request("clip.set", {"clip": ref, "gain_db": db})
    shown = float(r["gain_display_string"].replace("dB", ""))
    assert shown == pytest.approx(db, abs=0.05), r


async def test_raw_gain_one_is_plus_24_not_unity(client, live):
    ref, clip = await _audio_clip(client, live)
    assert clip.gain == pytest.approx(0.4)                       # default = 0 dB
    r = await client.request("clip.set", {"clip": ref, "gain": 1.0})
    assert r["gain"] == 1.0 and clip.gain_display_string.startswith("24.00")   # the trap, documented by this test


async def test_gain_db_rejects_midi_and_out_of_range(client, live):
    ref, clip = await _audio_clip(client, live)
    with pytest.raises(LiveError, match="between -69 and"):
        await client.request("clip.set", {"clip": ref, "gain_db": 40})
    m = await client.request("arrangement.create_midi_clip", {"track": 0, "start_time": 32.0, "length": 4.0})
    with pytest.raises(LiveError, match="audio clips"):
        await client.request("clip.set", {"clip": m["$ref"], "gain_db": -3})
