"""Generic Live Object Model access: path resolution, introspection, serialization.

Paths use the same shape as Max for Live's LiveAPI, plus a few shortcuts:

    live_set tracks 0 devices 1 parameters 3
    live_set.tracks[0].clip_slots[2].clip        (dots/brackets also accepted)
    id 4711                                      (handle returned by an earlier call)
    track 0 | return 1 | master | scene 3        (shortcuts)
    live_app | live_set view selected_track

Must only be called on Live's main thread.
"""

import math
import re
from collections import OrderedDict

MAX_LIST = 512

# Attributes of a parent that may hold a child; used to rebuild canonical paths.
_CHILD_ATTRS = (
    "tracks", "return_tracks", "master_track", "visible_tracks", "scenes", "clip_slots", "clip",
    "devices", "chains", "return_chains", "drum_pads", "parameters", "mixer_device",
    "volume", "panning", "sends", "track_activator", "crossfader", "cue_points",
    "arrangement_clips", "view", "groove_pool", "grooves", "take_lanes", "sample",
)

_TOKEN_SPLIT = re.compile(r"[\s\.\[\]/]+")



def ordered_props(obj, props):
    """Live validates loop_start / loop_end against the song length the moment each one is set, so writing them
    in dict order can fail ("Cannot set the Loopstart behind the Songlength" when moving a long loop later).
    Apply them so that the intermediate loop always stays inside the old/new bounds:
    moving the start later -> length first; moving it earlier -> start first."""
    items = list((props or {}).items())
    keys = [k for k, _ in items]
    if "loop_start" in keys and "loop_length" in keys:
        try:
            later = float(dict(items)["loop_start"]) > float(obj.loop_start)
        except Exception:
            later = False
        first = "loop_length" if later else "loop_start"
        items.sort(key=lambda kv: 0 if kv[0] == first else (1 if kv[0] in ("loop_start", "loop_length") else 2))
    return items

class LomError(Exception):
    pass


class Registry(object):
    """Maps stable integer handles to live objects (bounded LRU)."""

    def __init__(self, limit=20000):
        self._items = OrderedDict()
        self._limit = limit

    def put(self, obj):
        key = handle_of(obj)
        self._items[key] = obj
        self._items.move_to_end(key)
        if len(self._items) > self._limit:
            self._items.popitem(last=False)
        return key

    def get(self, key):
        obj = self._items.get(key)
        if obj is None:
            raise LomError("unknown or expired handle id %s; resolve the path again" % key)
        return obj

    def clear(self):
        self._items.clear()


def handle_of(obj):
    ptr = getattr(obj, "_live_ptr", None)
    if isinstance(ptr, int):
        return ptr
    return id(obj)


def is_lom(obj):
    if obj is None or isinstance(obj, (bool, int, float, str, bytes, list, tuple, dict)):
        return False
    if hasattr(obj, "_live_ptr"):
        return True
    mod = getattr(type(obj), "__module__", "") or ""
    return mod.startswith("Live") and not _is_sequence(obj)


def _is_sequence(obj):
    if isinstance(obj, (str, bytes, dict)):
        return False
    if isinstance(obj, (list, tuple)):
        return True
    return hasattr(obj, "__len__") and hasattr(obj, "__getitem__") and not hasattr(obj, "_live_ptr")


class Lom(object):
    def __init__(self, song_fn, app_fn):
        self._song = song_fn
        self._app = app_fn
        self.registry = Registry()

    # ---- resolution -----------------------------------------------------
    def tokens(self, path):
        if isinstance(path, (list, tuple)):
            toks = [str(t) for t in path]
        else:
            toks = [t for t in _TOKEN_SPLIT.split(str(path).strip()) if t]
        if not toks:
            raise LomError("empty path")
        return toks

    def resolve(self, path):
        if isinstance(path, dict) and "$ref" in path:
            path = path["$ref"]
        toks = self.tokens(path)
        head = toks[0].lower()
        rest = toks[1:]
        song = self._song()
        if head in ("live_set", "song"):
            obj = song
        elif head in ("live_app", "app", "application"):
            obj = self._app()
        elif head == "id":
            if not rest:
                raise LomError("'id' needs a number")
            obj = self.registry.get(int(rest[0]))
            rest = rest[1:]
        elif head == "track":
            obj = song.tracks
        elif head in ("return", "return_track"):
            obj = song.return_tracks
        elif head in ("master", "master_track"):
            obj = song.master_track
        elif head == "scene":
            obj = song.scenes
        elif head in ("selected_track", "selected_scene", "detail_clip", "selected_parameter"):
            obj = getattr(song.view, head)
        else:
            raise LomError("unknown path root %r (use live_set, live_app, id N, track N, return N, master, scene N)" % toks[0])
        where = [toks[0]]
        for tok in rest:
            obj = self._step(obj, tok, where)
            where.append(tok)
        if obj is None:
            raise LomError("%s is empty (None)" % " ".join(where))
        return obj

    def _step(self, obj, tok, where):
        if obj is None:
            raise LomError("%s is None; cannot continue with %r" % (" ".join(where), tok))
        if _is_int(tok):
            idx = int(tok)
            try:
                n = len(obj)
            except Exception:
                raise LomError("%s is not a list; cannot index %s" % (" ".join(where), tok)) from None
            if idx < 0:
                idx += n
            if not 0 <= idx < n:
                raise LomError("index %s out of range for %s (len %d)" % (tok, " ".join(where), n))
            return obj[idx]
        check_name(tok)
        try:
            return getattr(obj, tok)
        except AttributeError:
            raise LomError("%s (%s) has no attribute %r" % (" ".join(where), type(obj).__name__, tok)) from None

    # ---- canonical path -------------------------------------------------
    def path_of(self, obj, limit=12):
        song = self._song()
        parts = []
        cur = obj
        for _ in range(limit):
            if cur is None:
                return None
            if _same(cur, song):
                return " ".join(["live_set"] + parts)
            parent = getattr(cur, "canonical_parent", None)
            if parent is None:
                return None
            seg = _locate(parent, cur)
            if seg is None:
                return None
            parts = seg + parts
            cur = parent
        return None

    # ---- serialization --------------------------------------------------
    def ref(self, obj, with_path=True):
        h = self.registry.put(obj)
        out = {"$ref": "id %d" % h, "type": type(obj).__name__}
        name = _safe_get(obj, "name")
        if isinstance(name, str):
            out["name"] = name
        if with_path:
            p = self.path_of(obj)
            if p:
                out["path"] = p
        return out

    def ser(self, v, depth=0, with_path=True):
        if v is None or isinstance(v, (bool, str)):
            return v
        if isinstance(v, int):
            return v
        if isinstance(v, float):
            return None if (math.isnan(v) or math.isinf(v)) else v
        if isinstance(v, dict):
            return dict((str(k), self.ser(x, depth + 1, with_path)) for k, x in v.items())
        if is_lom(v):
            return self.ref(v, with_path=with_path and depth < 2)
        note = _note_dict(v)
        if note is not None:
            return note
        if _is_sequence(v):
            try:
                items = list(v[:MAX_LIST])
            except Exception:
                items = [v[i] for i in range(min(len(v), MAX_LIST))]
            return [self.ser(x, depth + 1, with_path) for x in items]
        name = getattr(v, "display_name", None)
        if isinstance(name, str):
            # Track/Device RoutingType and RoutingChannel objects: their str() is an unreadable object repr
            return name
        return str(v)

    def coerce(self, v):
        """Turn ``{"$ref": "id N"}`` / ``"id N"`` arguments back into objects."""
        if isinstance(v, dict) and "$ref" in v:
            return self.resolve(v["$ref"])
        if isinstance(v, str) and v.startswith("id ") and _is_int(v[3:].strip()):
            return self.resolve(v)
        if isinstance(v, list):
            return [self.coerce(x) for x in v]
        return v

    # ---- operations -----------------------------------------------------
    def get(self, path, props=None):
        obj = self.resolve(path)
        if not props:
            return self.describe(obj, values=True)
        out = {}
        for p in props:
            check_name(p)
            try:
                out[p] = self.ser(getattr(obj, p))
            except Exception as e:
                out[p] = {"$error": "%s: %s" % (type(e).__name__, e)}
        return out

    def set(self, path, values):
        obj = self.resolve(path)
        done = {}
        for prop, val in ordered_props(obj, values):
            check_name(prop)
            val = self.coerce(val)
            if isinstance(val, str):
                try:
                    current = getattr(obj, prop)
                except Exception:
                    current = None
                if is_lom(current) or current is None and prop.startswith(("selected_", "detail_", "highlighted_")):
                    val = self.resolve(val)
            setattr(obj, prop, val)
            done[prop] = self.ser(getattr(obj, prop))
        return done

    def call(self, path, method, args=None, kwargs=None):
        obj = self.resolve(path)
        check_name(method)
        fn = getattr(obj, method, None)
        if fn is None or not callable(fn):
            raise LomError("%s has no method %r" % (type(obj).__name__, method))
        res = fn(*[self.coerce(a) for a in (args or [])],
                 **dict((k, self.coerce(x)) for k, x in (kwargs or {}).items()))
        return self.ser(res)

    def children(self, path, attr, offset=0, limit=100):
        obj = self.resolve(path)
        check_name(attr)
        seq = getattr(obj, attr)
        n = len(seq)
        items = [seq[i] for i in range(offset, min(n, offset + limit))]
        return {"total": n, "offset": offset, "items": [self.ser(x) for x in items]}

    def describe(self, obj, values=False):
        props, methods, observable = {}, {}, []
        cls = type(obj)
        for name in dir(obj):
            if name.startswith("_"):
                continue
            if name.endswith("_has_listener"):
                continue
            if name.startswith(("add_", "remove_")) and name.endswith("_listener"):
                if name.startswith("add_"):
                    observable.append(name[4:-9])
                continue
            try:
                attr = getattr(obj, name)
            except Exception as e:
                props[name] = {"$error": str(e)}
                continue
            if callable(attr) and not is_lom(attr):
                doc = (getattr(attr, "__doc__", None) or "").strip().splitlines()
                methods[name] = _clean_doc(doc[0]) if doc else ""
                continue
            entry = {}
            if values:
                if _is_sequence(attr) and not isinstance(attr, str):
                    try:
                        entry["len"] = len(attr)
                    except Exception:
                        pass
                    if entry.get("len", 0) <= 16:
                        entry["value"] = self.ser(attr, depth=1, with_path=False)
                else:
                    entry["value"] = self.ser(attr, depth=1, with_path=False)
            w = _writable(cls, name)
            if w is not None:
                entry["writable"] = w
            props[name] = entry
        out = {"type": cls.__name__, "properties": props, "methods": methods,
               "observable": sorted(observable)}
        out["ref"] = self.ref(obj)
        return out


def check_name(name):
    if not isinstance(name, str) or not name or name.startswith("_"):
        raise LomError("invalid or private attribute name %r" % (name,))


def _is_int(tok):
    try:
        int(tok)
        return True
    except (TypeError, ValueError):
        return False


def _same(a, b):
    if a is b:
        return True
    pa, pb = getattr(a, "_live_ptr", None), getattr(b, "_live_ptr", None)
    return pa is not None and pa == pb


def _safe_get(obj, name):
    try:
        return getattr(obj, name)
    except Exception:
        return None


def _locate(parent, child):
    for attr in _CHILD_ATTRS:
        try:
            val = getattr(parent, attr)
        except Exception:
            continue
        if val is None:
            continue
        if _same(val, child):
            return [attr]
        if _is_sequence(val):
            try:
                for i, x in enumerate(val):
                    if _same(x, child):
                        return [attr, str(i)]
            except Exception:
                continue
    return None


def _writable(cls, name):
    p = getattr(cls, name, None)
    if isinstance(p, property):
        return p.fset is not None
    return None


def _clean_doc(line):
    # Boost.Python docs look like "fire( (Clip)arg1) -> None : Fire the clip"
    return line.replace("(Live.", "(").strip()[:240]


_NOTE_FIELDS = ("note_id", "pitch", "start_time", "duration", "velocity", "mute",
                "probability", "velocity_deviation", "release_velocity")


def _note_dict(v):
    if type(v).__name__ != "MidiNote":
        return None
    out = {}
    for f in _NOTE_FIELDS:
        x = getattr(v, f, None)
        if x is not None:
            out[f] = x
    return out
