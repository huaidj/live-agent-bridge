"""Browser search and loading.

Walking Live's browser tree is the slowest thing an agent asks for, so the
index is built as a generator: the scheduler runs it in small time slices
across ticks (Live's UI never freezes) and caches the result per category.
"""

import time

from .ops import OpError, op, track_of

DEFAULT_CATEGORIES = ("instruments", "audio_effects", "midi_effects", "drums", "sounds",
                      "plugins", "max_for_live", "user_library", "current_project")
ALL_CATEGORIES = DEFAULT_CATEGORIES + ("samples", "clips", "packs", "user_folders")
MAX_DEPTH = 8


class BrowserIndex(object):
    def __init__(self):
        self.items = {}      # category -> list of dicts
        self.by_uri = {}     # uri -> BrowserItem
        self.built_at = {}

    def clear(self):
        self.items.clear()
        self.by_uri.clear()
        self.built_at.clear()


def _roots(browser, category):
    if category == "user_folders":
        return list(getattr(browser, "user_folders", []) or [])
    root = getattr(browser, category, None)
    return [root] if root is not None else []


def _index_category(index, browser, category):
    out = []
    stack = [(r, 0, r.name) for r in reversed(_roots(browser, category))]
    n = 0
    while stack:
        item, depth, path = stack.pop()
        n += 1
        uri = getattr(item, "uri", None)
        if depth > 0 and (item.is_loadable or item.is_folder):
            out.append({"name": item.name, "uri": uri, "category": category, "path": path,
                        "loadable": bool(item.is_loadable), "device": bool(getattr(item, "is_device", False)),
                        "folder": bool(item.is_folder)})
            if uri:
                index.by_uri[uri] = item
        if depth < MAX_DEPTH and (item.is_folder or depth == 0 or getattr(item, "children", None)):
            try:
                kids = list(item.children)
            except Exception:
                kids = []
            for k in reversed(kids):
                stack.append((k, depth + 1, path + "/" + k.name))
        if n % 64 == 0:
            yield None  # let the scheduler check its time budget
    index.items[category] = out
    index.built_at[category] = time.time()


def _score(entry, terms, phrase):
    name = entry["name"].lower()
    if not all(t in name or t in entry["path"].lower() for t in terms):
        return None
    s = 0
    base = name.rsplit(".", 1)[0]
    if base == phrase:
        s += 100
    elif base.startswith(phrase):
        s += 60
    elif phrase in name:
        s += 30
    if entry["loadable"]:
        s += 20
    if entry["device"]:
        s += 5
    s -= entry["path"].count("/")
    return s


@op("browser.search")
def browser_search(ctx, query, categories=None, limit=25, loadable_only=True, refresh=False):
    index = ctx.browser_index
    cats = list(categories or DEFAULT_CATEGORIES)
    for c in cats:
        if c not in ALL_CATEGORIES:
            raise OpError("unknown category %r; one of %s" % (c, list(ALL_CATEGORIES)))
    if refresh:
        for c in cats:
            index.items.pop(c, None)
    browser = ctx.app().browser
    for c in cats:
        if c not in index.items:
            for step in _index_category(index, browser, c):
                yield step
    phrase = query.strip().lower()
    terms = phrase.split()
    hits = []
    for c in cats:
        for e in index.items.get(c, ()):
            if loadable_only and not e["loadable"]:
                continue
            s = _score(e, terms, phrase)
            if s is not None:
                hits.append((s, e))
    hits.sort(key=lambda x: -x[0])
    return {"query": query, "total": len(hits), "results": [e for _, e in hits[:limit]],
            "indexed": dict((c, len(index.items.get(c, ()))) for c in cats)}


@op("browser.tree")
def browser_tree(ctx, category="instruments", path=None, limit=200):
    """List one level of the browser (cheap; no indexing)."""
    browser = ctx.app().browser
    roots = _roots(browser, category)
    if not roots:
        raise OpError("unknown category %r" % category)
    node = roots[0]
    for part in [p for p in (path or "").split("/") if p]:
        match = [k for k in node.children if k.name == part]
        if not match:
            raise OpError("no %r under %s" % (part, node.name))
        node = match[0]
    kids = list(node.children)[:limit]
    for k in kids:
        if k.uri:
            ctx.browser_index.by_uri[k.uri] = k
    return [{"name": k.name, "uri": k.uri, "loadable": k.is_loadable, "folder": k.is_folder} for k in kids]


@op("browser.load", mutates=True)
def browser_load(ctx, uri=None, query=None, track=None, category=None):
    """Load a browser item onto a track (selects the track first).

    Give ``uri`` (from browser.search) or a ``query``; the best loadable hit is used.
    """
    index = ctx.browser_index
    cats = [category] if category else None
    item = index.by_uri.get(uri) if uri else None
    if item is None and uri:
        # Unknown URI (e.g. from an earlier Live session): index, then look again.
        yield from browser_search(ctx, "", categories=cats, limit=0, refresh=True)
        item = index.by_uri.get(uri)
        if item is None and not query:
            raise OpError("no browser item with uri %r" % uri)
    if item is None:
        if not query:
            raise OpError("give uri or query")
        res = yield from browser_search(ctx, query, categories=cats, limit=1)
        if not res["results"]:
            raise OpError("nothing loadable matches %r" % query)
        item = index.by_uri.get(res["results"][0]["uri"])
        if item is None:
            raise OpError("browser item vanished; retry browser.search with refresh=true")
    s = ctx.song()
    t = track_of(ctx, track) if track is not None else s.view.selected_track
    before = len(t.devices)
    with background_selection(s, t):
        ctx.app().browser.load_item(item)
    return {"loaded": item.name, "track": t.name, "devices": [d.name for d in t.devices],
            "added": len(t.devices) - before}


# ---------------------------------------------------------------- background loading

class background_selection(object):
    """Live's browser only loads onto the *selected* track / drum pad. Select the target
    just for the load, then restore the user's selection, so agent work happens in the
    background without moving what the user is looking at."""

    def __init__(self, song, track, rack=None, pad_note=None):
        self.view = song.view
        self.track = track
        self.rack = rack
        self.pad_note = pad_note
        self.prev_track = None
        self.prev_pad = None

    def __enter__(self):
        self.prev_track = self.view.selected_track
        if self.rack is not None:
            self.prev_pad = self.rack.view.selected_drum_pad
        if self.prev_track != self.track:
            self.view.selected_track = self.track
        if self.rack is not None:
            self.rack.view.selected_drum_pad = self.rack.drum_pads[self.pad_note]
        return self

    def __exit__(self, *exc):
        try:
            if self.rack is not None and self.prev_pad is not None:
                self.rack.view.selected_drum_pad = self.prev_pad
            if self.prev_track is not None and self.prev_track != self.track:
                self.view.selected_track = self.prev_track
        except Exception:
            pass  # the previously selected object may have been deleted meanwhile
        return False


# ---------------------------------------------------------------- samples

def _walk_hint(browser, hint):
    """Descend the browser tree along a known folder path: O(depth), no indexing."""
    roots = _roots(browser, hint.get("category", ""))
    root_name = hint.get("root")
    if root_name:
        roots = [r for r in roots if r.name.lower() == root_name.lower()]
    for node in roots:
        ok = True
        for part in hint.get("rel") or []:
            low = part.lower()
            nxt = None
            for k in node.children:
                if k.name.lower() == low:
                    nxt = k
                    break
            if nxt is None:
                ok = False
                break
            node = nxt
        if ok and node.is_loadable:
            return node
    return None


def find_sample_item(ctx, path, hint):
    browser = ctx.app().browser
    index = ctx.browser_index
    if hint:
        item = _walk_hint(browser, hint)
        if item is not None:
            return item
    base = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    tail = "/".join((hint or {}).get("rel") or []).lower()
    cats = [hint["category"]] if hint and hint.get("category") else \
        ["user_folders", "user_library", "samples", "packs", "current_project"]
    best = None
    for c in cats:
        if c not in index.items:
            for step in _index_category(index, browser, c):
                yield step
        for e in index.items.get(c, ()):
            if e["name"].lower() == base and e["loadable"]:
                if tail and e["path"].lower().endswith(tail):
                    return index.by_uri.get(e["uri"])
                best = best or e
    if best is not None:
        return index.by_uri.get(best["uri"])
    return None


def _simpler_in(devices):
    for d in devices:
        if hasattr(d, "replace_sample"):
            return d
    return None


def _base(path):
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0]


def _load_direct(t, rack, target, path, note, replace_instrument):
    """Load via the official device API (no browser): Track/Chain.insert_device('Simpler') +
    SimplerDevice.replace_sample(path); drum pads get a new chain via RackDevice.insert_chain
    with DrumChain.in_note. Returns None when this Live build lacks a needed call."""
    try:
        if target == "simpler":
            simpler = _simpler_in(t.devices)
            if simpler is None:
                if not hasattr(t, "insert_device"):
                    return None
                inst = [i for i, d in enumerate(t.devices) if int(getattr(d, "type", 0)) == 1]
                if inst:
                    if not replace_instrument:
                        raise OpError("track %r already has an instrument (%s); pass replace_instrument=true"
                                      % (t.name, t.devices[inst[0]].name))
                    t.delete_device(inst[0])
                t.insert_device("Simpler", inst[0] if inst else 0)
                simpler = _simpler_in(t.devices)
            simpler.replace_sample(path)
            return {"loaded": _base(path), "track": t.name, "target": target, "method": "replace_sample",
                    "devices": [d.name for d in t.devices]}
        pad = rack.drum_pads[note]
        for ch in pad.chains:
            simpler = _simpler_in(ch.devices)
            if simpler is not None:
                simpler.replace_sample(path)
                return {"loaded": _base(path), "track": t.name, "target": target, "pad": note,
                        "method": "replace_sample"}
        if not (hasattr(rack, "insert_chain") and hasattr(rack, "chains")):
            return None
        if pad.chains:
            pad.delete_all_chains()
        n = len(rack.chains)
        rack.insert_chain(n)
        ch = rack.chains[n]
        if not hasattr(ch, "in_note") or not hasattr(ch, "insert_device"):
            return None
        ch.in_note = note
        ch.insert_device("Simpler", 0)
        _simpler_in(ch.devices).replace_sample(path)
        ch.name = _base(path)
        return {"loaded": _base(path), "track": t.name, "target": target, "pad": note,
                "method": "insert_chain+replace_sample"}
    except OpError:
        raise
    except Exception:
        return None  # fall back to the browser path


@op("browser.preview", mutates=True)
def browser_preview(ctx, uri=None, stop=False):
    """Audition a browser item through Live's preview (or stop the preview)."""
    b = ctx.app().browser
    if stop:
        b.stop_preview()
        return {"preview": "stopped"}
    item = ctx.browser_index.by_uri.get(uri)
    if item is None:
        raise OpError("unknown uri; run browser.search first")
    b.preview_item(item)
    return {"preview": item.name}


@op("sample.load", mutates=True)
def sample_load(ctx, path, target="simpler", track=None, slot=None, start_time=None, pad_note=None,
                hint=None, replace_instrument=False):
    """Put an audio file into Live.

    target: session (audio clip in a clip slot), arrangement (audio clip at start_time),
    simpler (new Simpler on a MIDI track), drum_pad (pad ``pad_note`` of the track's Drum Rack).
    """
    s = ctx.song()
    t = track_of(ctx, track) if track is not None else s.view.selected_track
    if target == "session":
        try:
            cs = t.clip_slots[slot or 0]
        except (IndexError, AttributeError):
            raise OpError("clip slot %s not available on track %r" % (slot, t.name)) from None
        if not hasattr(cs, "create_audio_clip"):
            raise OpError("this Live version can't create session audio clips from files; use target=arrangement")
        if cs.has_clip:
            raise OpError("slot already has a clip")
        cs.create_audio_clip(path)
        return ctx.lom.ref(cs.clip)
    if target == "arrangement":
        if not hasattr(t, "create_audio_clip"):
            raise OpError("Track.create_audio_clip needs Live 12.2 or newer")
        return ctx.lom.ref(t.create_audio_clip(path, float(start_time or 0.0)))
    if target not in ("simpler", "drum_pad"):
        raise OpError("target must be session, arrangement, simpler or drum_pad")
    rack = None
    if target == "drum_pad":
        racks = [d for d in t.devices if getattr(d, "can_have_drum_pads", False)]
        if not racks:
            raise OpError("track %r has no Drum Rack; insert one first (insert_device name='Drum Rack')" % t.name)
        rack = racks[0]
    # Fast path, any file on disk: insert a native Simpler and replace_sample() into it.
    direct = _load_direct(t, rack, target, path, 36 if pad_note is None else int(pad_note), replace_instrument)
    if direct is not None:
        return direct
    item = yield from find_sample_item(ctx, path, hint)
    if item is None:
        raise OpError("sample is not visible in Live's browser (add its folder under Places, "
                      "or use target=session/arrangement): %s" % path)
    before = len(t.devices)
    with background_selection(s, t, rack, 36 if pad_note is None else int(pad_note)):
        ctx.app().browser.load_item(item)
    return {"loaded": item.name, "track": t.name, "target": target,
            "devices": [d.name for d in t.devices], "added": len(t.devices) - before}
