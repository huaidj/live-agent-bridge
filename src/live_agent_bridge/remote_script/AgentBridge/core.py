"""Main-thread scheduler. Framework-free so it can be tested without Live.

Latency model
-------------
The Live API may only be touched on Live's main thread, which gives a Remote
Script control only on its timer ticks (~10 Hz via update_display). The old
bridges paid one full tick *per request* and serialized every request behind a
lock. Here:

* ticks come from Live.Base.Timer when available (~80+ Hz instead of 10 Hz);
* sockets are polled inside the tick (no threads: Live's Python only yields
  the GIL while the main thread runs Python), so a request is read, executed
  and answered within one tick;
* every queued request is drained in the same tick (pipelined clients get N
  answers for the price of one tick);
* ``batch`` runs many ops in one tick and one undo step;
* a heartbeat lets clients detect a stalled main thread (modal dialog) fast,
  and requests carry an expiry so they are never executed late after a stall;
* slow work (browser indexing) is time-sliced so it never freezes Live.
"""

import sys
import time
from collections import deque

from . import browser as _browser  # noqa: F401  (registers browser ops)
from . import ops_ext as _ops_ext  # noqa: F401  (registers the extended API ops)
from . import plugins as _plugins  # noqa: F401  (third-party plug-in ops)

# The running entry module's hot-reload list predates plugins.py: reload it here so its ops
# register into the freshly reloaded OPS table.
import importlib as _importlib  # noqa: E402

_importlib.reload(_plugins)
from .browser import BrowserIndex
from .events import Events
from .lom import Lom
from .ops import OPS, track_summary
from .transport import Server

VERSION = "1.0.0"
PROTOCOL = 1

DEFAULT_PORT = 9890
TICK_BUDGET = 0.030          # seconds of main-thread work per tick
HEARTBEAT = 0.5              # seconds between heartbeats to connected clients
DEFAULT_TIMEOUT = 30.0
SNAPSHOT_MIN_INTERVAL = 0.25
# Ops whose purpose is to change what the user sees; everything else runs "in the
# background" and the user's selection is restored afterwards (Live auto-selects
# e.g. newly created tracks/scenes).
UI_OPS = frozenset(("view.select", "view.set", "view.show"))
UI_ACTIONS = {"device.control": ("select", "select_instrument"),
              "app": ("show_view", "hide_view", "focus_view", "scroll_view", "zoom_view", "toggle_browse")}
_UI_ATTRS = ("selected_track", "selected_scene", "highlighted_clip_slot")
UI_GUARD_WINDOW = 0.3   # hold the whole selection briefly after any background write
TOUCHED_GUARD_WINDOW = 3.0  # undo Live's delayed auto-select of the track the op worked on


class Job(object):
    __slots__ = ("cid", "rid", "gen", "t_in", "t_start", "ui", "touched", "op")

    def __init__(self, cid, rid, gen, t_in, t_start, ui=None):
        self.cid, self.rid, self.gen, self.t_in, self.t_start = cid, rid, gen, t_in, t_start
        self.ui = ui  # user selection to restore once the generator op finishes
        self.touched = None


class Core(object):
    def __init__(self, song_fn, app_fn, log=None, host="127.0.0.1", port=DEFAULT_PORT,
                 clock=time.perf_counter, live_version=None):
        self.song = song_fn
        self.app = app_fn
        self.log = log or (lambda *_: None)
        self.clock = clock
        self.lom = Lom(song_fn, app_fn)
        self.events = Events(self.lom, self._send_to)
        self.browser_index = BrowserIndex()
        self.inbox = deque()
        self.control = deque()
        self.jobs = deque()
        self.server = Server(host, port, self.on_request, self.on_connect, self.on_disconnect, self.log)
        self.live_version = live_version
        self.tick_mode = "update_display"
        self.last_tick = clock()
        self.tick_count = 0
        self.tick_interval = 0.1
        self.snapshot = None
        self.snapshot_at = 0.0
        self.snapshot_cost = 0.0
        self.snapshot_dirty = True
        self.stats = {"requests": 0, "fast_path": 0, "errors": 0, "max_exec_ms": 0.0}
        self.op_stats = {}
        self._last_heartbeat = 0.0
        self.hooks = {}  # set by the ControlSurface: reload(), timer(ms)

    # ---------------------------------------------------------- lifecycle
    def start(self):
        self.server.start()
        self.log("AgentBridge %s listening on %s:%d" % (VERSION, self.server.host, self.server.port))

    def stop(self):
        self.server.stop()
        self.events.close()
        self.lom.registry.clear()

    # ---------------------------------------------------------- socket I/O (polled in tick)
    def on_connect(self, client):
        client.send({"event": "hello", "bridge": VERSION, "protocol": PROTOCOL,
                     "live": self.live_version, "ops": sorted(OPS)})

    def on_disconnect(self, client):
        self.control.append(lambda: self.events.drop_client(client.cid))

    def on_request(self, client, msg):
        rid = msg.get("id")
        name = msg.get("op")
        now = self.clock()
        self.stats["requests"] += 1
        if name == "ping":
            self.stats["fast_path"] += 1
            client.send({"id": rid, "ok": True, "result": self._health(now)})
            return
        if name == "overview":
            args = msg.get("args") or {}
            snap = self.snapshot
            max_age = float(args.get("max_age", 1.0))
            if snap is not None and not self.snapshot_dirty and not args.get("fresh") \
                    and now - self.snapshot_at <= max_age:
                self.stats["fast_path"] += 1
                client.send({"id": rid, "ok": True, "result": snap,
                             "t": {"cached_age_ms": round((now - self.snapshot_at) * 1000, 1)}})
                return
        if name not in OPS:
            client.send({"id": rid, "ok": False,
                         "error": {"type": "UnknownOp", "message": "unknown op %r" % name}})
            return
        self.inbox.append((client.cid, msg, now))

    def _health(self, now):
        return {"pong": True, "bridge": VERSION, "live": self.live_version,
                "tick_age_ms": round((now - self.last_tick) * 1000, 1),
                "tick_hz": round(1.0 / self.tick_interval, 1) if self.tick_interval else None,
                "tick_mode": self.tick_mode, "queued": len(self.inbox), "jobs": len(self.jobs)}

    def _send_to(self, cid, message):
        client = self.server.get_client(cid)
        if client is not None:
            client.send(message)

    # ---------------------------------------------------------- main thread
    def tick(self):
        now = self.clock()
        if self.tick_count:
            dt = now - self.last_tick
            self.tick_interval = self.tick_interval * 0.9 + dt * 0.1
        self.last_tick = now
        self.tick_count += 1
        try:
            self.server.poll()
            self._tick_work(now)
        finally:
            if self.server.clients and now - self._last_heartbeat >= HEARTBEAT:
                self._last_heartbeat = now
                for c in list(self.server.clients.values()):
                    c.send({"event": "hb", "q": len(self.inbox), "jobs": len(self.jobs)})
            self.server.flush()

    def _tick_work(self, now):
        # Only run tasks queued before this tick: tasks may re-queue themselves for the next tick.
        for _ in range(len(self.control)):
            try:
                self.control.popleft()()
            except Exception as e:
                self.log("control task failed: %s" % e)
        if not (self.inbox or self.jobs or self.events.subs) and not self._snapshot_due(now):
            return
        deadline = now + TICK_BUDGET
        done = 0
        while self.inbox and (done == 0 or self.clock() < deadline):
            cid, msg, t_in = self.inbox.popleft()
            self._execute(cid, msg, t_in)
            done += 1
        while self.jobs and self.clock() < deadline:
            job = self.jobs.popleft()
            if not self._step(job, deadline):
                self.jobs.append(job)
        self.events.flush()
        if self._snapshot_due(self.clock()):
            self._refresh_snapshot()

    def _execute(self, cid, msg, t_in):
        rid = msg.get("id")
        name = msg.get("op")
        t0 = self.clock()
        timeout = float(msg.get("timeout") or DEFAULT_TIMEOUT)
        expires = msg.get("expires_at")  # wall clock, same machine as the client
        late = (t0 - t_in > timeout) or (expires is not None and time.time() > float(expires))
        if late:
            self._send_to(cid, {"id": rid, "ok": False, "error": {
                "type": "Expired", "message": "request arrived after its deadline (Live was busy); "
                                              "dropped without executing"}})
            return
        fn = OPS[name]
        args = dict(msg.get("args") or {})
        if name in ("observe", "unobserve"):
            args["_cid"] = cid
        changes_ui = name in UI_OPS or args.get("action") in UI_ACTIONS.get(name, ())
        keep_ui = getattr(fn, "mutates", False) and not changes_ui
        if getattr(fn, "mutates", False):
            self.snapshot_dirty = True
        ui = self._ui_state() if keep_ui else None
        if getattr(fn, "mutates", False):
            # Tell the client right now (before a possibly long op blocks this thread)
            # that the write has started, so a silent Live is never misreported as
            # "not executed".
            client = self.server.get_client(cid)
            if client is not None:
                client.send({"ack": rid})
                client._flush()
        try:
            result = fn(self, **args)
        except Exception as e:
            self._restore_ui(ui, touched=self._touched_track(args) if ui else None)
            self._fail(cid, rid, e, t_in, t0, op=name)
            return
        if hasattr(result, "send") and hasattr(result, "throw"):
            job = Job(cid, rid, result, t_in, t0, ui)
            job.touched = self._touched_track(args) if ui else None
            job.op = name
            if not self._step(job, t0 + TICK_BUDGET):
                self.jobs.append(job)
            return
        self._restore_ui(ui, touched=self._touched_track(args) if ui else None)
        self._reply(cid, rid, result, t_in, t0, op=name)

    def _ui_state(self):
        try:
            v = self.song().view
            return [(a, getattr(v, a, None)) for a in _UI_ATTRS]
        except Exception:
            return None

    def _touched_track(self, args):
        """The track an op worked on, if its args name one (for the delayed-select guard)."""
        ref = args.get("track")
        if ref is None:
            return None
        try:
            from .ops import track_of
            return track_of(self, ref)
        except Exception:
            return None

    def _restore_ui(self, state, until=None, touched=None, touched_until=None):
        if not state:
            return
        # Live applies some selection changes on later UI cycles (e.g. ~0.6 s after replacing
        # a plug-in instrument it selects that track). Hold the whole selection for a short
        # window, then for a longer window only undo jumps to the track the op touched, so a
        # user clicking elsewhere is never overridden.
        now = self.clock()
        if until is None:
            until = now + UI_GUARD_WINDOW
            touched_until = now + TOUCHED_GUARD_WINDOW
        v = self.song().view
        if now >= until:
            try:
                if touched is None or v.selected_track != touched:
                    if touched is not None and now < touched_until:
                        self.control.append(lambda: self._restore_ui(state, until, touched, touched_until))
                    return
            except Exception:
                return
        if now < max(until, touched_until if touched is not None else 0):
            self.control.append(lambda: self._restore_ui(state, until, touched, touched_until))
        for attr, val in state:
            if val is None:
                continue
            try:
                if getattr(v, attr) != val:
                    setattr(v, attr, val)
            except Exception:
                pass  # the object was deleted by the op

    def _step(self, job, deadline):
        """Advance a generator op. Returns True when it finished."""
        try:
            while True:
                next(job.gen)
                if self.clock() >= deadline:
                    return False
        except StopIteration as stop:
            self._restore_ui(job.ui, touched=job.touched)
            self._reply(job.cid, job.rid, stop.value, job.t_in, job.t_start, op=job.op)
        except Exception as e:
            self._restore_ui(job.ui, touched=job.touched)
            self._fail(job.cid, job.rid, e, job.t_in, job.t_start, op=job.op)
        return True

    def _timing(self, t_in, t0, op=None):
        t1 = self.clock()
        x = (t1 - t0) * 1000
        if x > self.stats["max_exec_ms"]:
            self.stats["max_exec_ms"] = round(x, 2)
        if op is not None:
            # per-op [count, total_ms, max_ms]: lets an agent see which ops are slow in this set
            agg = self.op_stats.get(op)
            if agg is None:
                agg = self.op_stats[op] = [0, 0.0, 0.0]
            agg[0] += 1
            agg[1] += x
            if x > agg[2]:
                agg[2] = x
        return {"queue_ms": round((t0 - t_in) * 1000, 2), "exec_ms": round(x, 2)}

    def _reply(self, cid, rid, result, t_in, t0, op=None):
        self._send_to(cid, {"id": rid, "ok": True, "result": result, "t": self._timing(t_in, t0, op)})

    def _fail(self, cid, rid, e, t_in, t0, op=None):
        self.stats["errors"] += 1
        etype = type(e).__name__
        msg = str(e)
        if etype == "TypeError" and ("argument" in msg or "keyword" in msg):
            etype = "BadArgs"
        self._send_to(cid, {"id": rid, "ok": False, "error": {"type": etype, "message": msg},
                            "t": self._timing(t_in, t0, op)})

    def run_to_end(self, gen):
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            return stop.value

    # ---------------------------------------------------------- snapshot
    def _snapshot_due(self, now):
        if not self.server.clients:
            return False
        interval = max(SNAPSHOT_MIN_INTERVAL, self.snapshot_cost * 50)
        return self.snapshot_dirty or now - self.snapshot_at >= interval

    def _refresh_snapshot(self):
        t0 = self.clock()
        try:
            snap = self.build_snapshot()
        except Exception as e:
            self.log("snapshot failed: %s" % e)
            return
        t1 = self.clock()
        self.snapshot = snap
        self.snapshot_at = t1
        self.snapshot_cost = t1 - t0
        self.snapshot_dirty = False

    def build_snapshot(self):
        s = self.song()
        view = s.view
        tracks = list(s.tracks)
        sel_track = view.selected_track
        sel_scene = view.selected_scene
        scenes = list(s.scenes)
        snap = {
            "tempo": s.tempo,
            "signature": [s.signature_numerator, s.signature_denominator],
            "is_playing": s.is_playing,
            "current_song_time": s.current_song_time,
            "loop": {"on": s.loop, "start": s.loop_start, "length": s.loop_length},
            "metronome": s.metronome,
            "record_mode": s.record_mode,
            "session_record": s.session_record,
            "can_undo": s.can_undo, "can_redo": s.can_redo,
            "song_length": getattr(s, "song_length", None),
            "tracks": [track_summary(self, t, i) for i, t in enumerate(tracks)],
            "return_tracks": [track_summary(self, t, i, "return") for i, t in enumerate(s.return_tracks)],
            "master": track_summary(self, s.master_track, None, "master"),
            "scenes": [{"index": i, "name": sc.name} for i, sc in enumerate(scenes)],
            "selected": {
                "track": sel_track.name if sel_track is not None else None,
                "track_index": _index(tracks, sel_track),
                "scene_index": _index(scenes, sel_scene),
            },
            "as_of": time.time(),
        }
        return snap

    # ---------------------------------------------------------- status
    def status(self):
        h = self._health(self.clock())
        h.update({
            "python": sys.version.split()[0],
            "port": self.server.port,
            "clients": len(self.server.clients),
            "subscriptions": len(self.events.subs),
            "tick_count": self.tick_count,
            "snapshot_cost_ms": round(self.snapshot_cost * 1000, 2),
            "browser_indexed": dict((k, len(v)) for k, v in self.browser_index.items.items()),
            "stats": dict(self.stats),
            "slowest_ops": self.slowest_ops(),
            "ops_executed": dict((k, v[0]) for k, v in self.op_stats.items()),
        })
        return h

    def slowest_ops(self, n=8):
        """Ops with the highest mean Live-side execution time since the bridge (re)started."""
        return _top_ops(self.op_stats, n)


def _top_ops(op_stats, n=8):
    rows = [{"op": k, "n": v[0], "mean_ms": round(v[1] / v[0], 2), "max_ms": round(v[2], 2)}
            for k, v in op_stats.items() if v[0]]
    rows.sort(key=lambda r: r["mean_ms"], reverse=True)
    return rows[:n]


def _index(seq, obj):
    if obj is None:
        return None
    for i, x in enumerate(seq):
        if x == obj:
            return i
    return None
