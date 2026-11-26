import types

import pytest

from live_agent_bridge.client import LiveError


async def test_insert_and_move_native_device(client, live):
    r = await client.request("device.insert", {"name": "Reverb", "track": 0})
    assert r["inserted"] == "Reverb" and r["devices"] == ["Reverb"]
    await client.request("device.move", {"device": r["ref"]["$ref"], "target": 1})
    assert [d.name for d in live.song.tracks[1].devices] == ["Reverb"] and not live.song.tracks[0].devices


async def test_clip_edit_and_automation(client, live):
    await client.request("clip.create", {"track": 0, "slot": 0, "length": 4})
    await client.request("clip.edit", {"action": "quantize", "track": 0, "slot": 0, "grid": 7, "amount": 0.5})
    clip = live.song.tracks[0].clip_slots[0].clip
    assert clip.quantized == (7, 0.5)
    await client.request("clip.edit", {"action": "duplicate_loop", "track": 0, "slot": 0})
    assert clip.length == 8
    pv = "track 0 mixer_device volume"
    w = await client.request("automation", {"action": "write", "parameter": pv, "track": 0, "slot": 0,
                                            "points": [{"time": 0, "value": 0.2}, {"time": 2, "value": 0.9}]})
    assert w["written"] == 2
    r = await client.request("automation", {"action": "read", "parameter": pv, "track": 0, "slot": 0,
                                            "times": [1, 3]})
    assert [x["value"] for x in r["values"]] == [0.2, 0.9]


async def test_automation_on_arrangement_clip_explains_workaround(client, live):
    c = await client.request("arrangement.create_midi_clip", {"track": 0, "start_time": 8.0, "length": 4.0})
    with pytest.raises(LiveError, match="duplicate_from_session"):
        await client.request("automation", {"action": "write", "parameter": "track 0 mixer_device volume",
                                            "clip": c["$ref"], "points": [{"time": 0, "value": 0.5}]})


async def test_routing_by_name(client, live):
    r = await client.request("routing", {"track": 1, "output_type": "sends"})
    assert r["output"]["type"] == "Sends Only" and "Master" in r["output"]["types"]
    with pytest.raises(LiveError, match="no routing"):
        await client.request("routing", {"track": 1, "input_type": "nope"})


async def test_rack_ops(client, live):
    await client.request("device.insert", {"name": "Drum Rack", "track": 0})
    r = await client.request("rack", {"action": "store_variation", "track": 0, "index": 0})
    assert r["variation_count"] == 1 and len(r["macros"]) == 8
    r = await client.request("rack", {"action": "add_macro", "track": 0, "index": 0})
    assert len(r["macros"]) == 9
    live.song.tracks[0].devices[0].drum_pads[36].chains = [types.SimpleNamespace(name="Kick")]
    r = await client.request("rack", {"action": "copy_pad", "track": 0, "index": 0, "source": 36,
                                      "destination": 37})
    assert {p["note"] for p in r["pads"]} == {36, 37}


async def test_cues_memory_app(client, live):
    live.song.current_song_time = 16.0
    r = await client.request("cue", {"action": "toggle"})
    assert r["added"] == [{"time": 16.0, "name": ""}] and r["items"] == [{"index": 0, "name": "", "time": 16.0}]
    await client.request("cue", {"action": "rename", "index": 0, "name": "Drop"})
    assert live.song.cue_points[0].name == "Drop"
    await client.request("set_data", {"key": "plan", "value": {"sections": ["intro", "drop"]}})
    assert (await client.request("get_data", {"key": "plan"}))["value"] == {"sections": ["intro", "drop"]}
    info = await client.request("app", {"action": "info"})
    assert info["version"] == "12.4.16" and info["average_process_usage"] == 0.12


async def test_missing_capability_is_clear(client):
    with pytest.raises(LiveError, match="not available in this Live version"):
        await client.request("take_lanes", {"action": "list", "track": 0})


async def test_audit_reports_against_catalog(client):
    r = await client.request("lom.audit", {})
    assert r["objects_walked"] > 10
    song = r["classes"]["Song"]
    assert song["present"] > 20 and "capture_midi" in song["missing"]
    assert r["classes"]["LooperDevice"]["status"].startswith("not in")
