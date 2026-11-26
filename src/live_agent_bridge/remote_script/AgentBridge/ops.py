"""High-level operations. Every op runs on Live's main thread in a single tick
unless it returns a generator, in which case the scheduler time-slices it.

Signature: ``op(ctx, **args) -> result | generator``.
"""

import re

from .lom import LomError, is_lom, ordered_props

OPS = {}


def op(name, mutates=False):
    def deco(fn):
        fn.mutates = mutates
        OPS[name] = fn
        return fn
    return deco


class OpError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def _live():
    import Live  # provided by Live (or the test double)
    return Live


def track_of(ctx, ref):
    song = ctx.song()
    if isinstance(ref, bool):
        raise OpError("track must be an index or path")
    if isinstance(ref, int):
        try:
            return song.tracks[ref]
        except IndexError:
            raise OpError("track index %d out of range (0..%d)" % (ref, len(song.tracks) - 1)) from None
    if isinstance(ref, str):
        low = ref.strip().lower()
        if low in ("master", "master_track"):
            return song.master_track
        m = re.match(r"^(?:return|r)\s*(\d+)$", low)
        if m:
            return song.return_tracks[int(m.group(1))]
        if not re.match(r"^(live_set|track|id|selected_track|song)\b", low):
            # name lookup
            for t in list(song.tracks) + list(song.return_tracks):
                if t.name == ref:
                    return t
            raise OpError("no track named %r" % ref)
    return ctx.lom.resolve(ref)


def clip_slot_of(ctx, track, slot):
    t = track_of(ctx, track)
    try:
        return t.clip_slots[slot]
    except (IndexError, AttributeError):
        raise OpError("clip slot %s not available on track %r" % (slot, t.name)) from None


def clip_of(ctx, clip=None, track=None, slot=None):
    if clip is not None:
        c = ctx.lom.resolve(clip)
    else:
        if track is None or slot is None:
            raise OpError("give either clip=<path> or track+slot")
        cs = clip_slot_of(ctx, track, slot)
        c = cs.clip
    if c is None:
        raise OpError("no clip there")
    return c


_PATH_ROOTS = ("live_set", "live_app", "id", "track", "tracks", "return", "master", "scene")


def device_of(ctx, device=None, track=None, index=None):
    if device is not None:
        # A plain device name with a track (as the MCP tools take it) rather than a LOM path.
        if (isinstance(device, str) and track is not None and index is None
                and device.split(" ", 1)[0].lower() not in _PATH_ROOTS):
            index = device
        else:
            return ctx.lom.resolve(device)
    t = track_of(ctx, track if track is not None else "selected_track")
    devs = t.devices
    if index is None:
        raise OpError("give device=<path> or track+index")
    if isinstance(index, str):
        for d in devs:
            if d.name == index:
                return d
        raise OpError("no device named %r on %r" % (index, t.name))
    return devs[index]


def _display(p):
    # str(p) can lag one tick behind a just-set value (plug-ins refresh their text asynchronously);
    # str_for_value computes the text for the current value immediately.
    try:
        return p.str_for_value(p.value)
    except Exception:
        return _safe_str(p)


def param_info(p, i=None):
    out = {"name": p.name, "value": p.value, "min": p.min, "max": p.max,
           "display": _display(p), "quantized": bool(getattr(p, "is_quantized", False))}
    if i is not None:
        out["index"] = i
    if out["quantized"]:
        try:
            out["items"] = list(p.value_items)
        except Exception:
            pass
    if not getattr(p, "is_enabled", True):
        out["enabled"] = False
    st = getattr(p, "automation_state", 0)
    if st:
        out["automation_state"] = int(st)  # 1 = automated, 2 = overridden
        if int(st) == 2:
            out["warning"] = ("this parameter's arrangement automation is OVERRIDDEN by a manual change (seen "
                              "2026-11-16: a fader write silenced its automated sections); call automation "
                              "action=re_enable with this parameter to hand control back to the automation")
    return out


def _safe_str(p):
    try:
        return str(p)
    except Exception:
        return None


_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _parse_display(text):
    if text is None:
        return None
    t = text.strip().lower().replace(",", ".").replace("−", "-")
    if "inf" in t:
        return float("-inf") if "-" in t else float("inf")
    m = _NUM.search(t)
    if not m:
        return None
    v = float(m.group(0))
    rest = t[m.end():].strip()
    if rest.startswith("k"):
        v *= 1000.0
    elif rest.startswith("ms"):
        v /= 1000.0
    return v


def _display_step(text):
    """Half of the last shown decimal, in the same units _parse_display returns."""
    t = (text or "").strip().lower().replace(",", ".")
    m = _NUM.search(t)
    if not m:
        return 0.0
    digits = m.group(0).split(".")[1] if "." in m.group(0) else ""
    step = 0.5 * 10.0 ** -len(digits)
    rest = t[m.end():].strip()
    if rest.startswith("k"):
        step *= 1000.0
    elif rest.startswith("ms"):
        step /= 1000.0
    return step


def set_param(p, value=None, normalized=None, display=None):
    """Set a DeviceParameter by raw value, 0..1 normalized, or display text
    such as "-6 dB", "1.2 kHz" or a value item name."""
    if not getattr(p, "is_enabled", True):
        raise OpError("parameter %r is not enabled (macro-mapped or automated?)" % p.name)
    lo, hi = p.min, p.max
    if display is not None:
        if getattr(p, "is_quantized", False):
            items = [str(x) for x in p.value_items]
            low = [x.lower() for x in items]
            d = str(display).lower()
            if d in low:
                target = lo + low.index(d)
            else:
                raise OpError("%r is not one of %s" % (display, items))
        else:
            want = _parse_display(str(display))
            if want is not None and hasattr(p, "display_value"):
                # Live 12.x: set the value in display units directly (exact, no search)
                before = p.value
                try:
                    p.display_value = want
                    shown = p.str_for_value(p.value) if hasattr(p, "str_for_value") else _safe_str(p)
                    got = _parse_display(shown)
                    # Relative check plus the display's own rounding step; an absolute floor would
                    # accept wrong values for small units (e.g. ms parsed to seconds).
                    if got is not None and abs(got - want) <= max(0.02 * abs(want), _display_step(shown)):
                        return param_info(p)
                except Exception:
                    pass
                p.value = before  # units differed; fall back to searching the display curve
            target = _solve_display(p, str(display))
    elif normalized is not None:
        target = lo + float(normalized) * (hi - lo)
    elif value is not None:
        target = float(value)
    else:
        raise OpError("give value, normalized or display")
    target = max(lo, min(hi, target))
    if getattr(p, "is_quantized", False):
        target = round(target)
    _gesture(p, target)
    return param_info(p)


def _gesture(p, value):
    """Set a parameter inside begin/end_gesture (Python API) so the change is recorded as
    automation when automation recording is armed, exactly like a mouse/controller move."""
    begin = getattr(p, "begin_gesture", None)
    if begin is None:
        p.value = value
        return
    begin()
    try:
        p.value = value
    finally:
        p.end_gesture()


def _solve_display(p, text):
    want = _parse_display(text)
    if want is None:
        raise OpError("cannot parse %r as a number" % text)
    lo, hi = p.min, p.max
    def f(v):
        return _parse_display(p.str_for_value(v))
    flo, fhi = f(lo), f(hi)
    if flo is None or fhi is None:
        raise OpError("parameter display is not numeric; use value or normalized")
    increasing = fhi >= flo
    if (increasing and want <= flo) or (not increasing and want >= flo):
        return lo
    if (increasing and want >= fhi) or (not increasing and want <= fhi):
        return hi
    a, b = lo, hi
    for _ in range(40):
        mid = (a + b) / 2.0
        fm = f(mid)
        if fm is None:
            break
        if (fm < want) == increasing:
            a = mid
        else:
            b = mid
    fa, fb = f(a), f(b)
    if fa is None or fb is None:
        return (a + b) / 2.0
    return b if abs(fb - want) <= abs(fa - want) else a


def apply_props(obj, props, allowed=None):
    done = {}
    for k, v in ordered_props(obj, props):
        if k.startswith("_") or (allowed is not None and k not in allowed):
            raise OpError("property %r cannot be set here" % k)
        setattr(obj, k, v)
        done[k] = getattr(obj, k)
    return done


def track_summary(ctx, t, i=None, kind="track"):
    out = {"name": t.name, "kind": kind}
    if i is not None:
        out["index"] = i
    for attr in ("mute", "solo", "arm", "color", "has_midi_input", "has_audio_input",
                 "is_foldable", "is_grouped", "playing_slot_index", "fired_slot_index"):
        try:
            v = getattr(t, attr)
        except Exception:
            continue
        out[attr] = v
    try:
        md = t.mixer_device
        out["volume"] = _safe_str(md.volume)
        out["pan"] = _safe_str(md.panning)
    except Exception:
        pass
    try:
        out["devices"] = [d.name for d in t.devices]
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- status / song

@op("status")
def status(ctx):
    return ctx.status()


@op("overview")
def overview(ctx, fresh=None, max_age=None):
    # fresh/max_age only steer the cached fast path; here we always build a new snapshot
    return ctx.build_snapshot()


@op("song.set", mutates=True)
def song_set(ctx, **props):
    return apply_props(ctx.song(), props)


def _seek(s, beats):
    # When stopped, Live starts playback (and recording) from start_time, not the playhead.
    if not s.is_playing:
        s.start_time = beats
    s.current_song_time = beats


@op("transport", mutates=True)
def transport(ctx, action, value=None):
    if action in ("undo", "redo"):
        raise OpError("Global Song undo/redo is unavailable through AgentBridge")
    s = ctx.song()
    acts = {
        "play": lambda: s.start_playing(),
        "stop": lambda: s.stop_playing(),
        "continue": lambda: s.continue_playing(),
        "stop_all_clips": lambda: s.stop_all_clips(),
        "tap_tempo": lambda: s.tap_tempo(),
        "record": lambda: setattr(s, "record_mode", not s.record_mode),
        "session_record": lambda: setattr(s, "session_record", not s.session_record),
        "metronome": lambda: setattr(s, "metronome", not s.metronome),
        "loop": lambda: setattr(s, "loop", not s.loop),
        "jump": lambda: s.jump_by(float(value or 0)),
        "seek": lambda: _seek(s, float(value or 0)),
        "capture_midi": lambda: s.capture_midi(),
        "re_enable_automation": lambda: s.re_enable_automation(),
        "jump_to_next_cue": lambda: s.jump_to_next_cue(),
        "jump_to_prev_cue": lambda: s.jump_to_prev_cue(),
    }
    if action not in acts:
        raise OpError("unknown action %r; one of %s" % (action, sorted(acts)))
    acts[action]()
    return {"is_playing": s.is_playing, "current_song_time": s.current_song_time,
            "record_mode": s.record_mode, "metronome": s.metronome, "loop": s.loop}


# ---------------------------------------------------------------- generic LOM

@op("lom.get")
def lom_get(ctx, path, props=None):
    return ctx.lom.get(path, props)


@op("lom.set", mutates=True)
def lom_set(ctx, path, values):
    return ctx.lom.set(path, values)


@op("lom.call", mutates=True)
def lom_call(ctx, path, method, args=None, kwargs=None):
    if method in ("undo", "redo") and type(ctx.lom.resolve(path)).__name__ == "Song":
        raise OpError("Global Song undo/redo is unavailable through AgentBridge")
    return ctx.lom.call(path, method, args, kwargs)


@op("lom.children")
def lom_children(ctx, path, attr, offset=0, limit=100):
    return ctx.lom.children(path, attr, offset, limit)


@op("lom.describe")
def lom_describe(ctx, path):
    return ctx.lom.describe(ctx.lom.resolve(path), values=False)


# ---------------------------------------------------------------- tracks

@op("track.info")
def track_info(ctx, track, clips=True):
    t = track_of(ctx, track)
    out = track_summary(ctx, t)
    out["ref"] = ctx.lom.ref(t)
    try:
        md = t.mixer_device
        out["mixer"] = {"volume": param_info(md.volume), "pan": param_info(md.panning),
                        "sends": [param_info(p, i) for i, p in enumerate(md.sends)]}
    except Exception:
        pass
    out["devices"] = [{"index": i, "name": d.name, "class_name": getattr(d, "class_name", None),
                       "type": str(getattr(d, "type", "")), "is_active": getattr(d, "is_active", None),
                       "can_have_chains": getattr(d, "can_have_chains", False),
                       "ref": ctx.lom.ref(d, with_path=False)}
                      for i, d in enumerate(t.devices)]
    if clips:
        slots = []
        for i, cs in enumerate(getattr(t, "clip_slots", [])):
            c = cs.clip if cs.has_clip else None
            if c is None:
                continue
            slots.append({"slot": i, "name": c.name, "length": c.length, "is_midi": c.is_midi_clip,
                          "looping": c.looping, "is_playing": c.is_playing, "color": c.color})
        out["clips"] = slots
        try:
            out["arrangement_clips"] = len(t.arrangement_clips)
        except Exception:
            pass
    for attr in ("current_monitoring_state", "input_routing_type", "output_routing_type"):
        try:
            v = getattr(t, attr)
            out[attr] = getattr(v, "display_name", v)
        except Exception:
            pass
    return out


_TRACK_PROPS = ("name", "mute", "solo", "arm", "color", "color_index", "current_monitoring_state",
                "fold_state", "is_showing_chains", "implicit_arm")


@op("track.create", mutates=True)
def track_create(ctx, kind="midi", index=-1, name=None, color=None):
    s = ctx.song()
    if kind == "midi":
        s.create_midi_track(index)
        t = s.tracks[index if index >= 0 else len(s.tracks) - 1]
    elif kind == "audio":
        s.create_audio_track(index)
        t = s.tracks[index if index >= 0 else len(s.tracks) - 1]
    elif kind == "return":
        s.create_return_track()
        t = s.return_tracks[-1]
    else:
        raise OpError("kind must be midi, audio or return")
    if name is not None:
        t.name = name
    if color is not None:
        t.color = color
    out = ctx.lom.ref(t)
    out["devices"] = [d.name for d in t.devices]  # from the user's default track template
    return out


@op("track.delete", mutates=True)
def track_delete(ctx, track):
    s = ctx.song()
    t = track_of(ctx, track)
    for i, x in enumerate(s.tracks):
        if x == t:
            s.delete_track(i)
            return {"deleted": i}
    for i, x in enumerate(s.return_tracks):
        if x == t:
            s.delete_return_track(i)
            return {"deleted_return": i}
    raise OpError("track not found in set")


@op("track.duplicate", mutates=True)
def track_duplicate(ctx, track):
    s = ctx.song()
    t = track_of(ctx, track)
    for i, x in enumerate(s.tracks):
        if x == t:
            s.duplicate_track(i)
            return ctx.lom.ref(s.tracks[i + 1])
    raise OpError("only regular tracks can be duplicated")


@op("track.set", mutates=True)
def track_set(ctx, track, volume=None, pan=None, sends=None, **props):
    t = track_of(ctx, track)
    out = apply_props(t, props, _TRACK_PROPS)
    md = t.mixer_device
    for key, p, v in (("volume", md.volume, volume), ("pan", md.panning, pan)):
        if v is None:
            continue
        out[key] = set_param(p, display=v) if isinstance(v, str) else set_param(p, value=v)
    for k, v in (sends or {}).items():
        p = md.sends[int(k)]
        out["send_%s" % k] = set_param(p, display=v) if isinstance(v, str) else set_param(p, value=v)
    return out


# ---------------------------------------------------------------- scenes

@op("scene.create", mutates=True)
def scene_create(ctx, index=-1, name=None):
    s = ctx.song()
    s.create_scene(index)
    sc = s.scenes[index if index >= 0 else len(s.scenes) - 1]
    if name is not None:
        sc.name = name
    return ctx.lom.ref(sc)


@op("scene.delete", mutates=True)
def scene_delete(ctx, index):
    ctx.song().delete_scene(index)
    return {"deleted": index}


@op("scene.duplicate", mutates=True)
def scene_duplicate(ctx, index):
    ctx.song().duplicate_scene(index)
    return ctx.lom.ref(ctx.song().scenes[index + 1])


@op("scene.fire", mutates=True)
def scene_fire(ctx, index, force_legato=False):
    sc = ctx.song().scenes[index]
    if force_legato:
        sc.fire(force_legato=True)
    else:
        sc.fire()
    return {"fired": index}


@op("scene.set", mutates=True)
def scene_set(ctx, index, **props):
    return apply_props(ctx.song().scenes[index], props, ("name", "color", "color_index", "tempo",
                                                         "tempo_enabled", "time_signature_numerator",
                                                         "time_signature_denominator",
                                                         "time_signature_enabled"))


# ---------------------------------------------------------------- clips

_CLIP_PROPS = ("name", "color", "color_index", "looping", "loop_start", "loop_end", "start_marker",
               "end_marker", "muted", "launch_mode", "launch_quantization", "legato", "velocity_amount",
               "warping", "warp_mode", "gain", "pitch_coarse", "pitch_fine", "signature_numerator",
               "signature_denominator", "ram_mode", "position",
               # Live 12.4.15+: per-clip scale (scale_intervals is read-only, derived from scale_name)
               "root_note", "scale_name", "scale_mode")


@op("clip.create", mutates=True)
def clip_create(ctx, track, slot, length=4.0, name=None, notes=None, color=None, looping=None):
    cs = clip_slot_of(ctx, track, slot)
    if cs.has_clip:
        raise OpError("slot %s already has a clip; delete it first or pick another slot" % slot)
    cs.create_clip(float(length))
    c = velocity_on(cs.clip)
    if name is not None:
        c.name = name
    if color is not None:
        c.color = color
    if looping is not None:
        c.looping = looping
    out = ctx.lom.ref(c)
    if notes:
        out["notes_added"] = add_notes(c, notes)
    return out


@op("clip.delete", mutates=True)
def clip_delete(ctx, track, slot):
    cs = clip_slot_of(ctx, track, slot)
    if cs.has_clip:
        cs.delete_clip()
    return {"deleted": True}


@op("clip.fire", mutates=True)
def clip_fire(ctx, track=None, slot=None, clip=None):
    if clip is None:
        clip_slot_of(ctx, track, slot).fire()
    else:
        ctx.lom.resolve(clip).fire()
    return {"fired": True}


@op("clip.stop", mutates=True)
def clip_stop(ctx, track, slot=None):
    t = track_of(ctx, track)
    if slot is None:
        t.stop_all_clips()
    else:
        t.clip_slots[slot].stop()
    return {"stopped": True}


@op("clip.info")
def clip_info(ctx, clip=None, track=None, slot=None):
    c = clip_of(ctx, clip, track, slot)
    out = {"ref": ctx.lom.ref(c)}
    for a in ("name", "length", "is_midi_clip", "is_audio_clip", "looping", "loop_start", "loop_end",
              "start_marker", "end_marker", "is_playing", "is_recording", "color", "muted",
              "signature_numerator", "signature_denominator", "launch_mode", "launch_quantization",
              "warping", "warp_mode", "gain", "pitch_coarse", "file_path", "start_time", "end_time",
              "is_arrangement_clip", "root_note", "scale_name", "scale_mode", "scale_intervals"):
        try:
            out[a] = ctx.lom.ser(getattr(c, a))
        except Exception:
            pass
    return out


def _db_from_display(text):
    text = str(text).strip().lower()
    if "inf" in text:
        return -999.0
    return float(text.replace("db", "").strip())


def set_clip_gain_db(c, db):
    """Set an AUDIO clip's gain in dB. The raw ``gain`` property is NOT unity-based: 0.4 = 0 dB, 1.0 = +24 dB
    (writing 1.0 'for unity', as one script did on 2026-11-16, boosted ~140 clips by 24 dB). The raw-to-dB curve is
    not linear below 0 dB, so search the raw value against Live's own ``gain_display_string``."""
    if not getattr(c, "is_audio_clip", True):
        raise OpError("gain_db applies to audio clips (MIDI clips use velocity_amount)")
    target = float(db)
    if not -69.0 <= target <= 24.0:
        raise OpError("gain_db must be between -69 and +24 dB")
    lo, hi = 0.0, 1.0
    for _ in range(26):
        mid = (lo + hi) / 2.0
        c.gain = mid
        if _db_from_display(c.gain_display_string) > target:
            hi = mid
        else:
            lo = mid
    c.gain = lo
    return {"gain": c.gain, "gain_display_string": c.gain_display_string}


@op("clip.set", mutates=True)
def clip_set(ctx, clip=None, track=None, slot=None, **props):
    c = clip_of(ctx, clip, track, slot)
    gain_db = props.pop("gain_db", None)
    # Live rejects start >= end at every intermediate step, so when moving a
    # range forward set the end first, otherwise the start first.
    first = []
    for lo, hi in (("loop_start", "loop_end"), ("start_marker", "end_marker")):
        if lo in props and hi in props:
            first.append(hi if props[lo] >= getattr(c, hi) else lo)
    order = sorted(props.items(), key=lambda kv: 0 if kv[0] in first else 1)
    out = {}
    for k, v in order:
        out.update(apply_props(c, {k: v}, _CLIP_PROPS))
    if gain_db is not None:
        out.update(set_clip_gain_db(c, gain_db))
    return out


@op("clip.duplicate", mutates=True)
def clip_duplicate(ctx, track, slot, to_track=None, to_slot=None):
    src = clip_slot_of(ctx, track, slot)
    if to_slot is None:
        t = track_of(ctx, track)
        idx = t.duplicate_clip_slot(slot)
        return {"slot": idx}
    dst = clip_slot_of(ctx, to_track if to_track is not None else track, to_slot)
    src.duplicate_clip_to(dst)
    return ctx.lom.ref(dst.clip)


# ---------------------------------------------------------------- notes

def _note_spec(n):
    Live = _live()
    kw = {"pitch": int(n["pitch"]), "start_time": float(n.get("start_time", n.get("start", 0.0))),
          "duration": float(n.get("duration", 0.25)), "velocity": float(n.get("velocity", 100)),
          "mute": bool(n.get("mute", False))}
    for opt in ("probability", "velocity_deviation", "release_velocity"):
        if opt in n:
            kw[opt] = float(n[opt])
    return Live.Clip.MidiNoteSpecification(**kw)


def add_notes(clip, notes):
    if not clip.is_midi_clip:
        raise OpError("not a MIDI clip")
    specs = tuple(_note_spec(n) for n in notes)
    ids = clip.add_new_notes(specs)
    try:
        return len(ids)
    except TypeError:
        return len(specs)


def _note_out(n):
    return {"note_id": n.note_id, "pitch": n.pitch, "start_time": n.start_time, "duration": n.duration,
            "velocity": n.velocity, "mute": n.mute, "probability": n.probability,
            "velocity_deviation": n.velocity_deviation, "release_velocity": n.release_velocity}


def _range(clip, from_pitch, pitch_span, from_time, time_span):
    if time_span is None:
        time_span = max(clip.length, getattr(clip, "loop_end", 0.0), getattr(clip, "end_marker", 0.0)) \
            - (from_time or 0.0) + 1e6
    return int(from_pitch), int(pitch_span), float(from_time), float(time_span)


@op("notes.get")
def notes_get(ctx, clip=None, track=None, slot=None, from_pitch=0, pitch_span=128, from_time=0.0,
              time_span=None):
    c = clip_of(ctx, clip, track, slot)
    notes = c.get_notes_extended(*_range(c, from_pitch, pitch_span, from_time, time_span))
    out = [_note_out(n) for n in notes]
    out.sort(key=lambda n: (n["start_time"], n["pitch"]))
    return {"clip": c.name, "length": c.length, "count": len(out), "notes": out}


@op("notes.add", mutates=True)
def notes_add(ctx, notes, clip=None, track=None, slot=None):
    c = clip_of(ctx, clip, track, slot)
    return {"added": add_notes(c, notes)}


@op("notes.replace", mutates=True)
def notes_replace(ctx, notes, clip=None, track=None, slot=None, from_pitch=0, pitch_span=128,
                  from_time=0.0, time_span=None):
    c = clip_of(ctx, clip, track, slot)
    c.remove_notes_extended(*_range(c, from_pitch, pitch_span, from_time, time_span))
    return {"added": add_notes(c, notes) if notes else 0}


@op("notes.write", mutates=True)
def notes_write(ctx, notes, track=None, slot=None, clip=None, length=None, name=None, mode="replace",
                from_pitch=0, pitch_span=128):
    """Create the session clip if the slot is empty (or grow it to ``length``), then
    replace (or add to) its notes - one tick, one call."""
    created = False
    if clip is None:
        cs = clip_slot_of(ctx, track, slot)
        if not cs.has_clip:
            cs.create_clip(float(length or 4.0))
            velocity_on(cs.clip)
            created = True
        c = cs.clip
    else:
        c = ctx.lom.resolve(clip)
    if not c.is_midi_clip:
        raise OpError("not a MIDI clip")
    if length and not created and c.length < length:
        c.loop_end = float(length)
        c.end_marker = float(length)
    if name is not None:
        c.name = name
    if mode == "replace":
        c.remove_notes_extended(int(from_pitch), int(pitch_span), 0.0, max(c.length, length or 0) + 1e6)
    out = ctx.lom.ref(c)
    out.update({"created": created, "added": add_notes(c, notes) if notes else 0, "length": c.length})
    return out


@op("notes.remove", mutates=True)
def notes_remove(ctx, clip=None, track=None, slot=None, note_ids=None, from_pitch=0, pitch_span=128,
                 from_time=0.0, time_span=None):
    c = clip_of(ctx, clip, track, slot)
    if note_ids:
        c.remove_notes_by_id(list(note_ids))
        return {"removed": len(note_ids)}
    c.remove_notes_extended(*_range(c, from_pitch, pitch_span, from_time, time_span))
    return {"removed": "range"}


@op("notes.modify", mutates=True)
def notes_modify(ctx, changes, clip=None, track=None, slot=None):
    """changes: [{note_id, pitch?, start_time?, duration?, velocity?, mute?, probability?, ...}]"""
    c = clip_of(ctx, clip, track, slot)
    by_id = dict((int(ch["note_id"]), ch) for ch in changes)
    notes = c.get_notes_by_id(list(by_id))
    for n in notes:
        ch = by_id[n.note_id]
        for k, v in ch.items():
            if k != "note_id":
                setattr(n, k, v)
    c.apply_note_modifications(notes)
    return {"modified": len(notes)}


# ---------------------------------------------------------------- arrangement

@op("arrangement.clips")
def arrangement_clips(ctx, track):
    t = track_of(ctx, track)
    return [{"name": c.name, "start_time": c.start_time, "end_time": c.end_time, "length": c.length,
             "is_midi": c.is_midi_clip, "ref": ctx.lom.ref(c, with_path=False)}
            for c in t.arrangement_clips]


def velocity_on(c):
    """MIDI clips created through Live's API start with velocity_amount 0.0, so note velocities are ignored
    (measured in Live 12.4.16: the same drum notes played 2.9 dB louder than the 1.0 clips made in the UI).
    Match the UI default so generated clips keep their dynamics."""
    try:
        if c is not None and getattr(c, "is_midi_clip", True) and hasattr(c, "velocity_amount"):
            c.velocity_amount = 1.0
    except Exception:
        pass
    return c


@op("arrangement.create_midi_clip", mutates=True)
def arrangement_create_midi_clip(ctx, track, start_time, length, name=None, notes=None):
    t = track_of(ctx, track)
    if not hasattr(t, "create_midi_clip"):
        raise OpError("Track.create_midi_clip needs Live 12.1 or newer")
    c = velocity_on(t.create_midi_clip(float(start_time), float(length)))
    if name is not None:
        c.name = name
    out = ctx.lom.ref(c)
    if notes:
        out["notes_added"] = add_notes(c, notes)
    return out


@op("arrangement.create_audio_clip", mutates=True)
def arrangement_create_audio_clip(ctx, track, file_path, start_time):
    t = track_of(ctx, track)
    if not hasattr(t, "create_audio_clip"):
        raise OpError("Track.create_audio_clip needs Live 12.2 or newer")
    return ctx.lom.ref(t.create_audio_clip(file_path, float(start_time)))


@op("arrangement.duplicate_clip", mutates=True)
def arrangement_duplicate_clip(ctx, track, slot, start_time):
    t = track_of(ctx, track)
    c = t.clip_slots[slot].clip
    if c is None:
        raise OpError("no clip in slot %s" % slot)
    return ctx.lom.ref(t.duplicate_clip_to_arrangement(c, float(start_time)))


# ---------------------------------------------------------------- devices

@op("device.params")
def device_params(ctx, device=None, track=None, index=None, filter=None):
    d = device_of(ctx, device, track, index)
    f = (filter or "").lower()
    params = [param_info(p, i) for i, p in enumerate(d.parameters) if not f or f in p.name.lower()]
    return {"device": d.name, "class_name": getattr(d, "class_name", None), "ref": ctx.lom.ref(d),
            "parameters": params}


def _norm(name):
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def match_params(plist, name):
    """Parameters matching `name`: exact, case-insensitive, then punctuation/space-insensitive (Live names the same
    knob 'Dry/Wet' on Reverb, 'Dry Wet' on Echo and 'DryWet' on Grain Delay), then substring forms."""
    name = str(name)
    low, norm = name.lower(), _norm(name)
    for test in (lambda p: p.name == name, lambda p: p.name.lower() == low, lambda p: _norm(p.name) == norm,
                 lambda p: low in p.name.lower(), lambda p: bool(norm) and norm in _norm(p.name)):
        found = [p for p in plist if test(p)]
        if found:
            return found
    return []


@op("device.set_params", mutates=True)
def device_set_params(ctx, params, device=None, track=None, index=None):
    """params: [{name|index, value|normalized|display}]"""
    d = device_of(ctx, device, track, index)
    plist = list(d.parameters)
    out = []
    for spec in params:
        if "index" in spec:
            p = plist[int(spec["index"])]
        else:
            name = str(spec.get("name", ""))
            matches = match_params(plist, name)
            if not matches:
                raise OpError("no parameter %r on %s; available: %s"
                              % (name, d.name, [p.name for p in plist][:60]))
            p = matches[0]
        out.append(set_param(p, spec.get("value"), spec.get("normalized"), spec.get("display")))
    return out


@op("device.delete", mutates=True)
def device_delete(ctx, track, index):
    t = track_of(ctx, track)
    t.delete_device(int(index))
    return {"deleted": index}


@op("device.chains")
def device_chains(ctx, device=None, track=None, index=None):
    d = device_of(ctx, device, track, index)
    if not getattr(d, "can_have_chains", False):
        return {"device": d.name, "chains": []}
    out = []
    for i, ch in enumerate(d.chains):
        out.append({"index": i, "name": ch.name, "mute": getattr(ch, "mute", None),
                    "devices": [x.name for x in ch.devices], "ref": ctx.lom.ref(ch)})
    res = {"device": d.name, "chains": out}
    if getattr(d, "can_have_drum_pads", False):
        res["drum_pads"] = [{"note": p.note, "name": p.name, "ref": ctx.lom.ref(p, with_path=False)}
                            for p in d.drum_pads if p.chains]
    return res


# ---------------------------------------------------------------- view / selection

@op("view.select", mutates=True)
def view_select(ctx, track=None, scene=None, clip=None, device=None, slot=None):
    s = ctx.song()
    v = s.view
    if track is not None:
        v.selected_track = track_of(ctx, track)
    if scene is not None:
        v.selected_scene = s.scenes[scene]
    if slot is not None and track is not None:
        v.highlighted_clip_slot = track_of(ctx, track).clip_slots[slot]
    if clip is not None:
        v.detail_clip = ctx.lom.resolve(clip)
    if device is not None:
        dev = ctx.lom.resolve(device)
        v.select_device(dev)
    return {"selected_track": v.selected_track.name,
            "selected_scene": list(s.scenes).index(v.selected_scene) if v.selected_scene else None}


@op("view.show", mutates=True)
def view_show(ctx, view, focus=True):
    av = ctx.app().view
    if focus:
        av.focus_view(view)
    else:
        av.show_view(view)
    return {"shown": view}


# ---------------------------------------------------------------- batch

@op("batch", mutates=True)
def batch(ctx, ops, undo_group=True, stop_on_error=True):
    """Run many ops inside a single main-thread tick (one round trip)."""
    s = ctx.song()
    results = []
    grouped = undo_group and hasattr(s, "begin_undo_step")
    if grouped:
        s.begin_undo_step()
    try:
        for sub in ops:
            name = sub.get("op")
            if name == "batch":
                raise OpError("nested batch is not allowed")
            try:
                fn = OPS[name]
            except KeyError:
                res = {"ok": False, "error": {"type": "UnknownOp", "message": "unknown op %r" % name}}
            else:
                try:
                    r = fn(ctx, **(sub.get("args") or {}))
                    if hasattr(r, "send"):
                        r = ctx.run_to_end(r)
                    res = {"ok": True, "result": r}
                except Exception as e:
                    res = {"ok": False, "error": {"type": type(e).__name__, "message": str(e)}}
            results.append(res)
            if not res["ok"] and stop_on_error:
                break
    finally:
        if grouped:
            s.end_undo_step()
    return {"results": results, "completed": sum(1 for r in results if r["ok"]), "total": len(ops)}


# ---------------------------------------------------------------- subscriptions

@op("observe")
def observe(ctx, path, prop, _cid=None):
    return ctx.events.subscribe(_cid, path, prop)


@op("unobserve")
def unobserve(ctx, sub=None, _cid=None):
    return ctx.events.unsubscribe(_cid, sub)


# browser ops are registered in browser.py
__all__ = ["OPS", "OpError", "LomError", "is_lom"]
