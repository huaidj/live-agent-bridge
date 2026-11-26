"""Clips created through the bridge must keep note dynamics: Live's API starts them at velocity_amount 0.0
(velocities ignored -> measured +2.9 dB on the same drum notes), the UI uses 1.0."""


async def test_arrangement_clip_gets_velocity_amount_one(client, live):
    r = await client.request("arrangement.create_midi_clip", {"track": 0, "start_time": 16.0, "length": 4.0,
                                                               "notes": [{"pitch": 36, "start_time": 0, "velocity": 40}]})
    clip = next(c for c in live.song.tracks[0].arrangement_clips if c.start_time == 16.0)
    assert r["notes_added"] == 1 and clip.velocity_amount == 1.0


async def test_session_clip_gets_velocity_amount_one(client, live):
    await client.request("clip.create", {"track": 1, "slot": 0, "length": 4})
    assert live.song.tracks[1].clip_slots[0].clip.velocity_amount == 1.0
