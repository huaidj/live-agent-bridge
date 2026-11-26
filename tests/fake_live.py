"""Minimal stand-in for Live's Python API, enough to exercise the bridge."""

import itertools
import sys
import types

_ptr = itertools.count(1000)


class Obj(object):
    def __init__(self, parent=None):
        self._live_ptr = next(_ptr)
        self.canonical_parent = parent
        self._listeners = {}

    def __eq__(self, other):
        return isinstance(other, Obj) and other._live_ptr == self._live_ptr

    def __hash__(self):
        return self._live_ptr

    def _fire(self, prop):
        for cb in list(self._listeners.get(prop, [])):
            cb()


def observable(*props):
    def deco(cls):
        for p in props:
            def add(self, cb, p=p):
                self._listeners.setdefault(p, []).append(cb)

            def rem(self, cb, p=p):
                self._listeners[p].remove(cb)

            def has(self, cb, p=p):
                return cb in self._listeners.get(p, [])

            def getter(self, p=p):
                return self.__dict__["_v_" + p]

            def setter(self, v, p=p):
                self.__dict__["_v_" + p] = v
                self._fire(p)
            setattr(cls, "add_%s_listener" % p, add)
            setattr(cls, "remove_%s_listener" % p, rem)
            setattr(cls, "%s_has_listener" % p, has)
            setattr(cls, p, property(getter, setter))
        return cls
    return deco


class MidiNoteSpecification(object):
    def __init__(self, pitch, start_time, duration, velocity=100, mute=False, probability=1.0,
                 velocity_deviation=0.0, release_velocity=64.0):
        self.__dict__.update(locals())
        del self.__dict__["self"]


class MidiNote(object):
    def __init__(self, note_id, spec):
        self.note_id = note_id
        for k in ("pitch", "start_time", "duration", "velocity", "mute", "probability",
                  "velocity_deviation", "release_velocity"):
            setattr(self, k, getattr(spec, k))


class DeviceParameter(Obj):
    def __init__(self, parent, name, lo=0.0, hi=1.0, value=0.0, quantized=False, items=(), fmt=None):
        Obj.__init__(self, parent)
        self.name, self.min, self.max, self.value = name, lo, hi, value
        self.is_quantized, self.value_items, self.is_enabled = quantized, list(items), True
        self._fmt = fmt or (lambda v: "%.2f" % v)

    def str_for_value(self, v):
        return self._fmt(v)

    def __str__(self):
        return self._fmt(self.value)


def _db(v):
    import math
    if v <= 0:
        return "-inf dB"
    return "%.1f dB" % (40 * math.log10(v / 0.85))


class MixerDevice(Obj):
    def __init__(self, parent):
        Obj.__init__(self, parent)
        self.volume = DeviceParameter(self, "Track Volume", 0.0, 1.0, 0.85, fmt=_db)
        self.panning = DeviceParameter(self, "Track Panning", -1.0, 1.0, 0.0)
        self.sends = [DeviceParameter(self, "Send A", 0.0, 1.0, 0.0, fmt=_db)]


class Device(Obj):
    def __init__(self, parent, name):
        Obj.__init__(self, parent)
        self.name = name
        self.class_name = name
        self.type = 1
        self.is_active = True
        self.can_have_chains = False
        self.parameters = [DeviceParameter(self, "Device On", 0, 1, 1, True, ["Off", "On"]),
                           DeviceParameter(self, "Filter Freq", 0.0, 1.0, 0.5,
                                           fmt=lambda v: "%.0f Hz" % (20 * (1000 ** v)))]


class DrumPad(Obj):
    def __init__(self, parent, note):
        Obj.__init__(self, parent)
        self.note, self.name, self.chains = note, "Pad %d" % note, []


class DrumRack(Device):
    def __init__(self, parent):
        Device.__init__(self, parent, "Drum Rack")
        self.class_name = "DrumGroupDevice"
        self.can_have_drum_pads = True
        self.drum_pads = [DrumPad(self, n) for n in range(128)]
        self.view = types.SimpleNamespace(selected_drum_pad=None)


class Clip(Obj):
    def __init__(self, parent, length, is_midi=True, start_time=0.0, path=None):
        Obj.__init__(self, parent)
        self.name = ""
        self.length = length
        self.loop_start, self.loop_end = 0.0, length
        self.start_marker, self.end_marker = 0.0, length
        self.looping = True
        self.is_midi_clip, self.is_audio_clip = is_midi, not is_midi
        self.is_playing = False
        self.color = 0
        self.start_time, self.end_time = start_time, start_time + length
        self.file_path = path
        self.root_note, self.scale_name, self.scale_mode = 0, "Major", False
        self.velocity_amount = 0.0  # Live 12.4: API-created clips start at 0 (velocities ignored)
        self.gain = 0.4  # audio clip gain: 0.4 = 0 dB, 1.0 = +24 dB (Live 12.4.16)
        self._notes = {}
        self._ids = itertools.count(1)
        self.fired = 0

    _SCALES = {"Major": (0, 2, 4, 5, 7, 9, 11), "Minor": (0, 2, 3, 5, 7, 8, 10),
               "Dorian": (0, 2, 3, 5, 7, 9, 10)}

    @property
    def scale_intervals(self):              # read-only in Live, derived from scale_name
        return self._SCALES[self.scale_name]

    def __setattr__(self, k, v):
        if k == "scale_name" and v not in self._SCALES:
            raise RuntimeError("Unknown scale name")
        if k == "loop_start" and "loop_end" in self.__dict__ and v >= self.loop_end:
            raise RuntimeError("LoopStart behind LoopEnd")
        if k == "loop_end" and "loop_start" in self.__dict__ and v <= self.loop_start:
            raise RuntimeError("LoopEnd before LoopStart")
        object.__setattr__(self, k, v)

    def add_new_notes(self, specs):
        ids = []
        for s in specs:
            n = MidiNote(next(self._ids), s)
            self._notes[n.note_id] = n
            ids.append(n.note_id)
        return tuple(ids)

    def get_notes_extended(self, from_pitch, pitch_span, from_time, time_span):
        return tuple(n for n in self._notes.values()
                     if from_pitch <= n.pitch < from_pitch + pitch_span
                     and from_time <= n.start_time < from_time + time_span)

    def remove_notes_extended(self, *rng):
        for n in self.get_notes_extended(*rng):
            del self._notes[n.note_id]

    def get_notes_by_id(self, ids):
        return tuple(self._notes[i] for i in ids if i in self._notes)

    def remove_notes_by_id(self, ids):
        for i in ids:
            self._notes.pop(i, None)

    def apply_note_modifications(self, notes):
        for n in notes:
            self._notes[n.note_id] = n

    def fire(self):
        self.fired += 1



def _clip_gain_display(self):
    g = float(self.gain)
    db = (g - 0.4) * 40.0 if g >= 0.4 else (g - 0.4) * 57.4
    return "-inf dB" if g <= 0.0 else "%.2f dB" % db


Clip.gain_display_string = property(_clip_gain_display)


class ClipSlot(Obj):
    def __init__(self, parent):
        Obj.__init__(self, parent)
        self.clip = None

    @property
    def has_clip(self):
        return self.clip is not None

    def create_clip(self, length):
        self.clip = Clip(self, length)

    def create_audio_clip(self, path):
        self.clip = Clip(self, 8.0, is_midi=False, path=path)

    def delete_clip(self):
        self.clip = None

    def fire(self):
        if self.clip:
            self.clip.fire()

    def stop(self):
        pass


@observable("name", "mute")
class Track(Obj):
    def __init__(self, parent, name, n_slots, midi=True):
        Obj.__init__(self, parent)
        self.__dict__["_v_name"] = name
        self.__dict__["_v_mute"] = False
        self.solo = self.arm = False
        self.color = 0
        self.has_midi_input, self.has_audio_input = midi, not midi
        self.is_foldable = self.is_grouped = False
        self.playing_slot_index = self.fired_slot_index = -1
        self.mixer_device = MixerDevice(self)
        self.devices = []
        self.clip_slots = [ClipSlot(self) for _ in range(n_slots)]
        self.arrangement_clips = []
        self.view = types.SimpleNamespace()

    def stop_all_clips(self):
        pass

    def delete_device(self, i):
        del self.devices[i]

    def create_midi_clip(self, start, length):
        c = Clip(self, length, True, start)
        c.is_arrangement_clip = True
        self.arrangement_clips.append(c)
        return c

    def create_audio_clip(self, path, start):
        c = Clip(self, 4.0, False, start, path)
        self.arrangement_clips.append(c)
        return c


class Scene(Obj):
    def __init__(self, parent, name=""):
        Obj.__init__(self, parent)
        self.name = name
        self.fired = 0

    def fire(self, force_legato=False):
        self.fired += 1


class SongView(Obj):
    def __init__(self, song):
        Obj.__init__(self, song)
        self.selected_track = None
        self.selected_scene = None
        self.detail_clip = None
        self.highlighted_clip_slot = None


@observable("tempo", "is_playing")
class Song(Obj):
    def __init__(self):
        Obj.__init__(self, None)
        self.__dict__["_v_tempo"] = 120.0
        self.__dict__["_v_is_playing"] = False
        self.signature_numerator, self.signature_denominator = 4, 4
        self.current_song_time = 0.0
        self.loop, self.loop_start, self.loop_length = False, 0.0, 16.0
        self.metronome = self.record_mode = self.session_record = False
        self.can_undo = self.can_redo = True
        self.song_length = 64.0
        self.scenes = [Scene(self, "Intro"), Scene(self, "Verse")]
        self.tracks = [Track(self, "Drums", 2), Track(self, "Bass", 2)]
        self.return_tracks = [Track(self, "A-Reverb", 0, midi=False)]
        self.master_track = Track(self, "Master", 0, midi=False)
        self.view = SongView(self)
        self.view.selected_track = self.tracks[0]
        self.view.selected_scene = self.scenes[0]
        self.undo_steps = 0

    def start_playing(self):
        self.is_playing = True

    def stop_playing(self):
        self.is_playing = False

    def create_midi_track(self, index):
        t = Track(self, "MIDI", len(self.scenes))
        if index < 0:
            self.tracks.append(t)
        else:
            self.tracks.insert(index, t)

    def create_audio_track(self, index):
        t = Track(self, "Audio", len(self.scenes), midi=False)
        if index < 0:
            self.tracks.append(t)
        else:
            self.tracks.insert(index, t)

    def delete_track(self, i):
        del self.tracks[i]

    def create_scene(self, index):
        sc = Scene(self)
        if index < 0:
            self.scenes.append(sc)
        else:
            self.scenes.insert(index, sc)
        for t in self.tracks:
            t.clip_slots.append(ClipSlot(t))

    def begin_undo_step(self):
        self.undo_steps += 1

    def end_undo_step(self):
        pass


class BrowserItem(object):
    def __init__(self, name, children=(), loadable=False, folder=False, device=False, uri=None):
        self.name, self.children = name, list(children)
        self.is_loadable, self.is_folder, self.is_device = loadable, folder, device
        self.uri = uri or ("query:" + name)


class Browser(object):
    def __init__(self):
        self.instruments = BrowserItem("Instruments", [
            BrowserItem("Operator", [BrowserItem("Bass Pluck.adv", loadable=True)], loadable=True, device=True),
            BrowserItem("Drift", loadable=True, device=True),
            BrowserItem("Drum Rack", loadable=True, device=True)], folder=True)
        self.audio_effects = BrowserItem("Audio Effects", [BrowserItem("Reverb", loadable=True, device=True)],
                                         folder=True)
        def empty(n):
            return BrowserItem(n, folder=True)
        self.midi_effects, self.drums, self.sounds = empty("MIDI"), empty("Drums"), empty("Sounds")
        def plug(fmt, parts):
            return BrowserItem(parts[-1], loadable=True, device=True,
                               uri="query:Plugins#" + ":".join([fmt] + list(parts)))
        self.plugins = BrowserItem("Plug-ins", [
            BrowserItem("VST3", [
                BrowserItem("Acme Audio", [plug("VST3", ["Acme Audio", "Acme Comp"]),
                                            plug("VST3", ["Acme Audio", "Acme Verb"])], folder=True),
                BrowserItem("VST3", [plug("VST3", ["VST3", "Same Name Synth"])], folder=True),
                BrowserItem("Deep Labs", [
                    BrowserItem("Bundle", [plug("VST3", ["Deep Labs", "Bundle", "Deep Delay"])],
                                folder=True)], folder=True),
                plug("VST3", ["Loose Synth"])], folder=True),
            BrowserItem("AU", [
                BrowserItem("Acme Audio", [plug("AU", ["Acme Audio", "Acme Comp"])], folder=True)],
                folder=True)], folder=True)
        self.max_for_live = empty("M4L")
        self.user_library, self.current_project = empty("User Library"), empty("Current Project")
        self.samples, self.clips, self.packs = empty("Samples"), empty("Clips"), empty("Packs")
        self.user_folders = [BrowserItem("samplepack", [
            BrowserItem("Drums", [BrowserItem("Kick 01.wav", loadable=True)], folder=True)], folder=True)]
        self.loaded = []
        self.song = None

    def load_item(self, item):
        self.loaded.append(item.name)
        t = self.song.view.selected_track
        racks = [d for d in t.devices if getattr(d, "can_have_drum_pads", False)]
        if item.name.endswith(".wav") and racks:
            pad = racks[0].view.selected_drum_pad
            pad.chains.append(item.name)
            return
        t.devices.append(DrumRack(t) if item.name == "Drum Rack" else Device(t, item.name))


class Application(Obj):
    def __init__(self, song):
        Obj.__init__(self)
        self.browser = Browser()
        self.browser.song = song
        self.view = types.SimpleNamespace(focus_view=lambda v: None, show_view=lambda v: None)


def install():
    live = types.ModuleType("Live")
    live.Clip = types.SimpleNamespace(MidiNoteSpecification=MidiNoteSpecification)
    live.Base = types.SimpleNamespace()
    sys.modules["Live"] = live
    song = Song()
    return song, Application(song)


# ---------------------------------------------------------------- extended API doubles

class Envelope(object):
    def __init__(self):
        self.steps = []

    def insert_step(self, time, duration, value):
        self.steps.append((time, duration, value))

    def value_at_time(self, t):
        v = None
        for st, _d, val in sorted(self.steps):
            if st <= t:
                v = val
        return v


def _clip_quantize(self, grid, amount):
    object.__setattr__(self, "quantized", (grid, amount))


def _clip_automation_envelope(self, p):
    return self.__dict__.setdefault("_envs", {}).get(p._live_ptr)


def _clip_create_env(self, p):
    if getattr(self, "is_arrangement_clip", False):  # real Live 12.4 refuses this (verified 2026-11-16)
        raise RuntimeError("Not a session clip or parameter belongs to another track.")
    e = Envelope()
    self.__dict__.setdefault("_envs", {})[p._live_ptr] = e
    return e


Clip.quantize = _clip_quantize
Clip.automation_envelope = _clip_automation_envelope
Clip.create_automation_envelope = _clip_create_env
Clip.duplicate_loop = lambda self: object.__setattr__(self, "length", self.length * 2)
Clip.clear_all_envelopes = lambda self: self.__dict__.pop("_envs", None)


class RoutingType(object):
    def __init__(self, name):
        self.display_name = name


def _track_insert_device(self, name, index):
    self.devices.insert(index, DrumRack(self) if name == "Drum Rack" else Device(self, name))


Track.insert_device = _track_insert_device
Track.available_output_routing_types = (RoutingType("Master"), RoutingType("Sends Only"))
Track.available_output_routing_channels = (RoutingType("Track In"),)
Track.available_input_routing_types = (RoutingType("Ext. In"), RoutingType("No Input"))
Track.available_input_routing_channels = (RoutingType("1/2"), RoutingType("1"))
for _side in ("input", "output"):
    setattr(Track, "%s_routing_type" % _side, None)
    setattr(Track, "%s_routing_channel" % _side, None)

_orig_dr_init = DrumRack.__init__


def _dr_init(self, parent):
    _orig_dr_init(self, parent)
    self.can_have_chains = True
    self.chains = []
    self.variation_count = 0
    self.selected_variation_index = -1
    self.visible_macro_count = 8
    self.parameters = [DeviceParameter(self, "Device On", 0, 1, 1, True, ["Off", "On"])] + \
        [DeviceParameter(self, "Macro %d" % i, 0, 127, 0) for i in range(1, 17)]


DrumRack.__init__ = _dr_init
DrumRack.store_variation = lambda self: setattr(self, "variation_count", self.variation_count + 1)
DrumRack.add_macro = lambda self: setattr(self, "visible_macro_count", self.visible_macro_count + 1)
DrumRack.copy_pad = lambda self, a, b: setattr(self.drum_pads[b], "chains", list(self.drum_pads[a].chains))


class CuePoint(Obj):
    def __init__(self, parent, name, time):
        Obj.__init__(self, parent)
        self.name, self.time = name, time

    def jump(self):
        self.canonical_parent.current_song_time = self.time


def _song_set_or_delete_cue(self):
    cues = self.cue_points
    for c in list(cues):
        if c.time == self.current_song_time:
            cues.remove(c)
            return
    cues.append(CuePoint(self, "", self.current_song_time))


def _song_move_device(self, device, target, pos):
    device.canonical_parent.devices.remove(device)
    target.devices.insert(pos, device)
    device.canonical_parent = target
    return pos


Song.cue_points = property(lambda self: self.__dict__.setdefault("_cues", []))
Song.set_or_delete_cue = _song_set_or_delete_cue
Song.move_device = _song_move_device
Song.set_data = lambda self, k, v: self.__dict__.setdefault("_data", {}).__setitem__(k, v)
Song.get_data = lambda self, k, d: self.__dict__.get("_data", {}).get(k, d)
Application.get_version_string = lambda self: "12.4.16"
Application.average_process_usage = 0.12
Application.current_dialog_button_count = 0
