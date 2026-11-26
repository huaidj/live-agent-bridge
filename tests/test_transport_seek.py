"""seek while stopped must also move Song.start_time.

Real Live starts playback from start_time (the insert marker) when stopped, so a seek that only
set current_song_time made the next play/record start elsewhere (a resample began at beat 0).
"""


async def test_seek_while_stopped_moves_start_marker(client, live):
    live.song.is_playing = False
    live.song.start_time = 0.0
    r = await client.request("transport", {"action": "seek", "value": 572.0})
    assert r["current_song_time"] == 572.0
    assert live.song.start_time == 572.0


async def test_seek_while_playing_leaves_start_marker(client, live):
    live.song.is_playing = True
    live.song.start_time = 16.0
    await client.request("transport", {"action": "seek", "value": 64.0})
    assert live.song.current_song_time == 64.0
    assert live.song.start_time == 16.0
