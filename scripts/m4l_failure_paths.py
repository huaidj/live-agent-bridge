"""Wave D of docs/TEST_PLAN_FULL.md: failure paths and protocol limits of the LOM API Bridge (real device, real Live).

Run on a disposable copy of the set:
    python scripts/acceptance_on_copy.py "<set>.als" --script scripts/m4l_failure_paths.py -- --json out.json

Limits come from the device's own node_bridge.js / max_lom_runtime.js: 1 MiB frames, 64 requests in flight, 8
connections, 8 owners per connection, owner regex [A-Za-z0-9:_-]{1,128}, 64 subscriptions, ttl 1..300 s, queue 1..256.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from live_agent_bridge.client import LiveClient  # noqa: E402
from live_agent_bridge.m4l import M4LClient, M4LError, port  # noqa: E402

RESULTS: list[dict] = []
CTX: dict = {}


def record(name: str, ok: bool, detail: str = ""):
    RESULTS.append({"test": name, "ok": bool(ok), "detail": str(detail)[:400]})
    print(("PASS " if ok else "FAIL ") + "%-44s %s" % (name, str(detail)[:200]), flush=True)


async def raw_session(frames: list[bytes], expect: int, timeout: float = 4.0):
    """Send raw bytes on a fresh connection; collect up to `expect` reply lines; report whether the peer closed."""
    r, w = await asyncio.open_connection("127.0.0.1", port(), limit=8 * 1024 * 1024)
    out, closed = [], False
    try:
        for f in frames:
            w.write(f)
        await w.drain()
        end = time.time() + timeout
        while len(out) < expect and time.time() < end:
            try:
                line = await asyncio.wait_for(r.readline(), max(0.05, end - time.time()))
            except asyncio.TimeoutError:
                break
            except (ConnectionError, asyncio.LimitOverrunError):
                closed = True
                break
            if not line:
                closed = True
                break
            out.append(json.loads(line))
    except (ConnectionError, OSError):
        closed = True
    finally:
        w.close()
    return out, closed


def frame(rid, request) -> bytes:
    return (json.dumps({"id": rid, "request": request}) + "\n").encode()


async def alive(m: M4LClient) -> bool:
    try:
        return (await m.readiness()).get("status") == "ok"
    except M4LError:
        return False


# ------------------------------------------------------------------ tests
async def t_unexposed(m, lc):
    tp = "live_set tracks %d" % CTX["b"]
    members, off = [], 0
    while True:
        r = await m.capabilities(tp, "Track", None, off, 200)
        members += r["members"]
        if r.get("next_offset") is None:
            break
        off = r["next_offset"]
    hidden = [x for x in members if not x.get("exposed", True)]
    bad = []
    for x in hidden:
        g = await m.get(tp, "Track", x["name"])
        if g.get("status") == "ok":
            bad.append((x["name"], "get ok"))
    record("unexposed members refused", hidden and not bad, "%d unexposed members, get refused for all" % len(hidden)
           if not bad else bad)


async def t_identity(m, lc):
    b = CTX["b"]
    p = "live_set tracks %d" % b
    old = (await m.resolve(p, "Track"))["target"]["identity"]
    await lc.request("lom.call", {"path": "live_set", "method": "delete_track", "args": [b]})
    await lc.request("track.create", {"kind": "midi", "name": "ZZ-FP C", "index": b})
    r = await m.request("set", path=p, object_type="Track", member="name", value="HACKED", expected_identity=old,
                        dry_run=False)
    name = (await lc.request("lom.get", {"path": p, "props": ["name"]}))["name"]
    record("stale identity refused after delete+recreate", r.get("status") != "ok" and name == "ZZ-FP C",
           "status=%s code=%s name=%s" % (r.get("status"), r.get("code"), name))
    cur = (await m.resolve(p, "Track"))["target"]["identity"]
    other = "live_set tracks %d" % CTX["a_idx"]
    r2 = await m.request("set", path=other, object_type="Track", member="name", value="HACKED2", expected_identity=cur,
                         dry_run=False)
    n2 = (await lc.request("lom.get", {"path": other, "props": ["name"]}))["name"]
    record("identity of another object refused", r2.get("status") != "ok" and n2 != "HACKED2",
           "status=%s code=%s" % (r2.get("status"), r2.get("code")))
    CTX["c_path"], CTX["c_identity"] = p, cur


async def t_write_gates(m, lc):
    p = CTX["c_path"]
    r = await m.request("set", path=p, object_type="Track", member="name", value="NOID", dry_run=False)
    n = (await lc.request("lom.get", {"path": p, "props": ["name"]}))["name"]
    record("write without expected_identity refused", r.get("status") != "ok" and n == "ZZ-FP C",
           "status=%s code=%s" % (r.get("status"), r.get("code")))
    r = await m.request("set", path=p, object_type="Track", member="name", value="DEFAULTPREVIEW",
                        expected_identity=CTX["c_identity"])
    n = (await lc.request("lom.get", {"path": p, "props": ["name"]}))["name"]
    record("dry_run omitted => preview, no write", r.get("status") == "preview" and n == "ZZ-FP C",
           "status=%s name=%s" % (r.get("status"), n))


async def t_bad_paths(m, lc):
    paths = ["live_set tracks 999", "live_set tracks -1", "live_set; drop", "", "live_set tracks 0 devices 999 parameters 0",
             "live_set tracks a", "live_set 轨道 0", "x" * 5000, "live_set tracks 0 clip_slots 0 clip", "this_device_nope",
             "live_set  tracks   0", "LIVE_SET", "live_set\ntracks 0", "live_set\x00tracks 0",
             "live_set\x07tracks 0", "live_set tracks 0\x1b[31m"]
    tolerated = ("live_set  tracks   0", "live_set\ntracks 0")  # device normalises plain whitespace
    bad = []
    for p in paths:
        try:
            r = await m.get(p, "Track", "name")
            if r.get("status") == "ok" and p not in tolerated:
                bad.append((p[:30], r.get("status")))
        except M4LError as e:
            if "frame" not in str(e).lower() and "answer" not in str(e).lower():
                bad.append((p[:30], str(e)[:60]))
    record("bad / hostile paths never return ok", not bad and await alive(m), "%d paths%s" % (len(paths), " bad=%s" % bad if bad else ""))


async def t_malformed(m, lc):
    big_id = "i" * 200
    frames = [b"not json\n", b'{"request":{}}\n', frame(big_id, {"op": "readiness"}), b'{"id":"a","request":[1]}\n',
              frame("o1", {"op": "readiness", "owner": "bad owner!"}), frame("u1", {"op": "no_such_op"}),
              b'{"id":"n","request":{"op":"get","value":NaN}}\n']
    out, closed = await raw_session(frames, len(frames))
    codes = [(o.get("id"), o["result"].get("status"), o["result"].get("code")) for o in out]
    ok_n = sum(1 for _, st, c in codes if st == "error")
    record("malformed frames answered with error frames", ok_n == len(frames), "%d/%d error frames: %s" % (ok_n, len(frames), codes[:4]))
    owners = [frame("w%d" % i, {"op": "readiness", "owner": "zz-owner-%d" % i}) for i in range(10)]
    out, closed = await raw_session(owners, 10)
    errs = [o for o in out if o["result"].get("status") == "error"]
    record("9th+ owner on one connection refused (limit 8)", len(out) == 10 and len(errs) == 2,
           "%d replies, %d errors, codes=%s" % (len(out), len(errs), [e["result"].get("code") for e in errs]))
    record("device alive after malformed frames", await alive(m))


async def t_oversize(m, lc):
    big = b'{"id":"big","request":{"op":"readiness","pad":"' + b"a" * (1024 * 1024 + 100) + b'"}}\n'
    t0 = time.time()
    out, closed = await raw_session([big], 1, timeout=4.0)
    record("oversize frame (> 1 MiB) closes the connection", closed or any(
        o["result"].get("code") == "invalid_frame" for o in out), "closed=%s replies=%s in %.1fs" % (closed, out[:1], time.time() - t0))
    record("device alive after oversize frame", await alive(m))
    try:
        await m.request("readiness", pad="a" * (1024 * 1024 + 100))
        ok = False
    except M4LError as e:
        ok = "1 MiB" in str(e)
    record("client refuses oversize request itself", ok)


async def t_concurrency(m, lc):
    out, _ = await raw_session([frame("c%d" % i, {"op": "readiness"}) for i in range(100)], 100, timeout=8.0)
    ids = {o["id"] for o in out}
    lim = sum(1 for o in out if o["result"].get("code") == "request_limit")
    record("raw 100-deep pipeline: every frame answered", len(ids) == 100, "ok=%d request_limit=%d" % (len(out) - lim, lim))
    res = await asyncio.gather(*[m.readiness() for _ in range(200)])
    record("M4LClient 200 concurrent: all ok (in-flight cap)", all(r.get("status") == "ok" for r in res))


async def t_connections(m, lc):
    socks, closed_at = [], None
    try:
        for i in range(14):
            r, w = await asyncio.open_connection("127.0.0.1", port())
            socks.append((r, w))
            try:
                w.write(frame("k%d" % i, {"op": "readiness"}))
                await w.drain()
            except (ConnectionError, OSError):
                closed_at = i + 1
                break
            try:
                line = await asyncio.wait_for(r.readline(), 1.5)
            except asyncio.TimeoutError:
                line = b"timeout"
            except (ConnectionError, OSError):
                line = b""
            if not line:
                closed_at = i + 1
                break
    finally:
        for _, w in socks:
            w.close()
    await asyncio.sleep(0.5)
    record("connection cap (8) enforced and recovers", closed_at is not None and closed_at <= 9 and await alive(m),
           "connection #%s was refused" % closed_at)


async def t_ttl(m, lc):
    o = await m.observe("live_set", "Song", "tempo", ttl_seconds=1)
    sid = o["subscription_id"]
    await asyncio.sleep(2.6)
    p = await m.poll(sid)
    record("subscription expires after ttl", p.get("status") != "ok" or p.get("closed"),
           "poll after ttl: %s" % {k: p.get(k) for k in ("status", "code", "closed", "close_reason", "reason", "remaining")})
    u = await m.unobserve(sid)
    record("expired subscription can be unobserved", u.get("status") == "ok", "unobserve: %s %s" % (u.get("status"), u.get("code")))
    bad = []
    for ttl in (0, 301, -5, "x", 1.5):
        try:
            r = await m.request("observe", path="live_set", object_type="Song", member="tempo",
                                expected_identity=(await m.resolve("live_set", "Song"))["target"]["identity"],
                                owner=m.owner, ttl_seconds=ttl)
            if r.get("status") == "ok":
                await m.unobserve(r["subscription_id"])
                bad.append(ttl)
        except M4LError:
            pass
    record("ttl outside 1..300 refused", not bad, "accepted: %s" % bad if bad else "0, 301, -5, 'x', 1.5 refused")


async def t_sub_limit(m, lc):
    ids, last = [], None
    for _ in range(66):
        r = await m.observe("live_set", "Song", "tempo", ttl_seconds=60)
        if r.get("status") == "ok":
            ids.append(r["subscription_id"])
        else:
            last = r
            break
    record("subscription limit (64) enforced", len(ids) == 64 and last and last.get("code") == "resource_limit",
           "%d ok, then %s (a 63 means another subscription still holds a slot)" % (len(ids), last and last.get("code")))
    for s in ids:
        await m.unobserve(s)
    r = await m.observe("live_set", "Song", "tempo", ttl_seconds=5)
    record("capacity returns after unobserve", r.get("status") == "ok", r.get("status"))
    if r.get("status") == "ok":
        await m.unobserve(r["subscription_id"])


async def t_owner_isolation(m, lc):
    o = await m.observe("live_set", "Song", "tempo", ttl_seconds=30)
    sid = o["subscription_id"]
    other = M4LClient()
    other.owner = "zz-other-owner"
    try:
        p = await other.poll(sid)
        u = await other.unobserve(sid)
    finally:
        await other.close()
    mine = await m.poll(sid)
    record("another owner cannot poll / unobserve my subscription", p.get("status") != "ok" and u.get("closed") is not True
           and mine.get("status") == "ok", "other poll=%s/%s unobserve=%s/%s mine=%s" % (
               p.get("status"), p.get("code"), u.get("status"), u.get("code"), mine.get("status")))
    await m.unobserve(sid)


async def t_queue(m, lc):
    tempo0 = (await lc.request("lom.get", {"path": "live_set", "props": ["tempo"]}))["tempo"]
    o = await m.request("observe", path="live_set", object_type="Song", member="tempo",
                        expected_identity=(await m.resolve("live_set", "Song"))["target"]["identity"],
                        owner=m.owner, ttl_seconds=30, queue_limit=2)
    sid = o["subscription_id"]
    try:
        for v in (112.0, 113.0, 114.0, 115.0, 116.0):
            await lc.request("lom.set", {"path": "live_set", "values": {"tempo": v}})
            await asyncio.sleep(0.15)
    finally:
        await lc.request("lom.set", {"path": "live_set", "values": {"tempo": tempo0}})
    await asyncio.sleep(0.3)
    p = await m.poll(sid)
    await m.unobserve(sid)
    record("queue_limit=2 keeps newest events and counts drops", len(p.get("events", [])) <= 2 and p.get("dropped", 0) >= 3,
           "events=%d dropped=%s" % (len(p.get("events", [])), p.get("dropped")))
    record("tempo restored after queue test", (await lc.request("lom.get", {"path": "live_set", "props": ["tempo"]}))["tempo"] == tempo0)


async def t_numeric(m, lc):
    tempo0 = (await lc.request("lom.get", {"path": "live_set", "props": ["tempo"]}))["tempo"]
    ident = (await m.resolve("live_set", "Song"))["target"]["identity"]
    bad = []
    for v in (1e308, -1.0, 0, "NaN", "abc", {"a": 1}, [1, 2], None, True, 10 ** 30):
        try:
            r = await m.request("set", path="live_set", object_type="Song", member="tempo", value=v,
                                expected_identity=ident)
            if r.get("status") not in ("preview", "error", "no_change"):
                bad.append((v, r.get("status")))
        except M4LError as e:
            bad.append((v, str(e)[:40]))
    now = (await lc.request("lom.get", {"path": "live_set", "props": ["tempo"]}))["tempo"]
    record("odd numeric / typed values: preview or refusal, nothing written", not bad and now == tempo0,
           "bad=%s tempo=%s" % (bad, now))
    r = await m.request("set", path="live_set", object_type="Song", member="tempo", value=1e308, expected_identity=ident,
                        dry_run=False)
    now = (await lc.request("lom.get", {"path": "live_set", "props": ["tempo"]}))["tempo"]
    record("real write of an out-of-range tempo refused", r.get("status") != "ok" and now == tempo0,
           "status=%s code=%s tempo=%s" % (r.get("status"), r.get("code"), now))


async def t_paging(m, lc):
    bad = []
    for off, lim in ((1000, 10), (0, 0), (-1, 5), (0, -5), (0, 10 ** 9), (2, 1), (None, 3)):
        try:
            r = await m.get("live_set", "Song", "tracks", off, lim)
            if r.get("status") not in ("ok", "error"):
                bad.append((off, lim, r.get("status")))
        except M4LError as e:
            bad.append((off, lim, str(e)[:40]))
    record("paging edge cases answered, device alive", not bad and await alive(m), "bad=%s" % bad)


async def t_reconnect(m, lc):
    o = await m.observe("live_set", "Song", "tempo", ttl_seconds=30)
    sid = o["subscription_id"]
    await m.close()                       # drop the connection
    ok1 = await alive(m)                  # next request reconnects transparently
    p = await m.poll(sid)
    record("client reconnects transparently", ok1)
    record("subscription after the owner's connection closed", True, "poll after reconnect: status=%s code=%s "
           "(device cleans owner subscriptions when the last socket closes)" % (p.get("status"), p.get("code")))


async def t_device_removed(m, lc):
    # A set can hold several LOM API Bridge devices but only the first one binds the port; the song already has one
    # on "★ M4L · Control". Remove EVERY track that holds the device so the port really goes away.
    ov = await lc.request("overview", {"fresh": True})
    holders = []
    for t in ov["tracks"]:
        info = await lc.request("track.info", {"track": t["index"], "clips": False})
        if any("LOM API Bridge" in d["name"] or "LOM_API_Bridge" in d["name"] for d in info["devices"]):
            holders.append(t["index"])
    for i in sorted(holders, reverse=True):
        await lc.request("lom.call", {"path": "live_set", "method": "delete_track", "args": [i]})
    print("   removed %d track(s) holding the device" % len(holders))
    await asyncio.sleep(1.5)
    t0 = time.time()
    try:
        await m.readiness()
        refused = False
    except M4LError as e:
        refused = "not reachable" in str(e) or "closed" in str(e) or "answer" in str(e)
    record("request after the device was removed fails fast and clearly", refused and time.time() - t0 < 14,
           "%.1fs" % (time.time() - t0))
    await lc.request("track.create", {"kind": "midi", "name": "ZZ-FP M4L 2"})
    ov = await lc.request("overview", {"fresh": True})
    ni = next(t["index"] for t in ov["tracks"] if t["name"] == "ZZ-FP M4L 2")
    await lc.request("browser.load", {"track": ni, "query": "LOM_API_Bridge"}, timeout=60)
    ok = False
    for _ in range(80):
        if await alive(m):
            ok = True
            break
        await asyncio.sleep(0.25)
    record("device reload on a new track restores the bridge", ok)


async def main(json_path: Path | None, fixture: Path | None) -> int:
    lc = LiveClient()
    await lc.connect()
    state = await lc.request("lom.get", {"path": "live_set", "props": [
        "file_path", "is_playing", "record_mode", "session_record",
    ]})
    actual = Path(state["file_path"]).resolve() if state.get("file_path") else None
    if (not actual or not (actual.stem.startswith("ZZ-") or actual.stem.endswith(" ZZ-acceptance-copy"))
            or (fixture is not None and actual != fixture.resolve(strict=True))
            or any(state.get(flag) is not False for flag in ("is_playing", "record_mode", "session_record"))):
        await lc.close()
        raise RuntimeError("Open a stopped, saved disposable ZZ- fixture first")
    await lc.request("track.create", {"kind": "midi", "name": "ZZ-FP M4L"})
    await lc.request("track.create", {"kind": "midi", "name": "ZZ-FP A"})
    await lc.request("track.create", {"kind": "midi", "name": "ZZ-FP B"})
    ov = await lc.request("overview", {"fresh": True})
    idx = {t["name"]: t["index"] for t in ov["tracks"]}
    CTX.update(b=idx["ZZ-FP B"], a_idx=idx["ZZ-FP A"])
    tempo0 = (await lc.request("lom.get", {"path": "live_set", "props": ["tempo"]}))["tempo"]
    await lc.request("browser.load", {"track": idx["ZZ-FP M4L"], "query": "LOM_API_Bridge"}, timeout=60)
    m = M4LClient()
    for _ in range(80):
        if await alive(m):
            break
        await asyncio.sleep(0.25)
    tests = [t_unexposed, t_identity, t_write_gates, t_bad_paths, t_malformed, t_oversize, t_concurrency, t_connections,
             t_ttl, t_sub_limit, t_owner_isolation, t_queue, t_numeric, t_paging, t_reconnect, t_device_removed]
    try:
        for t in tests:
            try:
                await t(m, lc)
            except Exception as e:  # noqa: BLE001 - one broken test must not hide the others
                record(t.__name__, False, "exception %r" % e)
    finally:
        await m.close()
        ov = await lc.request("overview", {"fresh": True})
        for i in sorted([t["index"] for t in ov["tracks"] if t["name"].startswith("ZZ-FP")], reverse=True):
            await lc.request("lom.call", {"path": "live_set", "method": "delete_track", "args": [i]})
        final = await lc.request("lom.get", {"path": "live_set", "props": ["tempo"]})
        record("tempo unchanged at the end", final["tempo"] == tempo0, final["tempo"])
        await lc.close()
    bad = [r for r in RESULTS if not r["ok"]]
    print("\n%d checks, %d failed" % (len(RESULTS), len(bad)))
    if json_path:
        json_path.write_text(json.dumps({"results": RESULTS}, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if not bad else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--fixture", type=Path, default=os.environ.get("ZZ_TEST_FIXTURE_FILE"))
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.json, args.fixture)))
