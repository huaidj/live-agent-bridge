"""Domain ops covering the rest of the official Live API: clip editing, warp
markers, automation, routing, racks, Simpler, device-specific properties,
take lanes, grooves, tuning, cue points, recording, application/dialogs,
persistent set data, and a live coverage audit against the official LOM.

Everything is feature-detected (``hasattr``) so one script works across Live
12.x builds; a missing capability produces a clear error, never a crash.
"""

import json
import os

from .lom import _same
from .ops import OpError, _live, clip_of, device_of, op, param_info, set_param, track_of, velocity_on


def _need(obj, name, what=None):
    fn = getattr(obj, name, None)
    if fn is None:
        raise OpError("%s.%s is not available in this Live version" % (what or type(obj).__name__, name))
    return fn


def _dispatch(table, action):
    if action not in table:
        raise OpError("unknown action %r; one of %s" % (action, sorted(table)))
    return table[action]()


# Python API enums (differ from the Max for Live documentation numbering)
REC_QUANT = {"none": 0, "1/4": 1, "1/8": 2, "1/8t": 3, "1/8+1/8t": 4, "1/16": 5, "1/16t": 6,
             "1/16+1/16t": 7, "1/32": 8}
GRID = {"none": 0, "8 bars": 1, "4 bars": 2, "2 bars": 3, "1 bar": 4, "1/2": 5, "1/4": 6, "1/8": 7,
        "1/16": 8, "1/32": 9}


def _enum(table, value, what):
    if value is None:
        return None
    if isinstance(value, int):
        return value
    key = str(value).strip().lower()
    if key not in table:
        raise OpError("unknown %s %r; one of %s" % (what, value, list(table)))
    return table[key]


def _routing_name(r):
    return getattr(r, "display_name", None) if r is not None else None


def _pick_routing(options, name):
    low = str(name).lower()
    for r in options:
        if r.display_name.lower() == low:
            return r
    for r in options:
        if low in r.display_name.lower():
            return r
    raise OpError("no routing %r; available: %s" % (name, [r.display_name for r in options]))


# ---------------------------------------------------------------- clip editing

@op("clip.edit", mutates=True)
def clip_edit(ctx, action, clip=None, track=None, slot=None, grid=None, amount=1.0, pitch=None,
              region_start=None, region_length=None, destination_time=None, transposition=0,
              note_ids=None, beats=None, state=None, grid_is_triplet=None, parameter=None):
    """action: quantize | quantize_pitch | crop | duplicate_loop | duplicate_region | duplicate_notes |
    select_all | deselect_all | select_notes | selected_notes | all_notes | move_playing_pos | scrub |
    stop_scrub | fire_button | set_grid | show_loop | show_envelope | hide_envelope | stop"""
    c = clip_of(ctx, clip, track, slot)
    view = getattr(c, "view", None)

    def notes_out(notes):
        from .ops import _note_out
        return [_note_out(n) for n in notes]

    def dup_region():
        _need(c, "duplicate_region")(float(region_start), float(region_length), float(destination_time),
                                     -1 if pitch is None else int(pitch), int(transposition))
        return {"duplicated": True}

    def set_grid():
        if grid is not None:
            view.grid_quantization = _enum(GRID, grid, "grid")
        if grid_is_triplet is not None:
            view.grid_is_triplet = bool(grid_is_triplet)
        return {"grid_quantization": str(view.grid_quantization), "grid_is_triplet": view.grid_is_triplet}

    q = _enum(REC_QUANT, grid if grid is not None else "1/16", "quantize grid") if action.startswith(
        "quantize") else None

    def all_notes():
        fn = getattr(c, "get_all_notes_extended", None)
        if fn is not None:
            return notes_out(fn())
        return notes_out(c.get_notes_extended(0, 128, -1e6, 2e6))

    def show_env():
        if parameter is not None:
            view.select_envelope_parameter(ctx.lom.resolve(parameter))
        view.show_envelope()
        return {"shown": True}

    table = {
        "quantize": lambda: (_need(c, "quantize")(q, float(amount)), {"quantized_to": grid or "1/16"})[1],
        "quantize_pitch": lambda: (_need(c, "quantize_pitch")(int(pitch), q, float(amount)),
                                   {"quantized_to": grid or "1/16"})[1],
        "crop": lambda: (_need(c, "crop")(), {"length": c.length})[1],
        "duplicate_loop": lambda: (_need(c, "duplicate_loop")(), {"length": c.length})[1],
        "duplicate_region": dup_region,
        "duplicate_notes": lambda: {"new_ids": ctx.lom.ser(_need(c, "duplicate_notes_by_id")(list(note_ids)))},
        "select_all": lambda: (c.select_all_notes(), {"ok": 1})[1],
        "deselect_all": lambda: (c.deselect_all_notes(), {"ok": 1})[1],
        "select_notes": lambda: (_need(c, "select_notes_by_id")(list(note_ids)), {"ok": 1})[1],
        "selected_notes": lambda: notes_out(_need(c, "get_selected_notes_extended")()),
        "all_notes": all_notes,
        "move_playing_pos": lambda: (c.move_playing_pos(float(beats)), {"ok": 1})[1],
        "scrub": lambda: (c.scrub(float(beats)), {"ok": 1})[1],
        "stop_scrub": lambda: (c.stop_scrub(), {"ok": 1})[1],
        "fire_button": lambda: (c.set_fire_button_state(bool(state)), {"ok": 1})[1],
        "stop": lambda: (c.stop(), {"ok": 1})[1],
        "set_grid": set_grid,
        "show_loop": lambda: (view.show_loop(), {"ok": 1})[1],
        "show_envelope": show_env,
        "hide_envelope": lambda: (view.hide_envelope(), {"ok": 1})[1],
    }
    return _dispatch(table, action)


# ---------------------------------------------------------------- warp markers

@op("clip.warp", mutates=True)
def clip_warp(ctx, action="list", clip=None, track=None, slot=None, beat_time=None, sample_time=None,
              distance=None, warp_mode=None, warping=None):
    """action: list | add | move | remove | set (warp_mode / warping)."""
    c = clip_of(ctx, clip, track, slot)
    if not getattr(c, "is_audio_clip", False):
        raise OpError("warp markers exist only on audio clips")

    def listing():
        out = {"warping": c.warping, "warp_mode": int(c.warp_mode),
               "available_warp_modes": [int(m) for m in getattr(c, "available_warp_modes", [])],
               "markers": [{"beat_time": m.beat_time, "sample_time": m.sample_time} for m in c.warp_markers]}
        return out

    def add():
        st = sample_time
        if st is None:  # place it on the current warp curve, so the audio does not move
            ms = sorted(((m.beat_time, m.sample_time) for m in c.warp_markers))
            if len(ms) < 2:
                raise OpError("give sample_time (the clip has fewer than two warp markers)")
            b = float(beat_time)
            lo = max([m for m in ms if m[0] <= b] or [ms[0]])
            hi = min([m for m in ms if m[0] > b] or [ms[-1]])
            if hi[0] == lo[0]:
                lo, hi = ms[-2], ms[-1]
            st = lo[1] + (b - lo[0]) * (hi[1] - lo[1]) / (hi[0] - lo[0])
        Live = _live()
        wm_cls = getattr(Live.Clip, "WarpMarker", None)
        if wm_cls is None:
            raise OpError("Live.Clip.WarpMarker is not available in this Live version")
        c.add_warp_marker(wm_cls(float(st), float(beat_time)))
        return listing()

    def setp():
        if warping is not None:
            c.warping = bool(warping)
        if warp_mode is not None:
            c.warp_mode = int(warp_mode)
        return listing()

    return _dispatch({
        "list": listing, "add": add, "set": setp,
        "move": lambda: (c.move_warp_marker(float(beat_time), float(distance)), listing())[1],
        "remove": lambda: (c.remove_warp_marker(float(beat_time)), listing())[1],
    }, action)


# ---------------------------------------------------------------- automation

def _param(ctx, parameter):
    p = ctx.lom.resolve(parameter)
    if not hasattr(p, "value") or not hasattr(p, "min"):
        raise OpError("%r is not a DeviceParameter" % parameter)
    return p


@op("automation", mutates=True)
def automation(ctx, action, parameter=None, clip=None, track=None, slot=None, steps=None, times=None,
               points=None):
    """Clip automation / modulation envelopes. Live only CREATES envelopes on session clips; an arrangement
    clip's existing envelopes (made in the UI) can be read and written.

    action: read (values at ``times`` or every beat) | write (``steps`` [{time, duration, value}] or
    ``points`` [{time, value}] as 1/16 steps) | clear | clear_all | has | re_enable."""
    if action == "re_enable":
        p = _param(ctx, parameter)
        p.re_enable_automation()
        return {"automation_state": int(getattr(p, "automation_state", 0))}
    c = clip_of(ctx, clip, track, slot)
    if action == "clear_all":
        c.clear_all_envelopes()
        return {"cleared": "all"}
    if action == "has":
        return {"has_envelopes": bool(getattr(c, "has_envelopes", False))}
    p = _param(ctx, parameter)
    if action == "clear":
        c.clear_envelope(p)
        return {"cleared": p.name}
    env = _need(c, "automation_envelope", "Clip")(p)
    if action == "read":
        if env is None:
            return {"parameter": p.name, "envelope": None}
        ts = times if times is not None else [float(i) for i in range(int(c.length) + 1)]
        return {"parameter": p.name, "min": p.min, "max": p.max,
                "values": [{"time": t, "value": env.value_at_time(float(t))} for t in ts]}
    if action == "write":
        if env is None:
            try:
                env = _need(c, "create_automation_envelope", "Clip")(p)
            except RuntimeError as e:
                if getattr(c, "is_arrangement_clip", False):
                    raise OpError("Live cannot create a new envelope on an arrangement clip (%s). Workaround for MIDI "
                                  "tracks: write the envelope on a session MIDI clip of the same track, then copy it "
                                  "with arrangement action=duplicate_from_session - Live converts it into the TRACK's "
                                  "arrangement automation over that clip's span (device and mixer parameters; "
                                  "verified Live 12.4.16). AUDIO clips lose the envelope on that copy." % e) from None
                raise
        if env is None:
            raise OpError("Live refused to create an envelope for %r on this clip" % p.name)
        items = list(steps or [])
        if points:
            pts = sorted(points, key=lambda x: x["time"])
            for i, pt in enumerate(pts):
                end = pts[i + 1]["time"] if i + 1 < len(pts) else pt["time"] + 0.25
                items.append({"time": pt["time"], "duration": max(1e-3, end - pt["time"]), "value": pt["value"]})
        for s in items:
            v = max(p.min, min(p.max, float(s["value"])))
            env.insert_step(float(s["time"]), float(s.get("duration", 0.25)), v)
        return {"parameter": p.name, "written": len(items)}
    raise OpError("action must be read, write, clear, clear_all, has or re_enable")


# ---------------------------------------------------------------- routing

@op("routing", mutates=True)
def routing(ctx, track=None, device=None, input_type=None, input_channel=None, output_type=None,
            output_channel=None):
    """Read or set routing of a track (input/output) or of a device's sidechain / IO
    (Compressor sidechain, Max devices' audio/midi IO). Names match display names."""
    if device is not None:
        d = ctx.lom.resolve(device)
        out = {}
        if hasattr(d, "available_input_routing_types"):  # CompressorDevice sidechain
            if input_type is not None:
                d.input_routing_type = _pick_routing(d.available_input_routing_types, input_type)
            if input_channel is not None:
                d.input_routing_channel = _pick_routing(d.available_input_routing_channels, input_channel)
            out["sidechain"] = {"type": _routing_name(d.input_routing_type),
                                "channel": _routing_name(d.input_routing_channel),
                                "types": [r.display_name for r in d.available_input_routing_types],
                                "channels": [r.display_name for r in d.available_input_routing_channels]}
        for io_name in ("audio_inputs", "audio_outputs", "midi_inputs", "midi_outputs"):
            ios = getattr(d, io_name, None)
            if ios:
                out[io_name] = [{"type": _routing_name(io.routing_type), "channel": _routing_name(io.routing_channel),
                                 "types": [r.display_name for r in io.available_routing_types]} for io in ios]
        if not out:
            raise OpError("device %r has no routable inputs/outputs" % d.name)
        return out
    t = track_of(ctx, track)
    if input_type is not None:
        t.input_routing_type = _pick_routing(t.available_input_routing_types, input_type)
    if input_channel is not None:
        t.input_routing_channel = _pick_routing(t.available_input_routing_channels, input_channel)
    if output_type is not None:
        t.output_routing_type = _pick_routing(t.available_output_routing_types, output_type)
    if output_channel is not None:
        t.output_routing_channel = _pick_routing(t.available_output_routing_channels, output_channel)
    res = {"track": t.name}
    for side in ("input", "output"):
        if not hasattr(t, "%s_routing_type" % side):
            continue
        res[side] = {"type": _routing_name(getattr(t, "%s_routing_type" % side)),
                     "channel": _routing_name(getattr(t, "%s_routing_channel" % side)),
                     "types": [r.display_name for r in getattr(t, "available_%s_routing_types" % side)],
                     "channels": [r.display_name for r in getattr(t, "available_%s_routing_channels" % side)]}
    return res


@op("routing.set_io", mutates=True)
def routing_set_io(ctx, device, io, index=0, type=None, channel=None):
    """Set a Max device's DeviceIO routing: io in audio_inputs/audio_outputs/midi_inputs/midi_outputs."""
    d = ctx.lom.resolve(device)
    if io not in ("audio_inputs", "audio_outputs", "midi_inputs", "midi_outputs"):
        raise OpError("io must be audio_inputs, audio_outputs, midi_inputs or midi_outputs")
    ports = getattr(d, io, None)
    if not ports:
        have = [k for k in ("audio_inputs", "audio_outputs", "midi_inputs", "midi_outputs") if getattr(d, k, None)]
        raise OpError("device %r has no %s; it exposes: %s" % (d.name, io, ", ".join(have) or "no routable IO"))
    if not 0 <= int(index) < len(ports):
        raise OpError("%s index must be 0..%d for %r" % (io, len(ports) - 1, d.name))
    port = ports[int(index)]
    if type is not None:
        port.routing_type = _pick_routing(port.available_routing_types, type)
    if channel is not None:
        port.routing_channel = _pick_routing(port.available_routing_channels, channel)
    return {"type": _routing_name(port.routing_type), "channel": _routing_name(port.routing_channel)}


# ---------------------------------------------------------------- device chain

def _is_instrument(d):
    return int(getattr(d, "type", 0)) == 1  # Live.Device.DeviceType.instrument


@op("device.insert", mutates=True)
def device_insert(ctx, name, track=None, chain=None, index=-1, replace_instrument=False):
    """Insert a native Live device by name (e.g. 'Reverb', 'EQ Eight', 'Operator', 'Drum Rack')
    directly - no browser walk. ``chain`` = rack chain path to insert into a chain instead.
    A chain holds one instrument: with replace_instrument=true an existing one is replaced."""
    target = ctx.lom.resolve(chain) if chain is not None else track_of(ctx, track if track is not None
                                                                       else "selected_track")
    fn = _need(target, "insert_device")
    n = len(target.devices)
    pos = n if index is None or int(index) < 0 else min(int(index), n)
    try:
        fn(str(name), pos)
    except RuntimeError as e:
        if "instrument" not in str(e):
            raise
        existing = [(i, d) for i, d in enumerate(target.devices) if _is_instrument(d)]
        if not existing or not replace_instrument:
            raise OpError("%s already has an instrument (%s); pass replace_instrument=true to replace it"
                          % (getattr(target, "name", "chain"), ", ".join(d.name for _, d in existing))) from e
        pos = existing[0][0]
        target.delete_device(pos)
        fn(str(name), pos)
    dev = target.devices[pos] if pos < len(target.devices) else target.devices[-1]
    return {"inserted": dev.name, "index": pos, "ref": ctx.lom.ref(dev), "devices": [d.name for d in target.devices]}


@op("device.move", mutates=True)
def device_move(ctx, device, target, position=-1):
    """Move a device to another track/chain (Song.move_device)."""
    d = ctx.lom.resolve(device)
    tgt = track_of(ctx, target)
    pos = len(tgt.devices) if int(position) < 0 else int(position)
    s = ctx.song()
    res = _need(s, "move_device", "Song")(d, tgt, pos)
    return {"moved": d.name, "position": ctx.lom.ser(res) if res is not None else pos,
            "devices": [x.name for x in tgt.devices]}


@op("device.control", mutates=True)
def device_control(ctx, action, device=None, track=None, index=None, value=None, bank=None, script_index=0):
    """action: info | activate | deactivate | collapse | expand | ab_save | ab_toggle | ab_set |
    select | select_instrument | store_bank | max_banks | delete"""
    d = device_of(ctx, device, track, index)

    def info():
        out = {"name": d.name, "class_name": getattr(d, "class_name", None),
               "class_display_name": getattr(d, "class_display_name", None), "type": str(getattr(d, "type", "")),
               "is_active": getattr(d, "is_active", None), "ref": ctx.lom.ref(d)}
        for a in ("latency_in_samples", "latency_in_ms", "can_compare_ab", "is_using_compare_preset_b",
                  "can_have_chains", "can_have_drum_pads"):
            if hasattr(d, a):
                out[a] = getattr(d, a)
        return out

    def onoff(v):
        p = d.parameters[0]  # "Device On"
        p.value = p.max if v else p.min
        return info()

    def store_bank():
        # Device.store_chosen_bank is only valid for Max for Live devices. Calling it on a native device
        # (a Reverb in the 2026-11-17 harness run) raised EXCEPTION_ACCESS_VIOLATION and killed Live, losing every
        # unsaved edit - so it must never reach Live unless the target and the bank index are valid.
        if not str(getattr(d, "class_name", "")).startswith("Mx"):
            raise OpError("store_bank only works on Max for Live devices; %r (%s) is not one. (Calling the Live API "
                          "on a non-Max device crashes Live.)" % (d.name, getattr(d, "class_name", "?")))
        n = _need(d, "get_bank_count")()
        if bank is None or not 0 <= int(bank) < int(n):
            raise OpError("bank must be between 0 and %d for %r" % (int(n) - 1, d.name))
        d.store_chosen_bank(int(script_index), int(bank))
        return info()

    def max_banks():
        n = _need(d, "get_bank_count")()
        return [{"index": i, "name": d.get_bank_name(i),
                 "parameters": [ctx.lom.ser(x) for x in d.get_bank_parameters(i)]} for i in range(n)]

    def delete():
        parent = d.canonical_parent
        for i, x in enumerate(parent.devices):
            if _same(x, d):
                name = d.name
                parent.delete_device(i)
                return {"deleted": name}
        raise OpError("device not found in its parent")

    def ab_set():
        d.is_using_compare_preset_b = bool(value)
        return info()

    def duplicate():
        parent = d.canonical_parent
        dup = _need(parent, "duplicate_device", type(parent).__name__)
        if _is_instrument(d):
            raise OpError("Live can't duplicate an instrument within one chain (one instrument per chain); "
                          "duplicate the track instead, or wrap it in an Instrument Rack")
        for i, x in enumerate(parent.devices):
            if _same(x, d):
                dup(i)
                return {"duplicated": d.name, "devices": [y.name for y in parent.devices]}
        raise OpError("device not found in its parent")

    table = {
        "info": info, "activate": lambda: onoff(True), "deactivate": lambda: onoff(False),
        "collapse": lambda: (setattr(d.view, "is_collapsed", True), info())[1],
        "expand": lambda: (setattr(d.view, "is_collapsed", False), info())[1],
        "ab_save": lambda: (_need(d, "save_preset_to_compare_ab_slot")(), info())[1],
        "ab_toggle": lambda: (setattr(d, "is_using_compare_preset_b", not d.is_using_compare_preset_b), info())[1],
        "ab_set": ab_set,
        "select": lambda: (ctx.song().view.select_device(d), info())[1],
        "select_instrument": lambda: (d.canonical_parent.view.select_instrument(), info())[1],
        "store_bank": store_bank,
        "max_banks": max_banks, "delete": delete, "duplicate": duplicate,
    }
    return _dispatch(table, action)


_BASE_DEVICE = {"parameters", "view", "can_have_chains", "can_have_drum_pads", "class_display_name", "class_name",
                "is_active", "name", "type", "latency_in_samples", "latency_in_ms", "can_compare_ab",
                "is_using_compare_preset_b", "canonical_parent", "store_chosen_bank",
                "save_preset_to_compare_ab_slot"}


@op("device.special", mutates=True)
def device_special(ctx, action="info", device=None, track=None, index=None, values=None, method=None,
                   args=None):
    """Class-specific device API (Wavetable, Drift, Meld, Roar, Hybrid Reverb, EQ Eight, Shifter,
    Spectral Resonator, Looper, Plug-in presets, Compressor, Drum Cell, Simpler, Racks...).

    action: info (list props incl. *_list options and methods) | set (``values``) | call (``method``, ``args``)"""
    d = device_of(ctx, device, track, index)
    if action == "set":
        out = {}
        for k, v in (values or {}).items():
            if k in _BASE_DEVICE or k.startswith("_"):
                raise OpError("%r is not a device-specific property" % k)
            setattr(d, k, ctx.lom.coerce(v))
            out[k] = ctx.lom.ser(getattr(d, k))
        return out
    if action == "call":
        if not method or method in _BASE_DEVICE or method.startswith("_"):
            raise OpError("give a device-specific method")
        res = getattr(d, method)(*[ctx.lom.coerce(a) for a in (args or [])])
        return {"result": ctx.lom.ser(res)}
    desc = ctx.lom.describe(d, values=True)
    props = dict((k, v) for k, v in desc["properties"].items() if k not in _BASE_DEVICE)
    methods = dict((k, v) for k, v in desc["methods"].items() if k not in _BASE_DEVICE)
    view_props = {}
    try:
        vd = ctx.lom.describe(d.view, values=True)
        view_props = dict((k, v) for k, v in vd["properties"].items() if k not in ("canonical_parent",))
    except Exception:
        pass
    return {"name": d.name, "class_name": getattr(d, "class_name", None), "properties": props,
            "methods": methods, "view": view_props, "observable": desc["observable"]}


# ---------------------------------------------------------------- racks

@op("rack", mutates=True)
def rack(ctx, action, device=None, track=None, index=None, chain=None, pad=None, source=None,
         destination=None, variation=None, values=None, device_name=None, position=-1):
    """Rack / Drum Rack control.

    action: info | add_macro | remove_macro | randomize_macros | store_variation | recall_variation |
    recall_last_variation | delete_variation | insert_chain | copy_pad | show_chains | hide_chains |
    chain_set (chain, values: name/mute/solo/color/volume/pan/in_note/out_note/choke_group) |
    chain_insert_device (chain, device_name) | chain_delete_device (chain, position) |
    pad_set (pad note, values: mute/solo/name) | pad_clear (pad) | select_chain | select_pad"""
    d = device_of(ctx, device, track, index)
    if not getattr(d, "can_have_chains", False):
        raise OpError("%r is not a rack" % d.name)
    s = ctx.song()

    def info():
        out = {"name": d.name, "class_name": d.class_name, "ref": ctx.lom.ref(d),
               "macros": [param_info(p, i) for i, p in enumerate(d.parameters)][1:1 + getattr(d, "visible_macro_count", 8)],
               "chains": [_chain_info(ctx, c, i) for i, c in enumerate(d.chains)]}
        for a in ("variation_count", "selected_variation_index", "visible_macro_count", "has_macro_mappings",
                  "is_showing_chains", "has_drum_pads"):
            try:  # Live raises RuntimeError (not AttributeError) for drum-pad props on non-drum racks
                out[a] = getattr(d, a)
            except (AttributeError, RuntimeError):
                pass
        if getattr(d, "can_have_drum_pads", False):
            out["pads"] = [{"note": p.note, "name": p.name, "mute": getattr(p, "mute", None),
                            "solo": getattr(p, "solo", None),
                            "chains": [c.name for c in p.chains]} for p in d.drum_pads if p.chains]
        if getattr(d, "return_chains", None):
            out["return_chains"] = [_chain_info(ctx, c, i) for i, c in enumerate(d.return_chains)]
        return out

    def get_chain():
        if chain is None:
            raise OpError("give chain (index or path)")
        if isinstance(chain, int):
            return d.chains[chain]
        return ctx.lom.resolve(chain)

    def get_pad():
        if pad is None:
            raise OpError("give pad (MIDI note)")
        return d.drum_pads[int(pad)]

    def chain_set():
        c = get_chain()
        out = {}
        for k, v in (values or {}).items():
            if k in ("volume", "pan", "panning"):
                p = c.mixer_device.volume if k == "volume" else c.mixer_device.panning
                out[k] = set_param(p, display=v) if isinstance(v, str) else set_param(p, value=v)
            elif k.startswith("send_"):
                p = c.mixer_device.sends[int(k[5:])]
                out[k] = set_param(p, display=v) if isinstance(v, str) else set_param(p, value=v)
            elif k in ("name", "mute", "solo", "color", "color_index", "is_auto_colored", "in_note", "out_note",
                       "choke_group"):
                setattr(c, k, v)
                out[k] = getattr(c, k)
            else:
                raise OpError("cannot set %r on a chain" % k)
        return out

    def chain_insert():
        c = get_chain()
        n = len(c.devices)
        pos = n if int(position) < 0 else min(int(position), n)
        _need(c, "insert_device", "Chain")(str(device_name), pos)
        return {"devices": [x.name for x in c.devices]}

    def recall():
        if variation is not None:
            d.selected_variation_index = int(variation)
        d.recall_selected_variation()
        return info()

    def copy_pad():
        d.copy_pad(int(source), int(destination))
        return info()

    table = {
        "info": info,
        "add_macro": lambda: (d.add_macro(), info())[1],
        "remove_macro": lambda: (d.remove_macro(), info())[1],
        "randomize_macros": lambda: (d.randomize_macros(), info())[1],
        "store_variation": lambda: (d.store_variation(), info())[1],
        "recall_variation": recall,
        "recall_last_variation": lambda: (d.recall_last_used_variation(), info())[1],
        "delete_variation": lambda: ((setattr(d, "selected_variation_index", int(variation))
                                      if variation is not None else None), d.delete_selected_variation(),
                                     info())[2],
        "insert_chain": lambda: (_need(d, "insert_chain", "RackDevice")(
            len(d.chains) if int(position) < 0 else int(position)), info())[1],
        "copy_pad": copy_pad,
        "show_chains": lambda: (setattr(d, "is_showing_chains", True), {"ok": 1})[1],
        "hide_chains": lambda: (setattr(d, "is_showing_chains", False), {"ok": 1})[1],
        "chain_set": chain_set,
        "chain_insert_device": chain_insert,
        "chain_delete_device": lambda: (get_chain().delete_device(int(position)), {"ok": 1})[1],
        "pad_set": lambda: (_apply(get_pad(), values, ("mute", "solo", "name")), info())[1],
        "pad_clear": lambda: (get_pad().delete_all_chains(), info())[1],
        "select_chain": lambda: (setattr(d.view, "selected_chain", get_chain()),
                                 setattr(s.view, "selected_chain", get_chain()), {"ok": 1})[2],
        "select_pad": lambda: (setattr(d.view, "selected_drum_pad", get_pad()), {"ok": 1})[1],
    }
    return _dispatch(table, action)


def _apply(obj, values, allowed):
    for k, v in (values or {}).items():
        if k not in allowed:
            raise OpError("cannot set %r here" % k)
        setattr(obj, k, v)


def _chain_info(ctx, c, i):
    out = {"index": i, "name": c.name, "mute": getattr(c, "mute", None), "solo": getattr(c, "solo", None),
           "devices": [x.name for x in c.devices], "ref": ctx.lom.ref(c, with_path=False)}
    for a in ("in_note", "out_note", "choke_group"):
        if hasattr(c, a):
            out[a] = getattr(c, a)
    try:
        out["volume"] = str(c.mixer_device.volume)
        out["pan"] = str(c.mixer_device.panning)
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- Simpler

_SIMPLER_PROPS = ("playback_mode", "slicing_playback_mode", "voices", "retrigger", "pad_slicing")
_SAMPLE_PROPS = ("warping", "warp_mode", "gain", "start_marker", "end_marker", "slicing_style",
                 "slicing_sensitivity", "slicing_beat_division", "slicing_region_count",
                 "beats_granulation_resolution", "beats_transient_envelope", "beats_transient_loop_mode",
                 "complex_pro_envelope", "complex_pro_formants", "texture_flux", "texture_grain_size",
                 "tones_grain_size")


@op("simpler", mutates=True)
def simpler(ctx, action="info", device=None, track=None, index=None, values=None, sample_values=None,
            beats=None, time=None, to_time=None, file_path=None, slice_index=None):
    """action: info | set (values: Simpler props, sample_values: Sample props) | crop | reverse |
    warp_as (beats) | warp_double | warp_half | guess_length | replace_sample (file_path) |
    insert_slice (time) | move_slice (time -> to_time) | remove_slice (time) | clear_slices | reset_slices |
    select_slice (slice_index). With only ``track`` the track's first Simpler is used."""
    if device is None and index is None and track is not None:
        found = [i for i, x in enumerate(track_of(ctx, track).devices)
                 if getattr(x, "class_name", "") == "OriginalSimpler"]
        if not found:
            raise OpError("no Simpler on track %r; pass device or index" % track)
        index = found[0]
    d = device_of(ctx, device, track, index)
    if getattr(d, "class_name", "") != "OriginalSimpler" and not hasattr(d, "sample"):
        raise OpError("%r is not a Simpler" % d.name)

    def smp():
        s = d.sample
        if s is None:
            raise OpError("Simpler has no sample loaded")
        return s

    def info():
        out = {"name": d.name, "ref": ctx.lom.ref(d)}
        for a in _SIMPLER_PROPS + ("multi_sample_mode", "can_warp_as", "can_warp_double", "can_warp_half",
                                   "playing_position", "playing_position_enabled"):
            if hasattr(d, a):
                out[a] = ctx.lom.ser(getattr(d, a))
        s = d.sample
        if s is not None:
            si = {"file_path": s.file_path, "length": s.length, "sample_rate": s.sample_rate,
                  "slices": list(s.slices) if hasattr(s, "slices") else []}
            for a in _SAMPLE_PROPS:
                if hasattr(s, a):
                    si[a] = ctx.lom.ser(getattr(s, a))
            try:
                si["gain_display"] = s.gain_display_string()
            except Exception:
                pass
            try:
                si["warp_markers"] = [{"beat_time": m.beat_time, "sample_time": m.sample_time}
                                      for m in s.warp_markers]
            except Exception:
                pass
            out["sample"] = si
        return out

    def setp():
        for k, v in (values or {}).items():
            if k not in _SIMPLER_PROPS:
                raise OpError("cannot set %r on Simpler; one of %s" % (k, list(_SIMPLER_PROPS)))
            setattr(d, k, v)
        if sample_values:
            s = smp()
            for k, v in sample_values.items():
                if k not in _SAMPLE_PROPS:
                    raise OpError("cannot set %r on Sample; one of %s" % (k, list(_SAMPLE_PROPS)))
                setattr(s, k, v)
        return info()

    table = {
        "info": info, "set": setp,
        "crop": lambda: (d.crop(), info())[1],
        "reverse": lambda: (d.reverse(), info())[1],
        "warp_as": lambda: (d.warp_as(float(beats)), info())[1],
        "warp_double": lambda: (d.warp_double(), info())[1],
        "warp_half": lambda: (d.warp_half(), info())[1],
        "guess_length": lambda: {"beats": d.guess_playback_length()},
        "replace_sample": lambda: (_need(d, "replace_sample", "SimplerDevice")(str(file_path)), info())[1],
        "insert_slice": lambda: (smp().insert_slice(int(time)), info())[1],
        "move_slice": lambda: (smp().move_slice(int(time), int(to_time)), info())[1],
        "remove_slice": lambda: (smp().remove_slice(int(time)), info())[1],
        "clear_slices": lambda: (smp().clear_slices(), info())[1],
        "reset_slices": lambda: (smp().reset_slices(), info())[1],
        "select_slice": lambda: (setattr(d.view, "selected_slice", smp().slices[int(slice_index)]), info())[1],
    }
    return _dispatch(table, action)


# ---------------------------------------------------------------- Looper

@op("looper", mutates=True)
def looper(ctx, action, device=None, track=None, index=None, clip_slot=None, values=None):
    """action: info | record | overdub | play | stop | clear | undo | double_speed | half_speed |
    double_length | half_length | export (clip_slot path) | set (overdub_after_record, record_length_index)"""
    d = device_of(ctx, device, track, index)
    if not hasattr(d, "overdub"):
        raise OpError("%r is not a Looper" % d.name)

    def info():
        return dict((a, ctx.lom.ser(getattr(d, a))) for a in ("loop_length", "overdub_after_record",
                                                              "record_length_index", "record_length_list",
                                                              "tempo") if hasattr(d, a))

    simple = ("record", "overdub", "play", "stop", "clear", "undo", "double_speed", "half_speed",
              "double_length", "half_length")
    if action in simple:
        getattr(d, action)()
        return info()
    if action == "export":
        d.export_to_clip_slot(ctx.lom.resolve(clip_slot))
        return {"exported": True}
    if action == "set":
        _apply(d, values, ("overdub_after_record", "record_length_index"))
        return info()
    if action == "info":
        return info()
    raise OpError("unknown looper action %r" % action)


# ---------------------------------------------------------------- recording / capture

@op("record", mutates=True)
def record(ctx, action, track=None, slot=None, length=None, launch_quantization=None, destination=0, value=None):
    """action: clip_slot (record into an empty slot, optional fixed length in beats) |
    session (trigger_session_record, optional length) | capture_midi | capture_scene |
    arrangement (toggle record_mode) | overdub | punch_in | punch_out | automation_arm | count_in"""
    s = ctx.song()

    def clip_slot_rec():
        t = track_of(ctx, track)
        cs = t.clip_slots[int(slot)]
        if not t.arm and getattr(t, "can_be_armed", True):
            t.arm = True
        kw = {}
        if length is not None:
            kw["record_length"] = float(length)
        if launch_quantization is not None:
            kw["launch_quantization"] = int(launch_quantization)
        try:
            cs.fire(**kw)
        except TypeError:
            cs.fire(*[kw[k] for k in ("record_length", "launch_quantization") if k in kw])
        return {"recording": True, "slot": int(slot), "track": t.name}

    def toggle(prop):
        cur = getattr(s, prop)
        setattr(s, prop, (not cur) if value is None else bool(value))
        return {prop: getattr(s, prop)}

    def session():
        fn = _need(s, "trigger_session_record", "Song")
        fn(float(length)) if length is not None else fn()
        return {"session_record": s.session_record}

    def capture_midi():
        if not s.can_capture_midi:
            raise OpError("nothing to capture (play something on an armed MIDI track first)")
        try:
            s.capture_midi(int(destination))
        except TypeError:
            s.capture_midi()
        return {"captured": True}

    table = {
        "clip_slot": clip_slot_rec, "session": session, "capture_midi": capture_midi,
        "capture_scene": lambda: (s.capture_and_insert_scene(), {"scenes": len(s.scenes)})[1],
        "arrangement": lambda: toggle("record_mode"), "overdub": lambda: toggle("arrangement_overdub"),
        "punch_in": lambda: toggle("punch_in"), "punch_out": lambda: toggle("punch_out"),
        "automation_arm": lambda: toggle("session_automation_record"),
        "count_in": lambda: {"count_in_duration": s.count_in_duration, "is_counting_in": s.is_counting_in},
    }
    return _dispatch(table, action)


# ---------------------------------------------------------------- song navigation / time

@op("song.action", mutates=True)
def song_action(ctx, action, beats=None, format=0, quantized=None):
    """action: play_selection | scrub_by | jump_by | nudge_up | nudge_down | back_to_arranger |
    force_link_beat_time | stop_all_clips | smpte | beats_time | loop_beats | re_enable_automation"""
    s = ctx.song()

    def smpte():
        t = s.get_current_smpte_song_time(int(format))
        return {"hours": t.hours, "minutes": t.minutes, "seconds": t.seconds, "frames": t.frames}

    def bbt(t):
        return {"bars": t.bars, "beats": t.beats, "sub_division": t.sub_division, "ticks": t.ticks}

    def stop_all():
        if quantized is None:
            s.stop_all_clips()
        else:
            s.stop_all_clips(bool(quantized))
        return {"ok": 1}

    def nudge(attr):
        # Song.nudge_up/nudge_down are HELD buttons: left at True they keep the transport running fast
        # (seen 2026-11-16: both stuck True, Live played ~186 BPM at tempo 111). Press, then release next tick.
        setattr(s, attr, True)

        def release():
            try:
                setattr(s, attr, False)
            except Exception:
                pass
        ctx.control.append(release)
        return {"ok": 1, "released": "next tick"}

    table = {
        "play_selection": lambda: (s.play_selection(), {"ok": 1})[1],
        "scrub_by": lambda: (s.scrub_by(float(beats)), {"time": s.current_song_time})[1],
        "jump_by": lambda: (s.jump_by(float(beats)), {"time": s.current_song_time})[1],
        "nudge_up": lambda: nudge("nudge_up"),
        "nudge_down": lambda: nudge("nudge_down"),
        "back_to_arranger": lambda: (setattr(s, "back_to_arranger", False), {"ok": 1})[1],
        "force_link_beat_time": lambda: (s.force_link_beat_time(), {"ok": 1})[1],
        "stop_all_clips": stop_all,
        "smpte": smpte,
        "beats_time": lambda: bbt(s.get_current_beats_song_time()),
        "loop_beats": lambda: {"start": bbt(s.get_beats_loop_start()), "length": bbt(s.get_beats_loop_length())},
        "re_enable_automation": lambda: (s.re_enable_automation(), {"ok": 1})[1],
    }
    return _dispatch(table, action)


@op("cue", mutates=True)
def cue(ctx, action="list", index=None, name=None, time=None):
    """action: list | toggle (set_or_delete_cue at the playhead, or at ``time``) | jump | rename |
    next | prev"""
    s = ctx.song()

    def listing():
        return [{"index": i, "name": c.name, "time": c.time} for i, c in
                enumerate(sorted(s.cue_points, key=lambda c: c.time))]

    def by_index():
        return sorted(s.cue_points, key=lambda c: c.time)[int(index)]

    def toggle():
        # Live adds/removes the cue at its own playhead position, which may differ from a
        # just-requested time while stopped - so report what actually changed.
        before = dict((round(c.time, 6), c.name) for c in s.cue_points)
        if time is not None and abs(s.current_song_time - float(time)) > 1e-6:
            # Live applies a playhead write on a later tick, and a read right after ANY write shows the old
            # position - so write only when needed, never toggle in the same call, and let the caller retry.
            was = s.current_song_time
            s.current_song_time = float(time)
            raise OpError("cue playhead has not reached %.3f yet (at %.4f); retry" % (float(time), was))
        s.set_or_delete_cue()
        after = dict((round(c.time, 6), c.name) for c in s.cue_points)
        added = [{"time": t, "name": after[t]} for t in after if t not in before]
        removed = [{"time": t, "name": before[t]} for t in before if t not in after]
        if time is not None:
            stray = [a for a in added + removed if abs(a["time"] - float(time)) > 1e-3]
            if stray:
                # While stopped Live places locators at the arrangement insert marker / time-selection start,
                # not at current_song_time (Live 12.4.16: asked for 1.0, every try landed at 0.0537, and the
                # playhead jumped there). The LOM cannot move that marker. Undo at the same spot and explain.
                s.set_or_delete_cue()
                raise OpError("Live placed the locator at %.4f (the arrangement insert marker / selection start "
                              "while stopped), not at %.3f; it was undone. Click the target spot in the "
                              "arrangement (or clear the time selection), or toggle while playing."
                              % (stray[0]["time"], float(time)))
        return {"added": added, "removed": removed, "requested_time": time,
                "playhead": s.current_song_time, "items": listing()}

    return _dispatch({
        "list": listing, "toggle": toggle,
        "jump": lambda: (by_index().jump(), {"time": s.current_song_time})[1],
        "rename": lambda: (setattr(by_index(), "name", name), listing())[1],
        "next": lambda: (s.jump_to_next_cue(), {"time": s.current_song_time})[1],
        "prev": lambda: (s.jump_to_prev_cue(), {"time": s.current_song_time})[1],
    }, action)


# ---------------------------------------------------------------- take lanes

@op("take_lanes", mutates=True)
def take_lanes(ctx, action="list", track=None, lane=None, name=None, start_time=None, length=None,
               file_path=None):
    """action: list | create | rename | create_midi_clip | create_audio_clip"""
    t = track_of(ctx, track)
    _need(t, "take_lanes", "Track")

    def listing():
        return [{"index": i, "name": ln.name, "ref": ctx.lom.ref(ln, with_path=False),
                 "clips": [{"name": c.name, "start_time": c.start_time, "end_time": c.end_time}
                           for c in ln.arrangement_clips]} for i, ln in enumerate(t.take_lanes)]

    def create():
        t.create_take_lane()
        if name is not None:
            t.take_lanes[-1].name = name
        return listing()

    def get_lane():
        return t.take_lanes[int(lane)]

    return _dispatch({
        "list": listing, "create": create,
        "rename": lambda: (setattr(get_lane(), "name", name), listing())[1],
        "create_midi_clip": lambda: ctx.lom.ref(velocity_on(get_lane().create_midi_clip(float(start_time),
                                                                                   float(length)))),
        "create_audio_clip": lambda: ctx.lom.ref(get_lane().create_audio_clip(str(file_path), float(start_time))),
    }, action)


# ---------------------------------------------------------------- grooves / tuning / scale

@op("groove", mutates=True)
def groove(ctx, action="list", groove=None, values=None, clip=None, track=None, slot=None):
    """action: list | set (groove index, values: base/quantization_amount/random_amount/timing_amount/
    velocity_amount/name) | assign (groove index or None -> clip) | global (values: groove_amount)"""
    s = ctx.song()
    pool = s.groove_pool

    def listing():
        return {"groove_amount": s.groove_amount,
                "grooves": [{"index": i, "name": g.name, "base": g.base, "quantization_amount": g.quantization_amount,
                             "random_amount": g.random_amount, "timing_amount": g.timing_amount,
                             "velocity_amount": g.velocity_amount} for i, g in enumerate(pool.grooves)]}

    def assign():
        c = clip_of(ctx, clip, track, slot)
        if groove is None:
            raise OpError("Live's API cannot remove a clip's groove; assign another groove index instead")
        c.groove = pool.grooves[int(groove)]
        return {"clip": c.name, "groove": c.groove.name if c.groove is not None else None}

    return _dispatch({
        "list": listing,
        "set": lambda: (_apply(pool.grooves[int(groove)], values, ("base", "quantization_amount", "random_amount",
                                                                   "timing_amount", "velocity_amount", "name")),
                        listing())[1],
        "assign": assign,
        "global": lambda: (_apply(s, values, ("groove_amount", "swing_amount")), listing())[1],
    }, action)


@op("tuning", mutates=True)
def tuning(ctx, values=None):
    """Read (no values) or set the set's tuning system: name, lowest_note, highest_note,
    reference_pitch, note_tunings."""
    s = ctx.song()
    if not hasattr(s, "tuning_system"):
        raise OpError("Song.tuning_system is not available in this Live version")
    ts = s.tuning_system
    if ts is None:
        if values:
            raise OpError("no tuning system is loaded in this set (12-TET); load a .ascl tuning first")
        return {"tuning_system": None, "note": "no tuning system loaded - standard 12-TET"}
    if values:
        _apply(ts, values, ("name", "lowest_note", "highest_note", "reference_pitch", "note_tunings"))
    return dict((a, ctx.lom.ser(getattr(ts, a))) for a in ("name", "pseudo_octave_in_cents", "lowest_note",
                                                           "highest_note", "reference_pitch", "note_tunings")
                if hasattr(ts, a))


# ---------------------------------------------------------------- application

@op("app", mutates=True)
def app(ctx, action="info", view=None, direction=0, modifier=False, button=None):
    """action: info (version, CPU, dialogs, views, control surfaces) | press_dialog_button (button) |
    show_view | hide_view | focus_view | is_view_visible | scroll_view | zoom_view | toggle_browse"""
    a = ctx.app()
    av = a.view

    def info():
        out = {"version": a.get_version_string() if hasattr(a, "get_version_string") else None}
        for k in ("get_variant", "get_build_id"):
            if hasattr(a, k):
                try:
                    out[k[4:]] = ctx.lom.ser(getattr(a, k)())
                except Exception:
                    pass
        if hasattr(a, "unavailable_features"):
            out["unavailable_features"] = ctx.lom.ser(a.unavailable_features)
        for k in ("average_process_usage", "peak_process_usage", "open_dialog_count", "current_dialog_message",
                  "current_dialog_button_count"):
            if hasattr(a, k):
                out[k] = ctx.lom.ser(getattr(a, k))
        try:
            out["main_views"] = list(av.available_main_views())
            out["visible_views"] = [v for v in out["main_views"] if av.is_view_visible(v)]
        except Exception:
            pass
        for k in ("browse_mode", "focused_document_view"):
            if hasattr(av, k):
                out[k] = ctx.lom.ser(getattr(av, k))
        try:
            out["control_surfaces"] = [type(cs).__name__ if cs is not None else None for cs in a.control_surfaces]
        except Exception:
            pass
        return out

    def press():
        n = getattr(a, "current_dialog_button_count", 0)
        if not n:
            raise OpError("no dialog is open")
        a.press_current_dialog_button(int(button or 0))
        return info()

    return _dispatch({
        "info": info, "press_dialog_button": press,
        "show_view": lambda: (av.show_view(view), info())[1],
        "hide_view": lambda: (av.hide_view(view), info())[1],
        "focus_view": lambda: (av.focus_view(view), info())[1],
        "is_view_visible": lambda: {"view": view, "visible": av.is_view_visible(view)},
        "scroll_view": lambda: (av.scroll_view(int(direction), view or "", bool(modifier)), {"ok": 1})[1],
        "zoom_view": lambda: (av.zoom_view(int(direction), view or "", bool(modifier)), {"ok": 1})[1],
        "toggle_browse": lambda: (av.toggle_browse(), info())[1],
        "message": lambda: (_message(ctx, str(view or "")), {"shown": view})[1],
    }, action)


def _message(ctx, text):
    hook = ctx.hooks.get("message")
    if hook is None:
        raise OpError("status-bar messages are not supported by this host")
    hook(text)


@op("view.set", mutates=True)
def view_set(ctx, values):
    """Song.View properties: draw_mode, follow_song, selected_parameter, selected_chain,
    mod_mapping_device, highlighted_clip_slot, detail_clip, selected_track, selected_scene."""
    v = ctx.song().view
    out = {}
    for k, x in values.items():
        if k not in ("draw_mode", "follow_song", "selected_chain", "mod_mapping_device", "highlighted_clip_slot",
                     "detail_clip", "selected_track", "selected_scene"):
            raise OpError("cannot set Song.View.%s" % k)
        setattr(v, k, ctx.lom.coerce(x))
        out[k] = ctx.lom.ser(getattr(v, k))
    return out


# ---------------------------------------------------------------- persistent per-set data

@op("set_data", mutates=True)
def set_data(ctx, key, value=None, delete=False, track=None):
    """Store JSON data inside the Live Set (saved with the .als) - on the song, or on a track."""
    owner = ctx.song() if track is None else track_of(ctx, track)
    fn = _need(owner, "set_data", type(owner).__name__)
    fn("agent_bridge:" + key, None if delete else json.dumps(value))
    return {"key": key, "stored": not delete}


@op("get_data")
def get_data(ctx, key, track=None):
    owner = ctx.song() if track is None else track_of(ctx, track)
    raw = _need(owner, "get_data", type(owner).__name__)("agent_bridge:" + key, None)
    return {"key": key, "value": json.loads(raw) if raw else None}


# ---------------------------------------------------------------- tracks extras

@op("track.extra", mutates=True)
def track_extra(ctx, action, track=None, clip=None, beats=None, index=None, values=None):
    """action: meters | group | fold | unfold | delete_arrangement_clip (clip) | jump_in_clip (beats) |
    duplicate_slot (index) | view (values: device_insert_mode, is_collapsed) | mixer (values:
    crossfade_assign, panning_mode, left_split_stereo, right_split_stereo, track_activator, cue_volume,
    crossfader, song_tempo)"""
    t = track_of(ctx, track)

    def _safe(attrs):
        out = {}
        for a in attrs:
            try:
                out[a] = getattr(t, a)
            except Exception:
                pass  # e.g. MIDI tracks have no stereo input meters
        return out

    def meters():
        return _safe(("output_meter_level", "output_meter_left", "output_meter_right", "input_meter_level",
                      "input_meter_left", "input_meter_right", "performance_impact", "is_frozen",
                      "can_be_frozen"))

    def group():
        out = _safe(("is_grouped", "is_foldable", "fold_state", "is_visible"))
        try:
            g = t.group_track
            out["group"] = g.name if g is not None else None
        except Exception:
            pass
        return out

    def fold(state):
        if not getattr(t, "is_foldable", False):
            raise OpError("track %r is not a group track, so it cannot be folded" % t.name)
        t.fold_state = state
        return group()

    def mixer():
        md = t.mixer_device
        out = {}
        for k, v in (values or {}).items():
            if k in ("crossfade_assign", "panning_mode"):
                setattr(md, k, int(v))
                out[k] = getattr(md, k)
            elif k in ("left_split_stereo", "right_split_stereo", "track_activator", "cue_volume", "crossfader",
                       "song_tempo"):
                p = getattr(md, k)
                out[k] = set_param(p, display=v) if isinstance(v, str) else set_param(p, value=v)
            else:
                raise OpError("cannot set mixer %r" % k)
        if not values:
            for k in ("crossfade_assign", "panning_mode"):
                try:
                    out[k] = int(getattr(md, k))
                except Exception:
                    pass
            # cue_volume / crossfader / song_tempo exist on the master track only
            for k in ("cue_volume", "crossfader", "song_tempo", "track_activator", "left_split_stereo",
                      "right_split_stereo"):
                try:
                    p = getattr(md, k)
                except Exception:
                    continue
                if p is not None:
                    out[k] = param_info(p)
        return out

    def view_vals():
        _apply(t.view, values, ("device_insert_mode", "is_collapsed"))
        return {"device_insert_mode": ctx.lom.ser(getattr(t.view, "device_insert_mode", None)),
                "is_collapsed": getattr(t.view, "is_collapsed", None)}

    return _dispatch({
        "meters": meters, "group": group,
        "fold": lambda: fold(1),
        "unfold": lambda: fold(0),
        "delete_arrangement_clip": lambda: (t.delete_clip(ctx.lom.resolve(clip)), {"ok": 1})[1],
        "jump_in_clip": lambda: (t.jump_in_running_session_clip(float(beats)), {"ok": 1})[1],
        "duplicate_slot": lambda: {"slot": t.duplicate_clip_slot(int(index))},
        "view": view_vals, "mixer": mixer,
    }, action)


@op("scene.launch", mutates=True)
def scene_launch(ctx, index, force_legato=False, as_selected=False, select=True):
    sc = ctx.song().scenes[int(index)]
    if as_selected:
        sc.fire_as_selected(bool(force_legato))
    else:
        try:
            sc.fire(force_legato=bool(force_legato), can_select_scene_on_launch=bool(select))
        except TypeError:
            sc.fire(bool(force_legato))
    return {"fired": int(index)}


# ---------------------------------------------------------------- bridge maintenance

@op("bridge.reload")
def bridge_reload(ctx):
    """Reload the Remote Script's code from disk (after `live-agent-bridge install`) without
    touching Live's preferences. The connection drops; clients reconnect automatically."""
    hook = ctx.hooks.get("reload")
    if hook is None:
        raise OpError("reload is not supported by this host")
    hook()
    return {"reloading": True}


@op("bridge.timer")
def bridge_timer(ctx, interval_ms):
    """Restart Live.Base.Timer with a new interval (persisted in config.json)."""
    hook = ctx.hooks.get("timer")
    if hook is None:
        raise OpError("timer control is not supported by this host")
    ctx.tick_interval = 0.1
    return {"timer_active": bool(hook(int(interval_ms))), "interval_ms": int(interval_ms)}


# ---------------------------------------------------------------- coverage audit

_CATALOG = None


def catalog():
    global _CATALOG
    if _CATALOG is None:
        with open(os.path.join(os.path.dirname(__file__), "lom_catalog.json"), "r") as f:
            _CATALOG = json.load(f)
    return _CATALOG


def _doc_class_name(obj):
    """Live.Song.Song -> 'Song', Live.Song.View -> 'Song.View' (the LOM documentation naming)."""
    cls = type(obj)
    full = cls.__module__ or ""
    if not full.startswith("Live."):
        return cls.__name__
    mod = full.split(".")[-1]
    return cls.__name__ if cls.__name__ == mod else "%s.%s" % (mod, cls.__name__)


@op("lom.audit")
def lom_audit(ctx, max_objects=4000):
    """Walk the current set, introspect every reachable object and compare its Python API
    with the official LOM catalog: what is documented & present, documented but missing,
    and available but undocumented (Python-only extras)."""
    cat = catalog()["classes"]
    seen_cls = {}
    stack = [ctx.song(), ctx.app()]
    visited = set()
    n = 0
    while stack and n < max_objects:
        obj = stack.pop()
        key = getattr(obj, "_live_ptr", id(obj))
        if key in visited:
            continue
        visited.add(key)
        n += 1
        cname = _doc_class_name(obj)
        if cname not in seen_cls:
            seen_cls[cname] = set(x for x in dir(obj) if not x.startswith("_"))
        for attr in ("tracks", "return_tracks", "master_track", "scenes", "clip_slots", "clip", "devices", "chains",
                     "return_chains", "drum_pads", "view", "mixer_device", "sample", "cue_points", "groove_pool",
                     "grooves", "tuning_system", "take_lanes", "arrangement_clips", "parameters"):
            try:
                v = getattr(obj, attr)
            except Exception:
                continue
            if v is None:
                continue
            if hasattr(v, "_live_ptr") or not hasattr(v, "__len__"):
                stack.append(v)
            else:
                try:
                    for x in list(v)[:64]:
                        if x is not None:
                            stack.append(x)
                except Exception:
                    pass
        if n % 50 == 0:
            yield None
    report = {"objects_walked": n, "classes": {}}
    totals = {"documented": 0, "present": 0, "missing": 0, "extra": 0}
    for doc_name, spec in cat.items():
        names = seen_cls.get(doc_name)
        if names is None:
            report["classes"][doc_name] = {"status": "not in this set (add one to audit it)"}
            continue
        members = set(spec["members"])
        if spec.get("parent") and spec["parent"] in cat:
            members |= set(cat[spec["parent"]]["members"])
        present = sorted(m for m in members if m in names)
        missing = sorted(m for m in members if m not in names and m != "__str__")
        extra = sorted(m for m in names - members if not m.endswith("_listener") and not m.endswith("_has_listener")
                       and m != "canonical_parent")
        totals["documented"] += len(members)
        totals["present"] += len(present)
        totals["missing"] += len(missing)
        totals["extra"] += len(extra)
        report["classes"][doc_name] = {"present": len(present), "missing": missing, "extra_python_only": extra}
    report["totals"] = totals
    return report
