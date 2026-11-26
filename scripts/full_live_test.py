"""Exhaustive real-Live test of every MCP tool, through the registered stdio server.

Works inside the user's open set: everything is created on tracks/scenes named 'ZZ-TEST ...'
and deleted by name afterwards; global song/view/arm/transport state is saved first and
restored at the end. Prints PASS / EXPECTED (documented refusal) / FAIL per call and a
coverage list of tools never exercised.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from fixture_audio import acceptance_audio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXE = str(Path(sys.executable).with_name("live-agent-bridge.exe" if os.name == "nt" else "live-agent-bridge"))
FIXTURE = ""
SONG_PROPS = ["tempo", "signature_numerator", "signature_denominator", "metronome", "loop", "loop_start",
              "loop_length", "current_song_time", "record_mode", "session_record", "punch_in", "punch_out",
              "arrangement_overdub", "session_automation_record", "swing_amount", "groove_amount",
              "clip_trigger_quantization", "root_note", "scale_name", "scale_mode", "back_to_arranger"]
A, B, AU = "ZZ-TEST MIDI A", "ZZ-TEST MIDI B", "ZZ-TEST Audio"

rows, used = [], set()
PAIRS: dict = {}  # (tool, action) -> number of calls, for the coverage report
INNER_OPS: dict = {}  # ops sent inside batch calls (the bridge counts only the batch op itself)
PARK, PARK_SAFE = 896.0, False  # recomputed from the open set in run_all()
LAST = {"tool": None, "label": None}


class Runner:
    def __init__(self, s):
        self.s = s

    async def __call__(self, label, tool, expect_error=None, check=None, **args):
        used.add(tool)
        LAST.update(tool=tool, label=label)
        key = (tool, args.get("action"))
        PAIRS[key] = PAIRS.get(key, 0) + 1
        if tool == "batch":
            for sub in args.get("ops") or []:
                INNER_OPS[sub.get("op")] = INNER_OPS.get(sub.get("op"), 0) + 1
        t = time.perf_counter()
        try:
            r = await asyncio.wait_for(self.s.call_tool(tool, args), 240)
        except Exception as e:  # transport-level failure
            rows.append(("FAIL", tool, label, 0, f"{type(e).__name__}: {e}"))
            print(f"FAIL  {tool:22} {label}: {e}")
            return None
        ms = (time.perf_counter() - t) * 1000
        text = r.content[0].text if len(r.content) == 1 else (
            "[" + ",".join(c.text for c in r.content) + "]" if r.content else "")
        try:
            data = json.loads(text) if text[:1] in "[{" else text
        except json.JSONDecodeError:
            data = text
        if r.isError:
            if expect_error is not None and expect_error.lower() in text.lower():  # "" = any documented refusal
                rows.append(("EXPECTED", tool, label, ms, text[:160]))
                print(f"EXPT  {tool:22} {ms:7.0f} ms  {label}: {text[:110]}")
                return None
            rows.append(("FAIL", tool, label, ms, text[:300]))
            print(f"FAIL  {tool:22} {ms:7.0f} ms  {label}\n        {text[:300]}")
            return None
        note = ""
        if check is not None:
            try:
                ok = check(data)
            except Exception as e:  # noqa: BLE001
                ok, note = False, f"check raised {e!r}"
            if not ok:
                rows.append(("FAIL", tool, label, ms, "check failed: " + (note or str(data)[:250])))
                print(f"FAIL  {tool:22} {ms:7.0f} ms  {label} (check)\n        {note or str(data)[:250]}")
                return data
        rows.append(("PASS", tool, label, ms, ""))
        print(f"PASS  {tool:22} {ms:7.0f} ms  {label}")
        return data


async def user_clip_signature(s):
    """Every arrangement clip of every existing track, so the run can prove it touched nothing."""
    sig = set()
    sess = json.loads("".join(c.text for c in (await s.call_tool("get_session", {})).content))
    for t in sess.get("tracks", []):
        r = await s.call_tool("arrangement", {"action": "list", "track": t["index"]})
        if r.isError:
            continue
        try:
            clips = json.loads("".join(c.text for c in r.content))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(clips, dict):
            clips = clips.get("clips") or clips.get("items") or []
        for c in clips or []:
            sig.add((t["name"], c.get("name"), round(float(c.get("start_time", 0)), 3),
                     round(float(c.get("end_time", 0)), 3), bool(c.get("muted"))))
    return sig


async def main():
    global FIXTURE
    if os.environ.get("ZZ_TEST_ALLOW_USER_SET") != "1":
        print("REFUSING to run the destructive suite on the user's open set.\n"
              "  Run it on a copy of the .als (recommended), or export ZZ_TEST_ALLOW_USER_SET=1 to accept\n"
              "  that this run may temporarily alter user clips (the user-objects check will report it).")
        return
    output_dir = Path(sys.argv[1]).resolve().parent if len(sys.argv) > 1 else Path.cwd()
    FIXTURE = acceptance_audio(os.environ.get("ZZ_TEST_FIXTURE_WAV"), output_dir)
    # MCP stdio only inherits a small allowlist by default. Keep the server's
    # test database and the in-process verification/cleanup in the same scope.
    scoped_env = {key: os.environ[key] for key in (
        "AGENT_BRIDGE_DATA_DIR", "AGENT_BRIDGE_HOST", "AGENT_BRIDGE_PORT", "ABLETON_MAX_PORT",
        "AGENT_BRIDGE_CLAP_MODEL", "AGENT_BRIDGE_AUDIO_PYTHON", "TEMP", "TMP", "PYTHONDONTWRITEBYTECODE",
    ) if key in os.environ}
    p = StdioServerParameters(command=EXE, args=["serve"], env=scoped_env)
    async with stdio_client(p) as (rd, wr), ClientSession(rd, wr) as s:
        await s.initialize()
        all_tools = {t.name for t in (await s.list_tools()).tools}
        T = Runner(s)
        print(f"MCP initialized: {len(all_tools)} tools\n")

        if expected_path := os.environ.get("ZZ_TEST_FIXTURE_FILE"):
            actual = await T("verify disposable fixture", "lom_get", path="live_set",
                             props=["file_path", "is_playing", "record_mode", "session_record"])
            if (not actual or not actual.get("file_path")
                    or Path(actual["file_path"]).resolve() != Path(expected_path).resolve()
                    or any(actual.get(flag) is not False for flag in ("is_playing", "record_mode", "session_record"))):
                raise RuntimeError("The exact stopped acceptance fixture is not open")

        # ------------------------------------------------ snapshot user state
        song0 = await T("save song state", "lom_get", path="live_set", props=SONG_PROPS)
        view0 = await T("save view state", "lom_get", path="live_set view",
                        props=["selected_track", "selected_scene", "follow_song", "draw_mode"])
        sess0 = await T("overview", "get_session", fresh=True)
        user_sig0 = await user_clip_signature(s)
        print(f"user clips snapshotted: {len(user_sig0)}")

        # live probe: which tool call is the first one that fragments a user clip?
        probe_idx = next((t["index"] for t in sess0["tracks"] if t["name"] == "TRIBE · Drums"), None)

        async def probe_sig():
            r = await s.call_tool("arrangement", {"action": "list", "track": probe_idx})
            if r.isError:
                return None
            try:
                data = json.loads("".join(c.text for c in r.content))
            except Exception:  # noqa: BLE001
                return None
            items = data.get("items") if isinstance(data, dict) else data
            return tuple(sorted((round(float(c.get("start_time", 0)), 3), round(float(c.get("end_time", 0)), 3))
                                for c in (items or []) if float(c.get("start_time", 0)) < 2.0))

        async def watch():
            sig = await probe_sig()
            while True:
                await asyncio.sleep(0.25)
                now = await probe_sig()
                if now is not None and sig is not None and now != sig:
                    print(f"\n!! USER-DAMAGE right after: {LAST['tool']} :: {LAST['label']}  {sig} -> {now}")
                    rows.append(("FAIL", LAST["tool"] or "-", f"{LAST['label']} [USER-DAMAGE]", 0,
                                 f"{sig} -> {now}"))
                    sig = now

        watcher = asyncio.create_task(watch()) if probe_idx is not None else None
        arms0 = {t["index"]: t.get("arm") for t in sess0["tracks"] if t.get("arm") is not None}
        n_scenes0 = len(sess0["scenes"])
        sel0 = (view0["selected_track"] or {}).get("path")
        try:
            await run_all(T, sess0, n_scenes0)
        finally:
            print("\n---- cleanup / restore")
            if watcher:
                watcher.cancel()
            await T("stop transport", "transport", action="stop")
            snap = await T("cleanup overview", "get_session", fresh=True)
            for t in reversed(snap["tracks"]):
                if t["name"].startswith("ZZ-TEST"):
                    await T(f"delete {t['name']}", "track_action", track=t["index"], action="delete")
            for t in reversed(snap["return_tracks"]):
                if "ZZ-TEST" in t["name"]:  # Live prefixes return names with "A-", "B-", ...
                    await T(f"delete return {t['name']}", "track_action", track=f"return {t['index']}",
                            action="delete")
            for sc in reversed(snap["scenes"]):
                if sc["name"].startswith("ZZ-TEST"):
                    await T(f"delete scene {sc['index']}", "scene", action="delete", index=sc["index"])
            restore = {k: v for k, v in song0.items() if not isinstance(v, dict)}
            restore.pop("current_song_time", None)
            await T("restore song props", "lom_set", path="live_set", values=restore)
            await T("release nudges", "lom_set", path="live_set", values={"nudge_up": False, "nudge_down": False})
            await T("restore song time", "lom_set", path="live_set",
                    values={"current_song_time": song0["current_song_time"]})
            await T("restore view props", "lom_set", path="live_set view",
                    values={"follow_song": view0["follow_song"], "draw_mode": view0["draw_mode"]})
            now = {t["index"]: t.get("arm") for t in final_tracks(snap)}
            for idx, armed in arms0.items():
                if now.get(idx) != armed:
                    await T(f"restore arm {idx}", "lom_set", path=f"track {idx}", values={"arm": armed})
            if sel0:
                await T("restore selection", "lom_set", path="live_set view", values={"selected_track": sel0})
            # ------------------------------------------------ user-object safety net
            user_sig1 = await user_clip_signature(s)
            added = sorted(user_sig1 - user_sig0)
            removed = sorted(user_sig0 - user_sig1)
            ok_user = not added and not removed
            rows.append(("PASS" if ok_user else "FAIL", "user-objects",
                         f"user clips unchanged (added {len(added)}, removed {len(removed)})", 0, ""))
            print(("PASS" if ok_user else "FAIL") +
                  f"  user clips unchanged: {len(user_sig0)} snapshotted, added {len(added)}, removed {len(removed)}")
            for x in (added + removed)[:5]:
                print("     diff:", x)
            final = await T("final overview", "get_session", fresh=True)
            same = (len(final["tracks"]) == len(sess0["tracks"]) and len(final["scenes"]) == n_scenes0
                    and final["tempo"] == sess0["tempo"])
            rows.append(("PASS" if same else "FAIL", "-", "set restored (tracks/scenes/tempo)", 0, ""))
            print(("PASS" if same else "FAIL") + "  set restored: tracks/scenes/tempo identical")

        missing = sorted(all_tools - used)
        counts = {k: sum(1 for r in rows if r[0] == k) for k in ("PASS", "EXPECTED", "FAIL")}
        print(f"\n==== {counts}  tools exercised {len(used & all_tools)}/{len(all_tools)}")
        if missing:
            print("not exercised:", missing)
        for r in rows:
            if r[0] == "FAIL":
                print(f"  FAIL {r[1]} / {r[2]}: {r[4][:200]}")
        json.dump(rows, open(sys.argv[1] if len(sys.argv) > 1 else "full_live_test_results.json", "w"), indent=1)
        # coverage evidence: every (tool, action) called + the ops the bridge actually executed in this run
        try:
            st = await s.call_tool("live_status", {})
            ops_run = json.loads(st.content[0].text).get("ops_executed", {})
            bo = await s.call_tool("bridge_ops", {})
            all_ops = json.loads(bo.content[0].text).get("ops", [])
        except Exception as e:  # noqa: BLE001
            ops_run, all_ops = {}, []
            print("coverage: could not read bridge status:", e)
        cov_path = (sys.argv[1] if len(sys.argv) > 1 else "full_live_test_results.json") + ".coverage.json"
        json.dump({"pairs": [[t, a, n] for (t, a), n in sorted(PAIRS.items(), key=lambda kv: (kv[0][0], str(kv[0][1])))],
                   "ops_executed": ops_run, "all_ops": all_ops, "inner_ops": INNER_OPS,
                   "note": "batch inner ops are not counted per op"}, open(cov_path, "w"), indent=1)
        print("coverage evidence ->", cov_path)


async def run_all(T, sess0, n0):
    # park the playhead far past the song: transport/record tests punch in at the playhead and would
    # otherwise rewrite user arrangement clips (this was the root cause of the bar-1 fragmentation)
    await T("stop before suite", "transport", action="stop")
    # the park position is derived from the CURRENT set (a hard-coded 896 punched holes into a set that had grown
    # past it). Live refuses a song time beyond song_length, so park AT song_length and require that it lies
    # after the end of every user clip; otherwise the record tests are skipped (see PARK_SAFE).
    global PARK, PARK_SAFE
    extent = await T("song extent", "lom_get", path="live_set", props=["song_length"])
    last_end = 0.0
    for t in (sess0 or {}).get("tracks", []):
        lst = await T("clips for park", "arrangement", action="list", track=t["name"])
        for x in (lst or {}).get("items", []) if isinstance(lst, dict) else (lst or []):
            last_end = max(last_end, float(x["end_time"]))
    PARK = float((extent or {}).get("song_length") or 0)
    PARK_SAFE = PARK >= last_end + 4.0
    print(f"park: song_length {PARK}, last user clip ends {last_end}, safe for recording: {PARK_SAFE}")
    await T("park playhead", "lom_set", path="live_set", values={"current_song_time": PARK, "start_time": PARK})
    parked = await T("verify parked", "lom_get", path="live_set", props=["current_song_time", "start_time"])
    print(f"playhead parked at: {(parked or {}).get('current_song_time')}")
    print("---- status / catalog")
    await T("live_status", "live_status", check=lambda r: r["tick_hz"] > 0)
    await T("bridge_ops", "bridge_ops", check=lambda r: "batch" in r["ops"])
    await T("lom_catalog all", "lom_catalog")
    await T("lom_catalog query", "lom_catalog", query="warp")
    await T("lom_catalog class", "lom_catalog", cls="Clip")
    await T("tuning read", "tuning", check=lambda r: "tuning_system" in r or "name" in r)
    await T("splice_status", "splice_status", check=lambda r: "subscription" in r)
    await T("app info", "app_control", action="info", check=lambda r: r["version"])
    await T("scale", "scale", key="A minor", check=lambda r: r["names"][0])

    print("---- tracks")
    await T("create MIDI A", "create_track", kind="midi", name=A)
    await T("create MIDI B", "create_track", kind="midi", name=B)
    await T("create audio", "create_track", kind="audio", name=AU)
    await T("create return", "create_track", kind="return", name="ZZ-TEST Return")
    await T("get_track", "get_track", track=A, check=lambda r: r["name"] == A)
    await T("update_track mixer", "update_track", track=A, volume="-6 dB", pan=0.25, sends={0: "-12 dB"},
            color=0xFF0000, check=lambda r: "volume" in r)
    await T("update_track mute/solo off", "update_track", track=A, mute=True, solo=False)
    await T("update_track unmute", "update_track", track=A, mute=False)
    await T("track_extra meters", "track_extra", action="meters", track=A)
    await T("track_extra group", "track_extra", action="group", track=A)
    await T("track_extra fold (not a group)", "track_extra", action="fold", track=A, expect_error="not a group")
    await T("track_extra view", "track_extra", action="view", track=A, values={"is_collapsed": False})
    await T("track_extra mixer read", "track_extra", action="mixer", track=A)
    await T("track_extra mixer set", "track_extra", action="mixer", track=A,
            values={"crossfade_assign": 1, "panning_mode": 0})
    await T("routing list", "routing", track=AU, check=lambda r: r["output"]["types"])
    await T("routing set output", "routing", track=AU, output_type="Sends Only",
            check=lambda r: r["output"]["type"] == "Sends Only")

    print("---- clips / notes / music helpers")
    notes = [{"pitch": 57, "start_time": 0, "duration": 1}, {"pitch": 60, "start_time": 1, "duration": 0.5}]
    await T("create_clip+notes", "create_clip", track=A, slot=0, length=4, name="ZZ clip", notes=notes)
    # pin the selection to a ZZ-TEST clip so every "selected clip" style tool acts on test material only
    await T("select ZZ clip", "select", track=A, slot=0)
    await T("get_clip", "get_clip", track=A, slot=0, check=lambda r: r["length"] == 4)
    await T("update_clip loop", "update_clip", track=A, slot=0, props={"loop_start": 1, "loop_end": 3,
                                                                         "name": "ZZ renamed"})
    await T("update_clip restore loop", "update_clip", track=A, slot=0, props={"loop_start": 0, "loop_end": 4})
    g = await T("get_notes", "get_notes", track=A, slot=0, check=lambda r: r["count"] == 2)
    await T("write_notes add", "write_notes", track=A, slot=0, notes=[{"pitch": 64, "start_time": 2}])
    await T("write_notes replace", "write_notes", track=A, slot=0, mode="replace", notes=notes)
    g = await T("get_notes again", "get_notes", track=A, slot=0, check=lambda r: r["count"] == 2)
    ids = [n["note_id"] for n in g["notes"]] if g else []
    await T("edit_notes modify", "edit_notes", track=A, slot=0, changes=[{"note_id": ids[0], "velocity": 70}])
    await T("edit_notes remove", "edit_notes", track=A, slot=0, remove_ids=[ids[1]])
    for act, kw in [("quantize", {"grid": "1/16", "amount": 0.5}), ("quantize_pitch", {"pitch": 57, "grid": "1/8"}),
                    ("duplicate_loop", {}), ("duplicate_region", {"region_start": 0, "region_length": 1,
                                                                    "destination_time": 2}),
                    ("select_all", {}), ("selected_notes", {}), ("deselect_all", {}), ("all_notes", {}),
                    ("duplicate_notes", {"note_ids": ids[:1]}), ("select_notes", {"note_ids": ids[:1]}),
                    ("set_grid", {"grid": "1/16"}), ("crop", {}), ("show_loop", {}),
                    ("fire_button", {"state": False}), ("move_playing_pos", {"beats": 0}),
                    ("scrub", {"beats": 0}), ("stop_scrub", {}), ("stop", {}),
                    ("show_envelope", {"parameter": f"track {A!r} mixer_device volume".replace("'", "")}),
                    ("hide_envelope", {})]:
        if act == "show_envelope":
            kw = {"parameter": None}
        await T(f"clip_edit {act}", "clip_edit", action=act, track=A, slot=0, **kw)
    await T("write_drum_pattern", "write_drum_pattern", track=B, slot=0,
            rows={"kick": "x...x...x...x...", "hat": "..x...x...x...x."}, bars_repeat=2)
    await T("write_chords", "write_chords", track=A, slot=1, progression="Am F C G", style="arp_up", bass=True)
    await T("write_chords roman", "write_chords", track=A, slot=2, progression="ii7 V7 Imaj7", key="C major")
    await T("write_melody", "write_melody", track=B, slot=1, melody="A3:1 C4:.5 E4:.5 r:1 [A2 E3]:1 G4:1@80")
    await T("clip_action duplicate", "clip_action", track=A, slot=1, action="duplicate", to_slot=3)
    await T("clip_action fire", "clip_action", track=A, slot=1, action="fire")
    await asyncio.sleep(0.4)
    await T("clip_action stop", "clip_action", track=A, slot=1, action="stop")
    await T("transport stop", "transport", action="stop")
    await T("clip_action delete", "clip_action", track=A, slot=3, action="delete")
    await T("track_extra duplicate_slot", "track_extra", action="duplicate_slot", track=A, index=0)

    print("---- automation")
    pv = f"track {A}".replace(A, "")  # placeholder, resolved below
    tinfo = await T("get_track for param path", "get_track", track=A, include_clips=False)
    vol = tinfo["mixer"]["volume"] if tinfo else None
    tref = tinfo["ref"]["$ref"] if tinfo else None
    pv = f"{tref} mixer_device volume"
    await T("automation write", "automation", action="write", parameter=pv, track=A, slot=0,
            points=[{"time": 0, "value": 0.4}, {"time": 2, "value": 0.8}])
    await T("automation read", "automation", action="read", parameter=pv, track=A, slot=0, times=[1, 3],
            check=lambda r: abs(r["values"][0]["value"] - 0.4) < 1e-3)
    await T("automation has", "automation", action="has", track=A, slot=0, check=lambda r: r["has_envelopes"])
    await T("automation clear", "automation", action="clear", parameter=pv, track=A, slot=0)
    await T("automation clear_all", "automation", action="clear_all", track=A, slot=0)
    await T("automation re_enable", "automation", action="re_enable", parameter=pv)
    _ = vol

    print("---- arrangement / audio / warp")
    t0 = 4096.0  # far right on the timeline, away from the user's material
    await T("arr create_midi_clip", "arrangement", action="create_midi_clip", track=A, start_time=t0, length=4,
            notes=notes)
    await T("arr duplicate_from_session", "arrangement", action="duplicate_from_session", track=A, slot=0,
            start_time=t0 + 8)
    await T("arr create_audio_clip", "arrangement", action="create_audio_clip", track=AU, start_time=t0,
            file_path=FIXTURE)
    arr = await T("arr list", "arrangement", action="list", track=AU, check=lambda r: r["count"] == 1)
    aref = arr["items"][0]["ref"]["$ref"] if arr else None
    await T("warp list", "warp_markers", clip=aref, check=lambda r: "markers" in r)
    await T("warp add", "warp_markers", action="add", clip=aref, beat_time=8.0)
    await T("warp move", "warp_markers", action="move", clip=aref, beat_time=8.0, distance=0.5)
    await T("warp remove", "warp_markers", action="remove", clip=aref, beat_time=8.5)
    await T("warp set mode", "warp_markers", action="set", clip=aref, warp_mode=0, warping=True)
    arrA = await T("arr list A", "arrangement", action="list", track=A)
    if arrA:
        await T("track_extra delete_arrangement_clip", "track_extra", action="delete_arrangement_clip", track=A,
                clip=arrA["items"][-1]["ref"]["$ref"])
    await T("take_lanes create", "take_lanes", action="create", track=A, name="ZZ lane")
    await T("take_lanes list", "take_lanes", track=A, check=lambda r: r["count"] >= 1)
    await T("take_lanes rename", "take_lanes", action="rename", track=A, lane=0, name="ZZ lane 2")
    await T("take_lanes midi clip", "take_lanes", action="create_midi_clip", track=A, lane=0, start_time=t0,
            length=4)

    print("---- devices")
    await T("insert Wavetable (replace)", "insert_device", name="Wavetable", track=A, replace_instrument=True)
    await T("insert Wavetable no replace", "insert_device", name="Operator", track=A, expect_error="replace_instrument")
    await T("insert Reverb", "insert_device", name="Reverb", track=A)
    dev = await T("get_device Reverb", "get_device", track=A, device="Reverb", include_chains=True)
    pname = dev["parameters"][1]["name"] if dev else "Dry/Wet"
    await T("set_device_params display", "set_device_params", track=A, device="Reverb",
            params=[{"name": "Dry/Wet", "display": "35 %"}, {"index": 1, "normalized": 0.5}])
    _ = pname
    await T("device_special info", "device_special", track=A, device=0, check=lambda r: r["properties"])
    await T("device_special set", "device_special", action="set", track=A, device=0, values={"mono_poly": 1})
    await T("device_special call", "device_special", action="call", track=A, device=0,
            method="get_modulation_value", args=[0, 0])
    for act in ("info", "deactivate", "activate", "collapse", "expand", "ab_save", "ab_toggle"):
        await T(f"device_control {act}", "device_control", action=act, track=A, device="Reverb")
    await T("device_control ab_set", "device_control", action="ab_set", track=A, device="Reverb", value=False)
    await T("device_control duplicate fx", "device_control", action="duplicate", track=A, device="Reverb")
    await T("device_control duplicate instrument", "device_control", action="duplicate", track=A, device=0,
            expect_error="can't duplicate an instrument")
    await T("device_control max_banks (not M4L)", "device_control", action="max_banks", track=A, device="Reverb",
            expect_error="not available")
    await T("device_control select (UI)", "device_control", action="select", track=A, device="Reverb")
    await T("device_control select_instrument (UI)", "device_control", action="select_instrument", track=A,
            device=0)
    r2 = await T("get_track A devices", "get_track", track=A, include_clips=False)
    last = r2["devices"][-1]["ref"]["$ref"] if r2 else None
    await T("move_device", "move_device", device=last, target=B)
    await T("delete_device", "delete_device", track=B, device=len((await T("get_track B", "get_track", track=B,
                                                                          include_clips=False))["devices"]) - 1)
    await T("device_control delete", "device_control", action="delete", track=A, device="Reverb")
    await T("insert Compressor", "insert_device", name="Compressor", track=AU)
    await T("routing sidechain list", "routing", device=f"track {await idx_of(T, AU)} devices 0",
            check=lambda r: "sidechain" in r)
    await T("load_device browser", "load_device", track=AU, query="Auto Filter", category="audio_effects")
    bs = await T("browser_search", "browser_search", query="reverb", categories=["audio_effects"], limit=3)
    uri = bs["results"][0]["uri"] if bs and bs.get("results") else None
    await T("preview start", "preview", uri=uri)
    await asyncio.sleep(0.5)
    await T("preview stop", "preview", stop=True)

    print("---- racks")
    await T("insert Drum Rack (replace)", "insert_device", name="Drum Rack", track=B, replace_instrument=True)
    for act, kw in [("info", {}), ("add_macro", {}), ("remove_macro", {}), ("randomize_macros", {}),
                    ("store_variation", {}), ("recall_variation", {"variation": 0}), ("recall_last_variation", {}),
                    ("delete_variation", {"variation": 0}), ("insert_chain", {}), ("insert_chain", {}),
                    ("show_chains", {}), ("hide_chains", {})]:
        await T(f"rack {act}", "rack", action=act, track=B, device=0, **kw)
    await T("rack chain_set", "rack", action="chain_set", track=B, device=0, chain=0,
            values={"name": "ZZ chain", "volume": "-3 dB", "in_note": 36})
    await T("rack chain_insert_device", "rack", action="chain_insert_device", track=B, device=0, chain=0,
            device_name="Simpler")
    await T("rack chain_delete_device", "rack", action="chain_delete_device", track=B, device=0, chain=0,
            position=0)
    await T("rack select_chain (UI)", "rack", action="select_chain", track=B, device=0, chain=0)
    await T("rack select_pad (UI)", "rack", action="select_pad", track=B, device=0, pad=36)
    await T("rack copy_pad", "rack", action="copy_pad", track=B, device=0, source=36, destination=37)
    await T("rack pad_set", "rack", action="pad_set", track=B, device=0, pad=37, values={"mute": True})
    await T("rack pad_clear", "rack", action="pad_clear", track=B, device=0, pad=37)

    print("---- samples / Simpler / Looper")
    await T("sample_library status", "sample_library", check=lambda r: r["indexed"] > 0)
    sk = await T("sample_search kick", "sample_search", query="kick", loop=False, limit=2)
    kick = sk["results"][0]["path"] if sk else FIXTURE
    await T("sample_search filters", "sample_search", bpm=124, key="Am", loop=True, limit=3)
    await T("load_drum_kit", "load_drum_kit", track=B, pads={"kick": "kick", "snare": "snare", "hat": "closed hat"})
    await T("load_sample drum_pad", "load_sample", path=kick, target="drum_pad", track=B, pad="clap")
    await T("load_sample simpler", "load_sample", path=FIXTURE, target="simpler", track=A, replace_instrument=True)
    await T("load_sample session", "load_sample", path=kick, target="session", track=AU, slot=0)
    await T("load_sample arrangement", "load_sample", path=kick, target="arrangement", track=AU,
            start_time=t0 + 64)
    await T("simpler info", "simpler", action="info", track=A, device=0)
    await T("simpler set slicing mode", "simpler", action="set", track=A, device=0, values={"playback_mode": 2})
    inserted = await T("simpler insert_slice", "simpler", action="insert_slice", track=A, device=0, time=48000,
                       check=lambda result: 48000 in result["sample"]["slices"])
    slices = (inserted or {}).get("sample", {}).get("slices", [])
    if 48000 in slices:
        await T("simpler select_slice", "simpler", action="select_slice", track=A, device=0,
                slice_index=slices.index(48000))
    for act, kw in [("move_slice", {"time": 48000, "to_time": 52000}), ("remove_slice", {"time": 52000}),
                    ("clear_slices", {}), ("reset_slices", {}),
                    ("warp_as", {"beats": 64}), ("warp_double", {}), ("warp_half", {}), ("guess_length", {}),
                    ("crop", {}), ("reverse", {}), ("replace_sample", {"file_path": kick}),
                    ("set", {"sample_values": {"gain": 0.5}})]:
        await T(f"simpler {act}", "simpler", action=act, track=A, device=0, **kw)
    tempo_before_looper = (await T("tempo before looper", "lom_get", path="live_set", props=["tempo"]))["tempo"]
    await T("insert Looper", "insert_device", name="Looper", track=AU)
    lp = await idx_of_device(T, AU, "Looper")
    for act in ("info", "record", "stop", "play", "overdub", "stop", "undo", "clear", "double_speed",
                "half_speed", "double_length", "half_length"):
        await T(f"looper {act}", "looper", action=act, track=AU, device=lp)
    await T("looper set", "looper", action="set", track=AU, device=lp, values={"overdub_after_record": True})
    await T("looper export (empty)", "looper", action="export", track=AU, device=lp,
            clip_slot=f"track {await idx_of(T, AU)} clip_slots 2", expect_error="audio content")
    await T("transport stop", "transport", action="stop")
    await T("restore tempo after looper", "set_song", tempo=tempo_before_looper)

    print("---- scenes")
    await T("scene create", "scene", action="create", index=-1, name="ZZ-TEST scene")
    n = n0
    await T("scene set", "scene", action="set", index=n, name="ZZ-TEST scene", tempo=None)
    await T("scene duplicate", "scene", action="duplicate", index=n)
    await T("scene set dup name", "scene", action="set", index=n + 1, name="ZZ-TEST scene dup")
    await T("scene fire", "scene", action="fire", index=n)
    await asyncio.sleep(0.3)
    await T("launch_scene", "launch_scene", index=n, force_legato=True, select=False)
    await asyncio.sleep(0.3)
    await T("transport stop", "transport", action="stop")

    print("---- transport / song / record")
    t_before = (await T("tempo read", "lom_get", path="live_set", props=["tempo"]))["tempo"]
    await T("set_song tempo", "set_song", tempo=t_before + 1)
    for action in ("undo", "redo"):
        await T(f"transport {action} guard", "transport", action=action, expect_error="unavailable through AgentBridge")
        await T(f"LOM {action} guard", "lom_call", path="live_set", method=action,
                expect_error="unavailable through AgentBridge")
    tv = (await T("tempo after undo", "lom_get", path="live_set", props=["tempo"]))["tempo"]
    rows.append(("PASS" if abs(tv - (t_before + 1)) < 1e-6 else "FAIL", "transport", "history guards", 0, str(tv)))
    await T("set_song restore tempo", "set_song", tempo=t_before)
    await T("set_song extra", "set_song", extra={"swing_amount": 0.0})
    for act in ("metronome", "metronome", "loop", "loop", "re_enable_automation", "jump_to_next_cue",
                "jump_to_prev_cue"):
        await T(f"transport {act}", "transport", action=act)
    await T("transport seek", "transport", action="seek", value=0)
    await T("transport jump", "transport", action="jump", value=4)
    await T("transport play", "transport", action="play")
    await asyncio.sleep(0.5)
    await T("transport stop", "transport", action="stop")
    await T("transport continue", "transport", action="continue")
    await asyncio.sleep(0.3)
    await T("transport stop_all_clips", "transport", action="stop_all_clips")
    await T("transport stop", "transport", action="stop")
    await T("transport tap_tempo", "transport", action="tap_tempo")
    await T("set_song restore tempo", "set_song", tempo=t_before)
    await T("transport capture_midi", "transport", action="capture_midi", expect_error="")
    # recording with the loop enabled jumps back to the loop start and punches user clips there
    await T("loop off for record tests", "lom_set", path="live_set", values={"loop": False})
    await T("stop before record tests", "transport", action="stop")
    # several earlier tests seek the playhead; re-park right before recording and abort if it did not stick
    await T("re-park before record tests", "lom_set", path="live_set", values={"current_song_time": PARK, "start_time": PARK})
    chk = await T("verify re-park", "lom_get", path="live_set", props=["current_song_time", "start_time"])
    if not PARK_SAFE:
        print("!! no park position after the last user clip (Live caps song time at song_length); aborting "
              "before the record tests to protect user clips")
        rows.append(("FAIL", "record", "no safe park position for record tests", 0, f"PARK={PARK}"))
        return
    if not (isinstance(chk, dict) and float(chk.get("current_song_time") or 0) >= PARK - 1e-3 and float(chk.get("start_time") or 0) >= PARK - 1e-3):
        print("!! playhead is not parked at the song end; aborting before the record tests to protect user clips")
        return
    for act in ("record", "record", "session_record", "session_record"):
        await T(f"transport {act} toggle", "transport", action=act)
    for act in ("punch_in", "punch_out", "overdub", "automation_arm", "arrangement"):
        await T(f"record {act} on", "record", action=act, value=True)
        await T(f"record {act} off", "record", action=act, value=False)
    await T("record count_in", "record", action="count_in")
    await T("record capture_midi", "record", action="capture_midi", expect_error="nothing to capture")
    ids_before = {x["$ref"] for x in (await T("scene ids", "lom_children", path="live_set", attr="scenes",
                                                  limit=500))["items"]}
    await T("record capture_scene", "record", action="capture_scene")
    after = (await T("scene ids after", "lom_children", path="live_set", attr="scenes", limit=500))["items"]
    new = [x for x in after if x["$ref"] not in ids_before]
    if new:  # captured scenes are inserted mid-list: tag it so cleanup removes exactly this one
        await T("tag captured scene", "lom_set", path=new[0]["$ref"], values={"name": "ZZ-TEST captured"})
    await T("record clip_slot 1 bar", "record", action="clip_slot", track=A, slot=5, length=4)
    await asyncio.sleep(0.6)
    await T("transport stop", "transport", action="stop")
    await T("record session", "record", action="session", length=4)
    await asyncio.sleep(0.4)
    await T("record session off", "record", action="session")
    await T("transport stop", "transport", action="stop")
    # leave record state clean and put the playhead back before the following song_action tests
    await T("record_mode off", "lom_set", path="live_set", values={"record_mode": False})
    await T("session_record off", "transport", action="session_record", expect_error="")
    await T("restore playhead after record tests", "lom_set", path="live_set",
            values={"current_song_time": 0.0, "start_time": 0.0})
    for act in ("smpte", "beats_time", "loop_beats", "re_enable_automation", "force_link_beat_time",
                "back_to_arranger", "nudge_up", "nudge_down", "stop_all_clips"):
        await T(f"song_action {act}", "song_action", action=act)
    # nudge_up/down are held buttons; a stuck nudge keeps Live's transport running fast (2026-11-16 incident)
    await asyncio.sleep(0.2)
    await T("nudges released", "lom_get", path="live_set", props=["nudge_up", "nudge_down"],
            check=lambda r: r["nudge_up"] is False and r["nudge_down"] is False)
    await T("song_action scrub_by", "song_action", action="scrub_by", beats=1)
    await T("song_action jump_by", "song_action", action="jump_by", beats=1)
    await T("song_action play_selection", "song_action", action="play_selection")
    await asyncio.sleep(0.3)
    await T("transport stop", "transport", action="stop")
    far = 1.0  # cue points must lie inside the song
    existing = await T("cue list before", "cue_points")
    while any(abs(c["time"] - far) < 1e-6 for c in (existing or {}).get("items", [])):
        far += 1.0
    # While stopped Live places locators at the arrangement insert marker / selection start, which the LOM cannot
    # move; an explicit-time toggle therefore depends on UI state. Accept the documented refusal (the op undoes
    # any stray cue) and exercise rename/jump on an existing cue instead.
    await T("cue fixture seek", "transport", action="seek", value=far)
    added = await T("cue toggle add", "cue_points", action="toggle", time=far, expect_error="insert marker",
                    check=lambda r: len(r["added"]) == 1)
    if added and added.get("added"):
        far = added["added"][0]["time"]
        marked = await T("cue fixture list", "cue_points", action="list")
        owned = next((c for c in (marked or {}).get("items", []) if abs(c["time"] - far) < 1e-6), None)
        if owned:
            await T("cue rename fixture", "cue_points", action="rename", index=owned["index"], name="ZZ-TEST Cue")
            await T("cue jump to fixture", "cue_points", action="jump", index=owned["index"])
        await T("cue fixture return", "transport", action="seek", value=far)
        await T("cue toggle remove", "cue_points", action="toggle", time=far, check=lambda r: len(r["removed"]) == 1)
    cues = await T("cue list", "cue_points", check=lambda r: len(r["items"]) == len((existing or {}).get("items", [])))
    first = ((cues or {}).get("items") or [None])[0]
    if first:
        await T("cue rename (same name)", "cue_points", action="rename", index=first["index"], name=first["name"])
        await T("cue jump", "cue_points", action="jump", index=first["index"])
    await T("cue prev", "cue_points", action="prev")
    await T("cue next", "cue_points", action="next")
    await T("cue list after", "cue_points",
            check=lambda r: [(c["name"], c["time"]) for c in r["items"]] ==
            [(c["name"], c["time"]) for c in (existing or {}).get("items", [])])
    gp = await T("groove list", "groove_pool")
    await T("groove assign none (unsupported)", "groove_pool", action="assign", track=A, slot=0,
            expect_error="cannot remove")
    if gp and gp.get("grooves"):
        await T("groove assign 0", "groove_pool", action="assign", track=A, slot=0, groove=0)
    await T("groove global same", "groove_pool", action="global", values={"groove_amount": gp["groove_amount"]})
    if gp and gp["grooves"]:
        g0 = gp["grooves"][0]
        await T("groove set same", "groove_pool", action="set", groove=0,
                values={"timing_amount": g0["timing_amount"]})
    ts = await T("tuning read 2", "tuning")
    if ts and ts.get("name"):
        await T("tuning set same", "tuning", values={"reference_pitch": ts["reference_pitch"]})

    print("---- generic LOM / events / memory / batch")
    ai = await idx_of(T, A)
    await T("lom_describe", "lom_describe", path=f"track {ai}", check=lambda r: r["methods"])
    await T("lom_get", "lom_get", path=f"live_set tracks {ai}", props=["name", "arm"])
    await T("lom_set", "lom_set", path=f"track {ai}", values={"name": A})
    await T("lom_children", "lom_children", path="live_set", attr="tracks", limit=3)
    await T("lom_call", "lom_call", path=f"track {ai}", method="stop_all_clips")
    sub = await T("observe", "observe", path=f"track {ai}", prop="name")
    await T("rename to trigger", "lom_set", path=f"track {ai}", values={"name": A + " x"})
    # a second, idle subscription: polling it must not hand out (or consume) the name events
    sub_idle = await T("observe idle", "observe", path=f"track {ai}", prop="solo")
    sid, sid_idle = (sub or {}).get("sub"), (sub_idle or {}).get("sub")
    await T("rename to trigger", "lom_set", path=f"track {ai}", values={"name": A + " x"})
    await T("poll_events other sub (filtered)", "poll_events", wait=0.5, sub=sid_idle,
            check=lambda r: r["count"] == 0)
    await T("poll_events", "poll_events", wait=2, sub=sid,
            check=lambda r: any(e.get("prop") == "name" and e.get("sub") == sid for e in r["events"]))
    await T("unobserve", "unobserve", sub=sid)
    await T("unobserve idle", "unobserve", sub=sid_idle)
    await T("lom_set name back", "lom_set", path=f"track {ai}", values={"name": A})
    await T("live_status slowest_ops", "live_status",
            check=lambda r: isinstance(r.get("slowest_ops"), list) and r["slowest_ops"]
            and all(x["n"] > 0 and x["max_ms"] >= x["mean_ms"] for x in r["slowest_ops"]))
    # Live cannot remove a song data key (set_data(key, None) leaves a null): round-trip song memory on a key
    # that already exists for this purpose, and keep throwaway keys on the fixture track (gone with the track)
    await T("set_memory fixture", "set_memory", key="zz_test", value={"ok": 1}, track=A)
    await T("get_memory fixture", "get_memory", key="zz_test", track=A, check=lambda r: r["value"] == {"ok": 1})
    await T("get_memory song (read only)", "get_memory", key="zz_test")
    await T("set_memory track", "set_memory", key="role", value="test", track=A)
    await T("get_memory track", "get_memory", key="role", track=A, check=lambda r: r["value"] == "test")
    await T("batch", "batch", ops=[{"op": "lom.get", "args": {"path": f"track {ai}", "props": ["name"]}}] * 10,
            undo_group=False, check=lambda r: r["completed"] == 10)
    await T("track_extra jump_in_clip", "track_extra", action="jump_in_clip", track=A, beats=0)
    await T("lom_audit", "lom_audit", check=lambda r: r["totals"]["present"] > 300)

    print("---- UI tools (restored afterwards)")
    vis = await T("app is_view_visible", "app_control", action="is_view_visible", view="Browser")
    await T("app show_view", "app_control", action="show_view", view="Session")
    await T("app hide/show Browser", "app_control", action="hide_view" if vis and vis["visible"] else "show_view",
            view="Browser")
    await T("app restore Browser", "app_control", action="show_view" if vis and vis["visible"] else "hide_view",
            view="Browser")
    await T("app focus_view", "app_control", action="focus_view", view="Session")
    await T("app scroll_view", "app_control", action="scroll_view", view="Session", direction=1)
    await T("app scroll back", "app_control", action="scroll_view", view="Session", direction=0)
    await T("app zoom_view", "app_control", action="zoom_view", view="Arranger", direction=0)
    await T("app zoom back", "app_control", action="zoom_view", view="Arranger", direction=1)
    await T("app toggle_browse", "app_control", action="toggle_browse")
    await T("app toggle_browse back", "app_control", action="toggle_browse")
    await T("app message", "app_control", action="message", view="AgentBridge test")
    await T("app press_dialog_button (none open)", "app_control", action="press_dialog_button",
            expect_error="no dialog")
    await T("view_state", "view_state", values={"follow_song": False})
    await T("select (UI)", "select", track=A, scene=0)
    await T("track_action stop_clips", "track_action", track=A, action="stop_clips")
    await T("track_action duplicate", "track_action", track=B, action="duplicate")

    print("---- ACE-Step")
    st = await T("acestep_status", "acestep_status")
    ace_ready = bool(isinstance(st, dict) and st.get("ready"))
    if ace_ready:
        await T("acestep_service start (idempotent)", "acestep_service", action="start",
                check=lambda r: r["status"]["ready"] and r.get("skipped"))
        gen = await T("acestep_generate_part + import", "acestep_generate_part", reference=FIXTURE, track_name="synth",
                      prompt="An isolated, dry melodic synth hook for instrumental tech house, A minor. Synth only, "
                             "no drums, no bass, no vocals.", bpm=124, key_scale="A minor", seed=1242026,
                      import_to="arrangement", start_time=t0, wait_s=180,
                      check=lambda r: r["status"] == "succeeded" and r["imported"])
        if gen and gen.get("task_id"):
            await T("acestep_result (resume, no import)", "acestep_result", task_id=gen["task_id"], import_to="none",
                    check=lambda r: r["status"] == "succeeded")
            name = gen["imported"][0]["track"]
            await T("rename ACE track", "lom_set", path=f"track {await idx_of(T, name)}",
                    values={"name": "ZZ-TEST ACE"})
            await playback_check(T, gen["outputs"][0]["file"])
    else:
        # acestep_result with an unknown id only does a loopback HTTP GET: a real, side-effect-free refusal check
        await T("acestep_result (service off)", "acestep_result", task_id="zz-no-such-task", import_to="none",
                wait_s=0, expect_error="")
        # service start/stop would start or stop the GPU service and generate_part stages files into it: documented NOT_RUN
        for label, tool in (("acestep_service start", "acestep_service"),
                            ("acestep_generate_part", "acestep_generate_part")):
            used.add(tool)
            rows.append(("EXPECTED", tool, f"{label} (ACE-Step service off)", 0, ""))
            print(f"EXPT  {tool:22} ACE-Step service off -> documented NOT_RUN")

    print("---- gap tools (plugin / taste / sample / M4L)")
    await run_gap_tools(T)
    if __import__("os").environ.get("ZZ_TEST_NEW_ACTIONS") == "1":
        print("---- coverage gaps (every remaining tool action / op) - DISPOSABLE COPY ONLY")
        await run_coverage_gaps(T)
    else:
        print("---- coverage gaps SKIPPED: these steps have never run against this Live build and one of them "
              "crashed Live on 2026-11-17; run them with scripts/acceptance_on_copy.py (sets ZZ_TEST_NEW_ACTIONS=1)")


async def run_coverage_gaps(T):
    """Wave B of docs/TEST_PLAN_FULL.md: the (tool, action) pairs and ops the first suite never called.
    Fixtures only (ZZ-TEST*); the user's taste store and sample index are left exactly as found."""
    import tempfile
    from pathlib import Path

    await T("gap fixture MIDI", "create_track", kind="midi", name="ZZ-TEST GAP")
    gi = await idx_of(T, "ZZ-TEST GAP")
    await T("gap fixture insert Reverb", "insert_device", name="Reverb", track=gi)
    # device_control.store_bank: a native device has no Max parameter banks -> documented refusal
    await T("device_control store_bank (not M4L)", "device_control", action="store_bank", track=gi, device="Reverb",
            bank=0, expect_error="")
    # track_extra.unfold: the LOM cannot create group tracks, so on a normal track it is the same documented refusal
    await T("track_extra unfold (not a group)", "track_extra", action="unfold", track=gi, expect_error="not a group")
    # take_lanes.create_audio_clip on an audio fixture
    await T("gap fixture audio", "create_track", kind="audio", name="ZZ-TEST GAP AUDIO")
    ga = await idx_of(T, "ZZ-TEST GAP AUDIO")
    await T("take_lanes create (audio)", "take_lanes", action="create", track=ga, name="ZZ lane")
    await T("take_lanes audio clip", "take_lanes", action="create_audio_clip", track=ga, lane=0, start_time=0.0,
            file_path=FIXTURE, expect_error="")

    # --- plugin_preset.select / open_editor / close_editor on the Stacker plug-in fixture (plug-in ones are UI/optional)
    pi = await idx_of(T, "ZZ-TEST PLUGIN")
    await T("plugin_preset select", "plugin_preset", action="select", preset=0, track=pi, device="Stacker",
            expect_error="")
    await T("plugin_preset open_editor", "plugin_preset", action="open_editor", track=pi, device="Stacker",
            expect_error="")
    await T("plugin_preset close_editor", "plugin_preset", action="close_editor", track=pi, device="Stacker",
            expect_error="")

    # --- music_reference / sample_analyze job management: read-only list/status/result, refusal for unknown jobs
    await T("music_reference list", "music_reference", action="list")
    await T("music_reference status (unknown job)", "music_reference", action="status", job_id="zz-none",
            expect_error="")
    await T("music_reference result (unknown job)", "music_reference", action="result", job_id="zz-none",
            expect_error="")
    await T("music_reference cancel (unknown job)", "music_reference", action="cancel", job_id="zz-none",
            expect_error="")
    await T("sample_analyze status", "sample_analyze", action="status")
    await T("sample_analyze result (not analysed)", "sample_analyze", action="result", path=FIXTURE, expect_error="")
    await T("sample_analyze cancel (unknown job)", "sample_analyze", action="cancel", job_id="zz-none",
            expect_error="")
    await T("sample_analyze resume (unknown job)", "sample_analyze", action="resume", job_id="zz-none",
            expect_error="")

    # --- sample_library: add a temporary empty folder, check it shows up, rescan (background), remove it again
    tmp = tempfile.mkdtemp(prefix="zz-agent-bridge-")
    before = await T("sample_library status before", "sample_library", action="status")
    await T("sample_library add_folder", "sample_library", action="add_folder", folder=tmp)
    await T("sample_library rescan (background)", "sample_library", action="rescan")
    await T("sample_library remove_folder", "sample_library", action="remove_folder", folder=tmp)
    after = await T("sample_library status after", "sample_library", action="status")
    try:
        Path(tmp).rmdir()
    except OSError:
        pass
    same_roots = isinstance(before, dict) and isinstance(after, dict) and before.get("roots") == after.get("roots")
    rows.append(("PASS" if same_roots else "FAIL", "sample_library", "temporary folder added and removed again: roots "
                 "unchanged", 0, "" if same_roots else f"{(before or {}).get('roots')} -> {(after or {}).get('roots')}"))
    print(("PASS" if same_roots else "FAIL") + "  sample_library roots unchanged after add/remove")

    # --- taste_remember lifecycle: event -> remember -> confirm -> correct -> retire, then remove everything it wrote
    from live_agent_bridge.taste.store import TasteStore
    store = TasteStore()

    def counts():
        with store.connection() as db:
            return tuple(db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("events", "preferences"))

    c0 = counts()
    tag = "ZZ-HARNESS-" + str(int(time.time()))
    ev = await T("taste_remember event", "taste_remember", action="event", statement=tag + " event", origin="user",
                 domain="zz-harness", kind="choice", writer="zz-harness")
    pref = await T("taste_remember remember (pending)", "taste_remember", action="remember", statement=tag + " pref",
                   origin="inference", domain="zz-harness", kind="preference", writer="zz-harness",
                   check=lambda r: r["status"] == "pending")
    pid = (pref or {}).get("id")
    if pid:
        await T("taste_remember confirm", "taste_remember", action="confirm", preference_id=pid, statement=tag + " pref",
                origin="user", kind="confirmation", domain="zz-harness", writer="zz-harness",
                check=lambda r: r["status"] == "confirmed")
        await T("taste_remember correct", "taste_remember", action="correct", preference_id=pid,
                statement=tag + " pref corrected", origin="user", kind="correction", domain="zz-harness",
                writer="zz-harness", check=lambda r: r["statement"].endswith("corrected"))
        await T("taste_remember retire", "taste_remember", action="retire", preference_id=pid, statement=tag + " pref",
                origin="user", kind="reject", domain="zz-harness", writer="zz-harness",
                check=lambda r: r["status"] == "retired")
        gone = store.forget(preference_id=pid)
    else:
        gone = {}
    eid = (ev or {}).get("event_id")
    if eid:
        with store.connection(True) as db:
            db.execute("DELETE FROM events WHERE id=?", (eid,))
            store._reindex(db)
    c1 = counts()
    rows.append(("PASS" if c1 == c0 else "FAIL", "taste_remember", f"lifecycle cleanup: (events, preferences) {c0} -> {c1}",
                 0, "" if c1 == c0 else str(gone)))
    print(("PASS" if c1 == c0 else "FAIL") + f"  taste store back to {c0}: now {c1}")

    # --- ops only reachable through other tools: browser.tree (batch), view.show (select), routing.set_io (routing io=)
    await T("batch browser.tree", "batch", ops=[{"op": "browser.tree", "args": {"category": "instruments", "limit": 5}}],
            check=lambda r: r["completed"] == 1)
    await T("select show_view (view.show)", "select", show_view="Session")
    mi = await idx_of(T, "ZZ-TEST M4L")
    await T("routing io (set_io) read on M4L device", "routing", device=f"live_set tracks {mi} devices 0",
            io="midi_inputs", io_index=0, expect_error="")


async def run_gap_tools(T):
    """Cover the tools that need a device or local store, on ZZ tracks only."""
    await T("plugin_inventory in_set", "plugin_inventory", scope="in_set")
    await T("plugin_inventory installed", "plugin_inventory", scope="installed")
    await T("create plugin fixture track", "create_track", kind="midi", name="ZZ-TEST PLUGIN")
    pi = await idx_of(T, "ZZ-TEST PLUGIN")
    await T("load Stacker", "load_device", track=pi, query="Stacker")
    insp = await T("plugin_inspect Stacker", "plugin_inspect", track=pi, device="Stacker")
    ps = (insp or {}).get("exposed_parameters") or []
    if ps:
        p0 = next((x for x in ps if x.get("controllable", True)), ps[0])
        await T("plugin_set no-op (same value)", "plugin_set",
                changes=[{"parameter": p0["name"], "normalized": p0["value"]}], track=pi, device="Stacker")
    await T("plugin_preset list", "plugin_preset", action="list", track=pi, device="Stacker")
    await T("plugin_snapshot save", "plugin_snapshot", action="save", name="zz-test-snap", track=pi, device="Stacker")
    await T("plugin_snapshot list", "plugin_snapshot", action="list", track=pi, device="Stacker")
    await T("plugin_snapshot diff", "plugin_snapshot", action="diff", name="zz-test-snap", track=pi, device="Stacker")
    await T("plugin_snapshot recall", "plugin_snapshot", action="recall", name="zz-test-snap", track=pi, device="Stacker")
    await T("plugin_snapshot morph", "plugin_snapshot", action="morph", name="zz-test-snap", amount=0.5, track=pi,
            device="Stacker")
    await T("plugin_snapshot delete", "plugin_snapshot", action="delete", name="zz-test-snap", track=pi, device="Stacker")
    # plugin_learn round trip on a fixture-only plug-in name: learn -> forget, so the user's store is untouched
    zz_plugin = "ZZ-TEST-PLUGIN"
    await T("plugin_learn pending inference", "plugin_learn", plugin=zz_plugin, parameter="ZZ Param", role="drive",
            note="harness fixture", origin="inference", check=lambda r: r["status"] == "pending")
    await T("plugin_learn forget", "plugin_learn", action="forget", plugin=zz_plugin, parameter="ZZ Param",
            origin="inference", check=lambda r: r["deleted"] is True)
    await T("plugin_learn forget again (no-op)", "plugin_learn", action="forget", plugin=zz_plugin,
            parameter="ZZ Param", origin="inference", check=lambda r: r["deleted"] is False)
    await T("taste_profile", "taste_profile")
    await T("taste_recall", "taste_recall", query="kick", limit=3)
    zz_pref = await T("taste_remember test entry", "taste_remember", action="remember",
                      statement="ZZ-GAP-TEST ignore this entry", origin="inference", domain="test", kind="preference")
    # remove the harness's own pending entry again (there is no MCP forget tool; use the same local store)
    if isinstance(zz_pref, dict) and zz_pref.get("id"):
        from live_agent_bridge.taste.store import TasteStore
        gone = TasteStore().forget(preference_id=zz_pref["id"])
        ok = gone.get("deleted") == 1
        rows.append(("PASS" if ok else "FAIL", "taste_remember", f"cleanup own test entry {gone}", 0, ""))
        print(("PASS" if ok else "FAIL") + f"  taste test entry removed: {gone}")
    hit = await T("sample_search for analyze", "sample_search", query="kick", limit=1)
    path = None
    if isinstance(hit, dict) and hit.get("results"):
        path = (hit["results"][0] or {}).get("path")
    if path:
        await T("sample_analyze", "sample_analyze", path=path)
    from live_agent_bridge.audio.runtime import settings
    missing_model = not (os.environ.get("AGENT_BRIDGE_CLAP_MODEL") or settings().get("model_directory"))
    await T("music_reference analyze", "music_reference", action="analyze", path=FIXTURE, remember=False,
            expect_error="Configure an existing local CLAP model directory" if missing_model else None)
    await T("create M4L fixture track", "create_track", kind="midi", name="ZZ-TEST M4L")
    mi = await idx_of(T, "ZZ-TEST M4L")
    await T("load LOM_API_Bridge", "load_device", track=mi, query="LOM_API_Bridge")
    await T("m4l status", "m4l_bridge", action="status")
    realtime_song = await T("Remote Script tempo for M4L comparison", "lom_get", path="live_set", props=["tempo"])
    await T("m4l get live_set tempo", "m4l_bridge", action="get", path="live_set", object_type="Song", member="tempo",
            check=lambda r: bool(realtime_song and r and isinstance(r.get("value"), (int, float))
                                 and abs(r["value"] - realtime_song["tempo"]) < 1e-6))
    # every other action, writes confined to the fixture track's own name
    mp, mname = f"live_set tracks {mi}", "ZZ-TEST M4L"
    await T("m4l resolve", "m4l_bridge", action="resolve", path=mp, object_type="Track",
            check=lambda r: r["status"] == "ok" and r["target"]["identity"])
    await T("m4l capabilities", "m4l_bridge", action="capabilities", path=mp, object_type="Track", member="name",
            check=lambda r: r["members"][0]["name"] == "name" and "set" in r["members"][0]["access"])
    await T("m4l set preview", "m4l_bridge", action="set", path=mp, object_type="Track", member="name",
            value=mname + " P", check=lambda r: r["status"] == "preview")
    await T("m4l preview did not write", "m4l_bridge", action="get", path=mp, object_type="Track", member="name",
            check=lambda r: r["value"] == mname)
    obs = await T("m4l observe", "m4l_bridge", action="observe", path=mp, object_type="Track", member="name",
                  check=lambda r: r["status"] == "ok" and r["subscription_id"])
    msub = (obs or {}).get("subscription_id")
    await T("m4l set write", "m4l_bridge", action="set", path=mp, object_type="Track", member="name",
            value=mname + " W", write=True, check=lambda r: r["status"] == "ok")
    await T("m4l write read back", "lom_get", path=mp, props=["name"], check=lambda r: r["name"] == mname + " W")
    await T("m4l set name back", "m4l_bridge", action="set", path=mp, object_type="Track", member="name",
            value=mname, write=True, check=lambda r: r["status"] == "ok")
    if msub:
        await asyncio.sleep(0.3)
        await T("m4l poll", "m4l_bridge", action="poll", subscription_id=msub,
                check=lambda r: any(mname + " W" in str(e.get("value")) for e in r["events"]))
        await T("m4l unobserve", "m4l_bridge", action="unobserve", subscription_id=msub,
                check=lambda r: r["closed"] is True)
    await T("m4l call preview", "m4l_bridge", action="call", path=mp, object_type="Track", member="stop_all_clips",
            check=lambda r: r["status"] == "preview")
    await T("m4l wrong object_type", "m4l_bridge", action="get", path=mp, object_type="Clip", member="name",
            expect_error="object_type_mismatch")


def final_tracks(snap):
    return [t for t in snap["tracks"] if not t["name"].startswith("ZZ-TEST")]


async def idx_of(T, name):
    snap = await T(f"find {name}", "get_session", fresh=True)
    return next(t["index"] for t in snap["tracks"] if t["name"] == name)


async def idx_of_device(T, track, dname):
    info = await T(f"devices of {track}", "get_track", track=track, include_clips=False)
    return next(d["index"] for d in info["devices"] if d["name"] == dname)


async def playback_check(T, path):
    """Native playback: launch the generated audio from a session slot and read the track's meter."""
    await T("create playback track", "create_track", kind="audio", name="ZZ-TEST ACE play")
    i = await idx_of(T, "ZZ-TEST ACE play")
    await T("ACE audio into session slot", "load_sample", path=path, target="session", track=i, slot=0)
    await T("fire ACE clip", "clip_action", track=i, slot=0, action="fire")
    peak = 0.0
    for _ in range(12):
        await asyncio.sleep(0.15)
        m = await T("meter", "lom_get", path=f"track {i}", props=["output_meter_level"])
        peak = max(peak, (m or {}).get("output_meter_level") or 0.0)
    await T("stop clip", "clip_action", track=i, slot=0, action="stop")
    await T("stop", "transport", action="stop")
    ok = peak > 0.01
    rows.append(("PASS" if ok else "FAIL", "playback", f"ACE clip plays in Live (peak meter {peak:.3f})", 0, ""))
    print(("PASS" if ok else "FAIL") + f"  native playback of imported ACE audio, output meter peak {peak:.3f}")


if __name__ == "__main__":
    asyncio.run(main())
