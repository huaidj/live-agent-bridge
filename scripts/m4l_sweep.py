"""Wave C of docs/TEST_PLAN_FULL.md: sweep EVERY member the LOM API Bridge (Max for Live) exposes.

Run it on a disposable copy of the set:
    python scripts/acceptance_on_copy.py "<set>.als" --script scripts/m4l_sweep.py -- --json out.json

For each LOM class reachable from fixtures (Song, Song.View, Track x4, MixerDevice, DeviceParameter x2, Device, Scene,
ClipSlot, Clip x2, CuePoint, Application, GroovePool) it pages through the device's own `capabilities` and then, per member:
  get       every member with 'get' access
  observe   every member with 'observe' access: observe -> poll -> unobserve
  set       every member with 'set' access: PREVIEW (dry_run) with the current value - must answer status=preview and
            must not change anything
  call      every member with 'call' access: PREVIEW with no arguments (never executed)
plus a short whitelist of REAL writes with read-back through M4L and through the Remote Script, then restore:
  Track.name, Track.mute, Clip.name, Scene.name, Reverb "Dry/Wet" (DeviceParameter.value).
Exit code 0 only if every member was exercised and no unexplained error remains.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

from fixture_audio import acceptance_audio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from live_agent_bridge.client import LiveClient  # noqa: E402
from live_agent_bridge.m4l import M4LClient, M4LError  # noqa: E402

# device error codes that are documented limits of the LiveAPI / the device, not defects
ALLOWED_DETAIL = ("Expected a dictionary", "Only finite JSON values", "Value is above the documented maximum",
                  "Value is below the documented minimum")
ALLOWED = {"object_unavailable", "unsupported_member", "not_observable", "write_not_supported", "member_unavailable",
           "invalid_argument", "invalid_arguments", "argument_required", "arguments_required", "call_failed",
           "read_failed", "value_not_json", "unsupported_value"}
OUT: dict = {"classes": {}, "failures": [], "writes": [], "counts": Counter()}


def jsonable(v):
    return isinstance(v, (str, int, float, bool)) or v is None


def alt_value(v):
    """A different value of the same type, used to prove a PREVIEW set does not write."""
    if isinstance(v, bool):
        return not v
    if isinstance(v, int):
        return v + 1
    if isinstance(v, float):
        return v + 0.01
    if isinstance(v, str):
        return v + "x"
    return None


async def fingerprint(lc):
    ov = await lc.request("overview", {"fresh": True})
    song = await lc.request("lom.get", {"path": "live_set", "props": [
        "tempo", "is_playing", "current_song_time", "loop", "loop_start", "loop_length", "metronome",
        "arrangement_overdub", "session_record", "record_mode", "signature_numerator"]})
    fx = {}
    ti = CTX.get("ti")
    if ti is not None:
        info = await lc.request("track.info", {"track": ti, "clips": True})
        fx["fixture_devices"] = [d["name"] for d in info["devices"]]
        fx["fixture_clips"] = len(info.get("clips") or info.get("clip_slots") or [])
        fx["fixture_arr_clips"] = len(await lc.request("arrangement.clips", {"track": ti}))
    return {"tracks": [(t["name"], t.get("mute"), t.get("solo")) for t in ov["tracks"]],
            "scenes": [s["name"] for s in ov["scenes"]], "song": song, **fx}


def fp_diff(a, b):
    out = []
    for k in sorted(set(a) | set(b)):
        if a.get(k) != b.get(k):
            if isinstance(a.get(k), dict):
                out += ["song.%s: %r -> %r" % (kk, a[k].get(kk), b[k].get(kk)) for kk in a[k] if a[k].get(kk) != b[k].get(kk)]
            elif isinstance(a.get(k), list) and len(a[k]) == len(b[k]):
                out += ["%s[%d]: %r -> %r" % (k, i, x, y) for i, (x, y) in enumerate(zip(a[k], b[k], strict=True)) if x != y]
            else:
                out.append("%s: %r -> %r" % (k, a.get(k), b.get(k)))
    return "; ".join(out)[:600]


CTX: dict = {}
READONLY_PREFIXES = ("get_", "is_", "__str__", "available_")
# valid arguments for state-changing functions: a PREVIEW must answer status=preview and execute NOTHING
CALL_ARGS = {
    ("Song", "create_midi_track"): lambda c: [-1], ("Song", "create_audio_track"): lambda c: [-1],
    ("Song", "create_scene"): lambda c: [-1], ("Song", "delete_track"): lambda c: [c["ti"]],
    ("Song", "duplicate_track"): lambda c: [c["ti"]], ("Song", "delete_scene"): lambda c: [c["scene_i"]],
    ("Song", "duplicate_scene"): lambda c: [c["scene_i"]], ("Song", "jump_by"): lambda c: [1.0],
    ("Song", "scrub_by"): lambda c: [1.0],
    ("Track", "create_midi_clip"): lambda c: [0.0, 4.0], ("Track", "duplicate_clip_slot"): lambda c: [0],
    ("Track", "insert_device"): lambda c: ["Reverb", 0], ("Track", "delete_device"): lambda c: [0],
}


async def discover(m: M4LClient, path: str, cls: str):
    members, off = [], 0
    while True:
        r = await m.capabilities(path, cls, None, off, 200)
        if r.get("status") != "ok":
            return None, "%s: %s" % (r.get("code"), r.get("detail"))
        members += r["members"]
        if r.get("next_offset") is None:
            return members, None
        off = r["next_offset"]


def fail(cls, member, op, detail):
    OUT["failures"].append({"class": cls, "member": member, "op": op, "detail": str(detail)[:240]})
    OUT["counts"]["FAIL " + op] += 1


async def sweep_target(m: M4LClient, path: str, cls: str, label: str):
    members, err = await discover(m, path, cls)
    row = {"path": path, "members": 0, "exposed": 0, "get": Counter(), "observe": Counter(), "set_preview": Counter(), "set_alt": Counter(),
           "call_preview": Counter(), "skipped_not_exposed": 0, "error": err, "notes": []}
    OUT["classes"][label] = row
    if members is None:
        fail(label, "-", "capabilities", err)
        return
    row["members"] = len(members)
    for mem in members:
        name, access = mem["name"], set(mem.get("access", []))
        if not mem.get("exposed", True):
            row["skipped_not_exposed"] += 1
            continue
        row["exposed"] += 1
        value, got = None, False
        if "get" in access or "set" in access:
            try:
                r = await m.get(path, cls, name)
                st = r.get("status")
                row["get"][st if st == "ok" else (r.get("code") or st)] += 1
                if st == "ok":
                    value, got = r.get("value"), True
                elif r.get("code") not in ALLOWED:
                    fail(label, name, "get", "%s %s" % (r.get("code"), r.get("detail")))
            except M4LError as e:
                row["get"]["exception"] += 1
                fail(label, name, "get", e)
        if "observe" in access:
            try:
                o = await m.observe(path, cls, name, ttl_seconds=20)
                if o.get("status") == "ok":
                    sid = o["subscription_id"]
                    p = await m.poll(sid)
                    u = await m.unobserve(sid)
                    ok = p.get("status") == "ok" and u.get("closed") is True
                    row["observe"]["ok" if ok else "bad_poll_or_close"] += 1
                    if not ok:
                        fail(label, name, "observe", "poll=%s unobserve=%s" % (p.get("status"), u))
                else:
                    row["observe"][o.get("code") or o.get("status")] += 1
                    if o.get("code") not in ALLOWED:
                        fail(label, name, "observe", "%s %s" % (o.get("code"), o.get("detail")))
            except M4LError as e:
                row["observe"]["exception"] += 1
                fail(label, name, "observe", e)
        if "set" in access:
            if got and jsonable(value):
                try:
                    s = await m.set(path, cls, name, value, write=False)
                    row["set_preview"][s.get("status") if s.get("status") in ("preview", "no_change") else (s.get("code") or s.get("status"))] += 1
                    if s.get("status") not in ("preview", "no_change") and s.get("code") not in ALLOWED \
                            and not (s.get("code") == "invalid_request" and any(x in str(s.get("detail")) for x in ALLOWED_DETAIL)):
                        fail(label, name, "set_preview", "%s %s" % (s.get("code"), s.get("detail")))
                    alt = alt_value(value)
                    if alt is not None:  # a DIFFERENT value: must answer preview (or a documented refusal) and change nothing
                        s2 = await m.set(path, cls, name, alt, write=False)
                        st2 = s2.get("status") if s2.get("status") == "preview" else (s2.get("code") or s2.get("status"))
                        row["set_alt"][st2] += 1
                        if s2.get("status") != "preview":
                            row["notes"].append((name, "set_alt", st2))
                            if s2.get("code") not in ALLOWED and not (s2.get("code") == "invalid_request" and any(
                                    x in str(s2.get("detail")) for x in ALLOWED_DETAIL)):
                                fail(label, name, "set_alt", "%s %s" % (s2.get("code"), s2.get("detail")))
                        back = (await m.get(path, cls, name)).get("value")
                        if back != value:
                            fail(label, name, "preview_changed_value", "%r -> %r" % (value, back))
                except M4LError as e:
                    row["set_preview"]["exception"] += 1
                    fail(label, name, "set_preview", e)
            else:
                row["set_preview"]["skipped_no_scalar_value"] += 1
        if "call" in access:
            try:
                argf = CALL_ARGS.get((cls, name))
                args = argf(CTX) if argf else []
                fp_before = await fingerprint(CTX["lc"]) if argf else None
                c = await m.call(path, cls, name, args, write=False)
                kind = "preview" if c.get("status") == "preview" else (c.get("code") or c.get("status"))
                if argf:
                    kind = "preview+args" if c.get("status") == "preview" else kind
                    fp_after = await fingerprint(CTX["lc"])
                    if fp_before != fp_after:
                        fail(label, name, "call_preview_executed", fp_diff(fp_before, fp_after))
                    elif c.get("status") != "preview":
                        fail(label, name, "call_preview_with_args", "%s %s" % (c.get("code"), c.get("detail")))
                row["call_preview"][kind] += 1
                if c.get("status") != "preview":
                    row["notes"].append((name, "call_preview", c.get("code") or c.get("status")))
                    readonly = name.startswith(READONLY_PREFIXES) and c.get("status") == "ok"
                    no_args_rejected = (not argf and c.get("code") == "invalid_request"
                                        and "documented signature" in str(c.get("detail")))
                    if not (readonly or no_args_rejected or c.get("code") in ALLOWED or argf):
                        fail(label, name, "call_preview", "%s %s" % (c.get("code"), c.get("detail")))
            except M4LError as e:
                row["call_preview"]["exception"] += 1
                fail(label, name, "call_preview", e)
    print("%-24s m=%-3d exp=%-3d get=%s obs=%s set=%s alt=%s call=%s" % (
        label[:24], row["members"], row["exposed"], dict(row["get"]), dict(row["observe"]),
        dict(row["set_preview"]), dict(row["set_alt"]), dict(row["call_preview"])), flush=True)


async def real_write(m: M4LClient, lc: LiveClient, label, path, cls, member, new, rs_path, rs_prop):
    """Write through M4L, read back through M4L and the Remote Script, restore, verify the restore."""
    before = (await m.get(path, cls, member)).get("value")
    w = await m.set(path, cls, member, new, write=True)
    via_m4l = (await m.get(path, cls, member)).get("value")
    via_rs = (await lc.request("lom.get", {"path": rs_path, "props": [rs_prop]}))[rs_prop]
    await m.set(path, cls, member, before, write=True)
    after = (await m.get(path, cls, member)).get("value")

    def same(a, b):
        try:
            return abs(float(a) - float(b)) < 1e-4
        except (TypeError, ValueError):
            return str(a).strip('"') == str(b).strip('"')

    ok = w.get("status") == "ok" and same(via_m4l, new) and same(via_rs, new) and same(after, before)
    OUT["writes"].append({"what": label, "before": before, "new": new, "m4l": via_m4l, "remote_script": via_rs,
                          "restored": after, "ok": ok})
    print(("PASS " if ok else "FAIL ") + "real write %-18s %r -> %r (M4L %r, RS %r) restored %r" % (
        label, before, new, via_m4l, via_rs, after), flush=True)
    if not ok:
        fail(label, member, "real_write", OUT["writes"][-1])


async def main(json_path: Path | None, fixture: Path | None, sample: Path | None) -> int:
    fixture_wav = acceptance_audio(sample, json_path.resolve().parent if json_path else Path.cwd())
    lc = LiveClient()
    await lc.connect()
    state = await lc.request("lom.get", {"path": "live_set", "props": [
        "file_path", "is_playing", "record_mode", "session_record", "start_time", "current_song_time",
    ]})
    actual = Path(state["file_path"]).resolve() if state.get("file_path") else None
    if (not actual or not (actual.stem.startswith("ZZ-") or actual.stem.endswith(" ZZ-acceptance-copy"))
            or (fixture is not None and actual != fixture.resolve(strict=True))
            or any(state.get(flag) is not False for flag in ("is_playing", "record_mode", "session_record"))):
        await lc.close()
        raise RuntimeError("Open a stopped, saved disposable ZZ- fixture first")
    names = ["ZZ-SW M4L", "ZZ-SW MIDI", "ZZ-SW AUDIO"]
    await lc.request("track.create", {"kind": "midi", "name": names[0]})
    await lc.request("track.create", {"kind": "midi", "name": names[1]})
    await lc.request("track.create", {"kind": "audio", "name": names[2]})
    ov = await lc.request("overview", {"fresh": True})
    idx = {t["name"]: t["index"] for t in ov["tracks"]}
    mi, ti, ai = idx[names[0]], idx[names[1]], idx[names[2]]
    n_scenes0 = len(ov["scenes"])
    fp0 = None
    added_cue = None
    try:
        await lc.request("browser.load", {"track": mi, "query": "LOM_API_Bridge"}, timeout=60)
        await lc.request("device.insert", {"name": "Reverb", "track": ti})
        devs = (await lc.request("track.info", {"track": ti, "clips": False}))["devices"]
        rv = next(d["index"] for d in devs if d["name"] == "Reverb")
        CTX.update(lc=lc, ti=ti, ai=ai)
        await lc.request("clip.create", {"track": ti, "slot": 0, "length": 4, "name": "ZZ-SW clip",
                                         "notes": [{"pitch": 60, "start_time": 0, "duration": 1}]})
        await lc.request("arrangement.create_midi_clip", {"track": ti, "start_time": 4000.0, "length": 4.0,
                                                           "name": "ZZ-SW arr clip"})
        await lc.request("sample.load", {"path": fixture_wav, "target": "session", "track": ai, "slot": 0})
        await lc.request("scene.create", {"index": -1, "name": "ZZ-SW scene"})
        scene_i = len((await lc.request("overview", {"fresh": True}))["scenes"]) - 1
        if not await lc.request("cue", {"action": "list"}):
            await lc.request("transport", {"action": "seek", "value": 4.0})
            await lc.request("cue", {"action": "toggle", "time": 4.0})
            created = await lc.request("cue", {"action": "list"})
            if len(created) != 1:
                raise RuntimeError("Cue fixture was not created")
            added_cue = created[0]["time"]
            await lc.request("cue", {"action": "rename", "index": 0, "name": "ZZ-SW Cue"})
        CTX["scene_i"] = scene_i
        fp0 = await fingerprint(lc)
        m = M4LClient()
        for _ in range(60):
            try:
                await m.readiness()
                break
            except M4LError:
                await asyncio.sleep(0.25)
        t0 = time.perf_counter()
        targets = [("live_set", "Song", "Song"), ("live_set view", "Song.View", "Song.View"),
                   (f"live_set tracks {ti}", "Track", "Track (MIDI)"), (f"live_set tracks {ai}", "Track", "Track (audio)"),
                   ("live_set master_track", "Track", "Track (master)"), ("live_set return_tracks 0", "Track", "Track (return)"),
                   (f"live_set tracks {ti} mixer_device", "MixerDevice", "MixerDevice"),
                   (f"live_set tracks {ti} mixer_device volume", "DeviceParameter", "DeviceParameter (volume)"),
                   (f"live_set tracks {ti} devices {rv}", "Device", "Device (Reverb)"),
                   (f"live_set tracks {ti} devices {rv} parameters 1", "DeviceParameter", "DeviceParameter (Reverb)"),
                   (f"live_set scenes {scene_i}", "Scene", "Scene"), (f"live_set tracks {ti} clip_slots 0", "ClipSlot", "ClipSlot"),
                   (f"live_set tracks {ti} clip_slots 0 clip", "Clip", "Clip (session MIDI)"),
                   (f"live_set tracks {ai} clip_slots 0 clip", "Clip", "Clip (session audio)"),
                   (f"live_set tracks {ti} arrangement_clips 0", "Clip", "Clip (arrangement)"),
                   ("live_set cue_points 0", "CuePoint", "CuePoint"), ("live_app", "Application", "Application"),
                   ("live_set groove_pool", "GroovePool", "GroovePool")]
        for path, cls, label in targets:
            try:
                await sweep_target(m, path, cls, label)
            except Exception as e:  # noqa: BLE001 - keep sweeping; the failure is recorded
                fail(label, "-", "sweep", repr(e))
        print("sweep time: %.1f s" % (time.perf_counter() - t0))
        # real writes (fixtures only), read back through both backends, restored
        await real_write(m, lc, "Track.name", f"live_set tracks {ti}", "Track", "name", "ZZ-SW MIDI renamed",
                         f"live_set tracks {ti}", "name")
        await real_write(m, lc, "Track.mute", f"live_set tracks {ti}", "Track", "mute", 1,
                         f"live_set tracks {ti}", "mute")
        await real_write(m, lc, "Clip.name", f"live_set tracks {ti} clip_slots 0 clip", "Clip", "name", "ZZ-SW renamed",
                         f"live_set tracks {ti} clip_slots 0 clip", "name")
        await real_write(m, lc, "Scene.name", f"live_set scenes {scene_i}", "Scene", "name", "ZZ-SW scene renamed",
                         f"live_set scenes {scene_i}", "name")
        pp = f"live_set tracks {ti} devices {rv} parameters 9"
        pr = await lc.request("lom.get", {"path": pp, "props": ["name", "value", "max"]})
        await real_write(m, lc, "Reverb " + pr["name"], pp, "DeviceParameter", "value",
                         round(float(pr["value"]) * 0.5 + 0.01, 4), pp, "value")
        fp1 = await fingerprint(lc)
        same = fp0 == fp1
        print(("PASS" if same else "FAIL") + "  song fingerprint unchanged by the whole sweep (previews wrote nothing)")
        if not same:
            fail("Song", "-", "fingerprint", fp_diff(fp0, fp1))
            print("   fingerprint diff:", fp_diff(fp0, fp1))
    finally:
        if added_cue is not None:
            current = await lc.request("cue", {"action": "list"})
            if len(current) == 1 and current[0]["time"] == added_cue and current[0]["name"] == "ZZ-SW Cue":
                await lc.request("transport", {"action": "seek", "value": added_cue})
                await lc.request("cue", {"action": "toggle", "time": added_cue})
            if await lc.request("cue", {"action": "list"}):
                fail("CuePoint", "-", "cleanup", "The owned cue could not be removed")
            await lc.request("lom.set", {"path": "live_set", "values": {"start_time": state["start_time"]}})
            await lc.request("lom.set", {"path": "live_set", "values": {
                "current_song_time": state["current_song_time"],
            }})
        ov = await lc.request("overview", {"fresh": True})
        for t in sorted([t["index"] for t in ov["tracks"] if t["name"].startswith("ZZ-SW")], reverse=True):
            await lc.request("lom.call", {"path": "live_set", "method": "delete_track", "args": [t]})
        for i in range(len((await lc.request("overview", {"fresh": True}))["scenes"]) - 1, n_scenes0 - 1, -1):
            await lc.request("scene.delete", {"index": i})
        await lc.close()
    tot = Counter()
    for r in OUT["classes"].values():
        tot["members"] += r["members"]
        tot["exposed"] += r["exposed"]
        for k in ("get", "observe", "set_preview", "set_alt", "call_preview"):
            tot[k] += sum(r[k].values())
    report = {"totals": dict(tot), "failures": OUT["failures"], "writes": OUT["writes"],
              "classes": {k: {kk: (dict(vv) if isinstance(vv, Counter) else vv) for kk, vv in v.items()}
                          for k, v in OUT["classes"].items()}}
    print("\nTOTAL: %s | failures: %d | real writes ok: %d/%d" % (
        dict(tot), len(OUT["failures"]), sum(1 for w in OUT["writes"] if w["ok"]), len(OUT["writes"])))
    for f in OUT["failures"][:40]:
        print("   FAIL", f)
    if json_path:
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return 0 if not OUT["failures"] else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--fixture", type=Path, default=os.environ.get("ZZ_TEST_FIXTURE_FILE"))
    ap.add_argument("--sample", type=Path, default=os.environ.get("ZZ_TEST_FIXTURE_WAV"))
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.json, args.fixture, args.sample)))
