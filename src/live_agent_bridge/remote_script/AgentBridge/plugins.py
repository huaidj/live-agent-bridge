"""Third-party plug-in (VST2/VST3/AU) and Max device support.

Live exposes a plug-in's parameters to the API only when they are configured. Plug-ins with
few parameters are configured automatically; large instruments (hundreds of parameters) only
expose what the user added in the device's "Configure" mode. ``get_parameter_names`` (Python
API) still lists every parameter, so the agent can see the whole plug-in and say exactly which
ones need to be configured before it can move them.
"""

import json

from .ops import OpError, device_of, match_params, op, param_info, set_param

SNAPSHOT_KEY = "agent_bridge:plugin_snapshots"
PLUGIN_CLASSES = ("PluginDevice", "AuPluginDevice")
MAX_CLASSES = ("MxDeviceAudioEffect", "MxDeviceInstrument", "MxDeviceMidiEffect")
DEVICE_TYPES = {0: "undefined", 1: "instrument", 2: "audio_effect", 4: "midi_effect"}


def _kind(d):
    cls = getattr(d, "class_name", "")
    if cls in PLUGIN_CLASSES:
        return "plugin_au" if cls == "AuPluginDevice" else "plugin"
    if cls in MAX_CLASSES:
        return "max_for_live"
    return "native"


def _all_names(d):
    fn = getattr(d, "get_parameter_names", None)
    if fn is None:
        return None
    names, begin, page = [], 0, 512
    while True:
        chunk = list(fn(begin, begin + page))
        names.extend(chunk)
        if len(chunk) < page or len(names) >= 8192:
            return names
        begin += page


def _owner(d):
    parent = getattr(d, "canonical_parent", None)
    return getattr(parent, "name", None)


@op("plugin.inspect")
def plugin_inspect(ctx, device=None, track=None, index=None, include_values=True):
    d = device_of(ctx, device, track, index)
    exposed = list(d.parameters)
    names = _all_names(d)
    by_name = {}
    for i, p in enumerate(exposed):
        by_name.setdefault(p.name, (i, p))
    params = []
    for i, p in enumerate(exposed):
        info = param_info(p, i) if include_values else {"index": i, "name": p.name}
        try:
            info["default"] = p.default_value
        except Exception:
            pass
        info["controllable"] = True
        params.append(info)
    hidden = []
    if names is not None:
        hidden = [n for n in names if n not in by_name]
    out = {"name": d.name, "class_name": getattr(d, "class_name", None),
           "plugin_name": getattr(d, "class_display_name", None), "kind": _kind(d),
           "device_type": DEVICE_TYPES.get(int(getattr(d, "type", 0)), "undefined"),
           "track": _owner(d), "ref": ctx.lom.ref(d),
           "exposed_parameters": params, "exposed_count": len(params),
           "total_parameters": len(names) if names is not None else len(params),
           "unexposed_parameter_names": hidden}
    for attr in ("presets", "selected_preset_index", "is_editor_open"):
        try:
            v = getattr(d, attr)
            out[attr] = list(v) if attr == "presets" else v
        except Exception:
            pass
    if hidden:
        out["how_to_expose"] = ("Live only exposes configured plug-in parameters. In Live: click the device's "
                                "'Configure' button (wrench), move the wanted controls in the plug-in window, "
                                "then click 'Configure' again. They become controllable and are saved with the set.")
    return out


@op("plugin.set", mutates=True)
def plugin_set(ctx, changes, device=None, track=None, index=None):
    """changes: [{name|index, value|normalized|display|delta|delta_normalized}]"""
    d = device_of(ctx, device, track, index)
    plist = list(d.parameters)
    names = _all_names(d) or []
    out = []
    for spec in changes:
        if "index" in spec:
            p = plist[int(spec["index"])]
        else:
            name = str(spec.get("name", ""))
            low = name.lower()
            matches = match_params(plist, name)
            if not matches:
                hidden = [n for n in names if low in n.lower()]
                if hidden:
                    raise OpError("%r exists in %s but is not exposed to Live's API; configure it first (device "
                                  "'Configure' button), then retry. Exposed now: %s"
                                  % (hidden[0], d.name, [q.name for q in plist][:40]))
                raise OpError("no parameter %r on %s" % (name, d.name))
            p = matches[0]
        before = param_info(p)
        if "delta" in spec or "delta_normalized" in spec:
            span = p.max - p.min
            delta = float(spec["delta"]) if "delta" in spec else float(spec["delta_normalized"]) * span
            after = set_param(p, value=p.value + delta)
        else:
            after = set_param(p, spec.get("value"), spec.get("normalized"), spec.get("display"))
        entry = {"name": p.name, "before": before["display"], "after": after["display"], "value": after["value"]}
        if abs(after["value"] - before["value"]) < 1e-9:
            entry["note"] = ("already at its maximum" if after["value"] >= p.max else
                             "already at its minimum" if after["value"] <= p.min else "unchanged")
        out.append(entry)
    return out


@op("plugin.preset", mutates=True)
def plugin_preset(ctx, action="list", device=None, track=None, index=None, preset=None):
    """action: list | select (preset index or name) | open_editor | close_editor (UI)"""
    d = device_of(ctx, device, track, index)
    presets = list(getattr(d, "presets", []) or [])
    if action == "select":
        if isinstance(preset, str) and not preset.isdigit():
            low = preset.lower()
            match = [i for i, n in enumerate(presets) if n.lower() == low] or \
                    [i for i, n in enumerate(presets) if low in n.lower()]
            if not match:
                raise OpError("no preset %r; available: %s" % (preset, presets[:50]))
            preset = match[0]
        d.selected_preset_index = int(preset)
    elif action in ("open_editor", "close_editor"):
        if not hasattr(d, "is_editor_open"):
            raise OpError("this device has no plug-in editor")
        d.is_editor_open = action == "open_editor"
    elif action != "list":
        raise OpError("action must be list, select, open_editor or close_editor")
    return {"presets": presets, "selected_preset_index": getattr(d, "selected_preset_index", None),
            "is_editor_open": getattr(d, "is_editor_open", None)}


def _load_snapshots(song):
    try:
        raw = song.get_data(SNAPSHOT_KEY, None)
    except Exception:
        raw = None
    return json.loads(raw) if raw else {}


def _save_snapshots(song, data):
    song.set_data(SNAPSHOT_KEY, json.dumps(data))


def _device_key(d):
    return "%s|%s|%s" % (_owner(d) or "?", d.name, getattr(d, "class_display_name", ""))


@op("plugin.snapshot", mutates=True)
def plugin_snapshot(ctx, action="list", name=None, amount=1.0, device=None, track=None, index=None):
    """Snapshots of a device's exposed parameters, stored inside the Live set.
    action: save | recall | morph (towards a snapshot by amount 0..1) | diff | list | delete"""
    s = ctx.song()
    if not hasattr(s, "get_data"):
        raise OpError("this Live version cannot store data in the set")
    d = device_of(ctx, device, track, index)
    store = _load_snapshots(s)
    key = _device_key(d)
    mine = store.setdefault(key, {})
    plist = list(d.parameters)
    if action == "list":
        return {"device": key, "snapshots": sorted(mine)}
    if not name:
        raise OpError("give a snapshot name")
    if action == "save":
        mine[name] = dict((p.name, p.value) for p in plist[1:] if getattr(p, "is_enabled", True))
        _save_snapshots(s, store)
        return {"saved": name, "parameters": len(mine[name])}
    if name not in mine:
        raise OpError("no snapshot %r for this device; have %s" % (name, sorted(mine)))
    snap = mine[name]
    if action == "delete":
        del mine[name]
        if not mine:  # no empty per-device entries left behind in the saved set
            store.pop(key, None)
        _save_snapshots(s, store)
        return {"deleted": name}
    changes = []
    for p in plist[1:]:
        if p.name not in snap or not getattr(p, "is_enabled", True):
            continue
        target = float(snap[p.name])
        if action == "diff":
            if abs(p.value - target) > 1e-6:
                changes.append({"name": p.name, "now": str(p), "snapshot": p.str_for_value(target)})
            continue
        amt = 1.0 if action == "recall" else max(0.0, min(1.0, float(amount)))
        new = target if getattr(p, "is_quantized", False) and amt >= 0.5 else p.value + (target - p.value) * amt
        if getattr(p, "is_quantized", False) and amt < 0.5:
            continue
        if abs(new - p.value) > 1e-9:
            set_param(p, value=new)
            changes.append({"name": p.name, "display": str(p)})
    if action not in ("recall", "morph", "diff"):
        raise OpError("action must be save, recall, morph, diff, list or delete")
    return {"action": action, "snapshot": name, "changed": changes, "count": len(changes)}


def _walk_plugins(ctx, root, path, out, depth):
    for item in list(root.children):
        p = path + [item.name]
        if item.is_loadable and not item.is_folder:
            fi = next((i for i, x in enumerate(p[:-1])
                       if x.upper() in ("VST3", "VST", "VST2", "AU", "AUDIO UNITS", "CLAP")), None)
            fmt = p[fi] if fi is not None else None
            vendor = p[fi + 1] if fi is not None and fi + 1 < len(p) - 1 else None
            uri = item.uri or ""
            if "#" in uri:  # e.g. query:Plugins#VST3:Vendor:Name
                parts = uri.split("#", 1)[1].split(":")
                fmt = fmt or parts[0] or None
                if len(parts) >= 3:
                    vendor = parts[1] or None
            out.append({"name": item.name, "path": "/".join(p), "format": fmt,
                        "vendor": vendor, "uri": item.uri})
            if item.uri:
                ctx.browser_index.by_uri[item.uri] = item
        elif depth < 6:
            yield None
            for step in _walk_plugins(ctx, item, p, out, depth + 1):
                yield step


@op("plugin.inventory")
def plugin_inventory(ctx, query=None):
    """Installed plug-ins from Live's browser (Plug-ins), with format and vendor folder."""
    root = getattr(ctx.app().browser, "plugins", None)
    if root is None:
        raise OpError("Live's browser has no Plug-ins category")
    out = []
    for step in _walk_plugins(ctx, root, [], out, 0):
        yield step
    if query:
        q = query.lower()
        out = [x for x in out if q in x["name"].lower() or q in (x["vendor"] or "").lower()]
    return {"total": len(out), "plugins": out}


@op("plugin.list_in_set")
def plugin_list_in_set(ctx):
    """Every third-party plug-in and Max device instance in the set, with how much is exposed."""
    s = ctx.song()
    found = []
    tracks = [("track", i, t) for i, t in enumerate(s.tracks)] + \
             [("return", i, t) for i, t in enumerate(s.return_tracks)] + [("master", 0, s.master_track)]

    def visit(devices, where):
        for di, d in enumerate(devices):
            kind = _kind(d)
            if kind != "native":
                names = _all_names(d)
                found.append({"where": where, "device_index": di, "name": d.name,
                              "plugin_name": getattr(d, "class_display_name", None), "kind": kind,
                              "exposed": len(d.parameters), "total": len(names) if names else len(d.parameters),
                              "ref": ctx.lom.ref(d, with_path=False)})
            if getattr(d, "can_have_chains", False):
                for ci, ch in enumerate(d.chains):
                    visit(ch.devices, "%s > %s chain %d" % (where, d.name, ci))

    for kind, i, t in tracks:
        visit(t.devices, "%s %d (%s)" % (kind, i, t.name))
    return {"count": len(found), "devices": found}

