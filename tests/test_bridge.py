import asyncio
import time

import pytest

from live_agent_bridge.client import LiveError


async def test_hello_and_ping(client, live):
    assert client.hello["live"] == "12.4.16"
    assert "batch" in client.hello["ops"]
    res = await client.request("ping")
    assert res["pong"] and res["tick_hz"] > 0


async def test_pipelined_requests_share_one_tick(client):
    t = time.perf_counter()
    res = await asyncio.gather(*[client.request("lom.get", {"path": "live_set", "props": ["tempo"]})
                                 for _ in range(100)])
    elapsed = time.perf_counter() - t
    assert all(r == {"tempo": 120.0} for r in res)
    assert elapsed < 0.35, elapsed  # ~1-2 ticks, not 100 ticks


async def test_overview_cached_fast_path(client):
    first = await client.request("overview")
    assert [t["name"] for t in first["tracks"]] == ["Drums", "Bass"]
    await asyncio.sleep(0.25)
    t = time.perf_counter()
    again = await client.request("overview")
    assert time.perf_counter() - t < 0.25  # served from the snapshot within one tick
    assert again["tempo"] == 120.0


async def test_overview_fresh_goes_to_main_thread(client):
    snap = await client.request("overview", {"fresh": True, "max_age": 0})
    assert snap["tempo"] == 120.0


async def test_snapshot_invalidated_by_writes(client):
    await client.request("overview")
    await client.request("song.set", {"tempo": 98})
    snap = await client.request("overview")
    assert snap["tempo"] == 98


async def test_lom_paths_and_refs(client, live):
    r = await client.request("lom.get", {"path": "live_set.tracks[1]", "props": ["name", "mixer_device"]})
    assert r["name"] == "Bass"
    ref = r["mixer_device"]["$ref"]
    assert r["mixer_device"]["path"] == "live_set tracks 1 mixer_device"
    v = await client.request("lom.get", {"path": ref + " volume", "props": ["value"]})
    assert v["value"] == pytest.approx(0.85)
    await client.request("lom.set", {"path": "track 0", "values": {"name": "Kit"}})
    assert live.song.tracks[0].name == "Kit"
    d = await client.request("lom.describe", {"path": "live_set"})
    assert "tempo" in d["observable"] and "create_midi_track" in d["methods"]
    with pytest.raises(LiveError, match="private"):
        await client.request("lom.get", {"path": "live_set", "props": ["__class__"]})
    with pytest.raises(LiveError, match="out of range"):
        await client.request("lom.get", {"path": "track 9"})


async def test_clip_with_notes_in_one_call(client, live):
    notes = [{"pitch": 36, "start_time": i, "duration": 0.25, "velocity": 110} for i in range(4)]
    r = await client.request("clip.create", {"track": "Drums", "slot": 0, "length": 4, "name": "Beat",
                                             "notes": notes})
    assert r["notes_added"] == 4
    got = await client.request("notes.get", {"track": 0, "slot": 0})
    assert got["count"] == 4 and got["notes"][0]["pitch"] == 36
    nid = got["notes"][0]["note_id"]
    await client.request("notes.modify", {"track": 0, "slot": 0, "changes": [{"note_id": nid, "velocity": 64}]})
    assert live.song.tracks[0].clip_slots[0].clip._notes[nid].velocity == 64
    await client.request("notes.replace", {"track": 0, "slot": 0, "notes": [{"pitch": 38, "start_time": 1}]})
    got = await client.request("notes.get", {"track": 0, "slot": 0})
    assert [n["pitch"] for n in got["notes"]] == [38]


async def test_clip_loop_order(client, live):
    await client.request("clip.create", {"track": 0, "slot": 1, "length": 4})
    await client.request("clip.set", {"track": 0, "slot": 1, "loop_start": 8, "loop_end": 12})
    c = live.song.tracks[0].clip_slots[1].clip
    assert (c.loop_start, c.loop_end) == (8, 12)
    await client.request("clip.set", {"track": 0, "slot": 1, "loop_start": 0, "loop_end": 2})
    assert (c.loop_start, c.loop_end) == (0, 2)


async def test_batch_single_tick_and_undo_group(client, live):
    ops = [{"op": "track.create", "args": {"kind": "midi", "name": "Lead"}},
           {"op": "clip.create", "args": {"track": "Lead", "slot": 0, "length": 8}},
           {"op": "track.set", "args": {"track": "Lead", "volume": "-6 dB", "mute": True}},
           {"op": "nope"}]
    r = await client.request("batch", {"ops": ops})
    assert r["completed"] == 3 and r["results"][3]["error"]["type"] == "UnknownOp"
    t = live.song.tracks[-1]
    assert t.name == "Lead" and t.mute and t.clip_slots[0].has_clip
    assert str(t.mixer_device.volume).startswith("-6.0")
    assert live.song.undo_steps == 1


async def test_device_params_by_display(client, live):
    await client.request("browser.load", {"query": "drift", "track": 1})
    assert live.song.tracks[1].devices[-1].name == "Drift"
    r = await client.request("device.set_params", {"track": 1, "index": 0, "params": [
        {"name": "filter", "display": "632 Hz"}, {"name": "Device On", "display": "off"}]})
    assert r[0]["display"] == "632 Hz" and r[1]["value"] == 0


async def test_browser_search_is_time_sliced_and_cached(client, live):
    r = await client.request("browser.search", {"query": "bass pluck"})
    assert r["results"][0]["name"] == "Bass Pluck.adv"
    r = await client.request("browser.search", {"query": "kick", "categories": ["user_folders"]})
    assert r["results"][0]["path"] == "samplepack/Drums/Kick 01.wav"


async def test_observe_coalesces(client, live):
    sub = await client.request("observe", {"path": "live_set", "prop": "tempo"})
    for v in (100, 101, 102):
        live.song.tempo = v  # three changes within one tick
    await asyncio.sleep(0.25)
    ev = client.take_events()
    assert len(ev) == 1 and ev[0]["value"] == 102 and ev[0]["sub"] == sub["sub"]
    await client.request("unobserve", {"sub": sub["sub"]})
    assert not live.song._listeners["tempo"]


async def test_stall_reports_busy_and_never_executes_late(client, live, monkeypatch):
    import live_agent_bridge.client as cmod
    monkeypatch.setattr(cmod, "START_DEADLINE", 0.4)
    await asyncio.sleep(0.6)  # let a heartbeat pass
    live.paused = True  # modal dialog: main thread stops ticking
    await asyncio.sleep(0.25)  # let an in-flight harness tick finish
    t = time.perf_counter()
    with pytest.raises(LiveError, match="LiveBusy"):
        await client.request("song.set", {"tempo": 77})
    assert time.perf_counter() - t < 1.5
    live.paused = False  # dialog closed: the stale write must be dropped, not applied
    await asyncio.sleep(0.4)
    assert live.song.tempo == 120.0


async def test_bad_args_reported(client):
    with pytest.raises(LiveError, match="BadArgs"):
        await client.request("clip.create", {"track": 0})


async def test_long_write_is_not_misreported_as_busy(client, live, monkeypatch):
    from AgentBridge import ops

    import live_agent_bridge.client as cmod
    monkeypatch.setattr(cmod, "START_DEADLINE", 0.3)
    orig = ops.OPS["song.set"]

    def slow_set(ctx, **props):  # e.g. a track whose template loads a heavy plug-in
        time.sleep(0.9)
        return orig(ctx, **props)
    slow_set.mutates = True
    monkeypatch.setitem(ops.OPS, "song.set", slow_set)
    r = await client.request("song.set", {"tempo": 90})
    assert r == {"tempo": 90} and live.song.tempo == 90


async def test_connection_loss_fails_pending(live):
    from live_agent_bridge.client import LiveClient
    c = LiveClient("127.0.0.1", live.port)
    await c.connect()
    live.paused = True
    fut = asyncio.ensure_future(c.request("lom.get", {"path": "live_set", "props": ["tempo"]}))
    await asyncio.sleep(0.05)
    for cl in list(live.core.server.clients.values()):
        cl.close()
    with pytest.raises(LiveError, match="ConnectionLost"):
        await fut
    live.paused = False
    assert (await c.request("ping"))["pong"]  # reconnects transparently
    await c.close()


async def test_loading_happens_in_background(client, live):
    live.song.view.selected_track = live.song.tracks[0]
    await client.request("browser.load", {"query": "drift", "track": 1})
    assert live.song.tracks[1].devices[-1].name == "Drift"
    assert live.song.view.selected_track is live.song.tracks[0]  # user's selection untouched


async def test_selection_guard_never_blocks_a_tick(client, live):
    import time as _t
    await client.request("track.create", {"kind": "midi", "name": "Guard"})
    live.paused = True
    await asyncio.sleep(0.25)  # let the harness thread finish its tick: Live itself is single-threaded
    worst = 0.0
    for _ in range(10):  # ticks during the guard window must stay short
        t = _t.perf_counter()
        live.core.tick()
        worst = max(worst, _t.perf_counter() - t)
    live.paused = False
    assert worst < 0.05, worst
