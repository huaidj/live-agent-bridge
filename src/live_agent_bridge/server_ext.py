"""Extended tools: the rest of the official Live API (registered unless
AGENT_BRIDGE_TOOLSET=core)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from .server import DESTRUCTIVE, RO, RW, ClipPath, DevicePath, DeviceSel, TrackRef, call, mcp

CATALOG_PATH = Path(__file__).parent / "remote_script" / "AgentBridge" / "lom_catalog.json"
ParamPath = Annotated[str | None, Field(description=(
    "DeviceParameter path/ref, e.g. 'track 0 devices 1 parameters 3', 'track 2 mixer_device volume', "
    "or a ref 'id 88' from get_device"))]


# --------------------------------------------------------------------------- clips

@mcp.tool(annotations=DESTRUCTIVE)
async def clip_edit(
    action: Literal["quantize", "quantize_pitch", "crop", "duplicate_loop", "duplicate_region", "duplicate_notes",
                    "select_all", "deselect_all", "select_notes", "selected_notes", "all_notes",
                    "move_playing_pos", "scrub", "stop_scrub", "fire_button", "stop", "set_grid", "show_loop",
                    "show_envelope", "hide_envelope"],
    track: TrackRef | None = None,
    slot: int | None = None,
    clip: ClipPath = None,
    grid: Annotated[str | None, Field(description=(
        "quantize: '1/4', '1/8', '1/8T', '1/8+1/8T', '1/16' (default), '1/16T', '1/16+1/16T', '1/32'; "
        "set_grid: 'none', '8 bars', '4 bars', '2 bars', '1 bar', '1/2', '1/4', '1/8', '1/16', '1/32'"))] = None,
    amount: Annotated[float, Field(ge=0, le=1, description="Quantize strength")] = 1.0,
    pitch: Annotated[int | None, Field(description="quantize_pitch: only this pitch; duplicate_region: "
                                                   "only this pitch (-1/None = all)")] = None,
    region_start: float | None = None,
    region_length: float | None = None,
    destination_time: float | None = None,
    transposition: int = 0,
    note_ids: list[int] | None = None,
    beats: Annotated[float | None, Field(description="move_playing_pos / scrub position in beats")] = None,
    state: Annotated[bool | None, Field(description="fire_button: pressed state")] = None,
    grid_is_triplet: bool | None = None,
    parameter: ParamPath = None,
) -> Any:
    """Clip editing: quantize, crop, duplicate loop/region/notes, note selection, playhead
    move/scrub, launch-button state, clip view grid, show loop or an envelope."""
    return await call("clip.edit", action=action, track=track, slot=slot, clip=clip, grid=grid, amount=amount,
                      pitch=pitch, region_start=region_start, region_length=region_length,
                      destination_time=destination_time, transposition=transposition, note_ids=note_ids,
                      beats=beats, state=state, grid_is_triplet=grid_is_triplet, parameter=parameter)


@mcp.tool(annotations=RW)
async def warp_markers(
    action: Literal["list", "add", "move", "remove", "set"] = "list",
    track: TrackRef | None = None,
    slot: int | None = None,
    clip: ClipPath = None,
    beat_time: float | None = None,
    sample_time: Annotated[float | None, Field(description="Seconds in the sample (add)")] = None,
    distance: Annotated[float | None, Field(description="move: beats to shift the marker by")] = None,
    warp_mode: Annotated[int | None, Field(description="0 Beats, 1 Tones, 2 Texture, 3 Re-Pitch, 4 Complex, "
                                                       "6 Complex Pro")] = None,
    warping: bool | None = None,
) -> dict:
    """Audio clip warping: list/add/move/remove warp markers, set warp mode or toggle warping."""
    return await call("clip.warp", action=action, track=track, slot=slot, clip=clip, beat_time=beat_time,
                      sample_time=sample_time, distance=distance, warp_mode=warp_mode, warping=warping)


@mcp.tool(annotations=DESTRUCTIVE)
async def automation(
    action: Literal["read", "write", "clear", "clear_all", "has", "re_enable"],
    parameter: ParamPath = None,
    track: TrackRef | None = None,
    slot: int | None = None,
    clip: ClipPath = None,
    points: Annotated[list[dict[str, float]] | None, Field(description=(
        "write: breakpoints [{time, value}] in clip beats; each holds until the next"))] = None,
    steps: Annotated[list[dict[str, float]] | None, Field(description=(
        "write: explicit steps [{time, duration, value}]"))] = None,
    times: Annotated[list[float] | None, Field(description="read: sample the envelope at these beats")] = None,
) -> dict:
    """Clip automation envelopes for any device/mixer parameter: read values, write breakpoints,
    clear one or all; re_enable reverts an overridden parameter to its automation. Values use the
    parameter's raw range (see get_device min/max). Live only creates NEW envelopes on session clips.
    For ARRANGEMENT automation on a MIDI track: write the envelope on a session MIDI clip, then arrangement
    duplicate_from_session; Live turns it into the track's arrangement automation over that clip's span
    (device and mixer parameters; check the parameter's automation_state). Audio clips lose the envelope."""
    return await call("automation", action=action, parameter=parameter, track=track, slot=slot, clip=clip,
                      points=points, steps=steps, times=times)


# --------------------------------------------------------------------------- routing

@mcp.tool(annotations=RW)
async def routing(
    track: TrackRef | None = None,
    device: DevicePath = None,
    input_type: Annotated[str | None, Field(description="e.g. 'Ext. In', 'All Ins', a track name, "
                                                        "'Resampling'; for a Compressor: sidechain source")] = None,
    input_channel: Annotated[str | None, Field(description="e.g. '1/2', 'Post FX', 'Pre FX'")] = None,
    output_type: Annotated[str | None, Field(description="e.g. 'Master', 'Sends Only', a track name")] = None,
    output_channel: str | None = None,
    io: Annotated[Literal["audio_inputs", "audio_outputs", "midi_inputs", "midi_outputs"] | None,
                  Field(description="Max device IO to set (with device)")] = None,
    io_index: int = 0,
) -> dict:
    """Read or set routing by display name: track input/output type & channel, Compressor
    sidechain source, or a Max for Live device's audio/MIDI IO. Call with only track/device
    to list the available options."""
    if io:
        if not device:
            raise ToolError("io needs device")
        return await call("routing.set_io", device=device, io=io, index=io_index, type=input_type or output_type,
                          channel=input_channel or output_channel)
    return await call("routing", track=track, device=device, input_type=input_type, input_channel=input_channel,
                      output_type=output_type, output_channel=output_channel)


# --------------------------------------------------------------------------- devices

@mcp.tool(annotations=RW)
async def insert_device(
    name: Annotated[str, Field(description=(
        "Native device name as in Live: 'Reverb', 'Delay', 'EQ Eight', 'Compressor', 'Glue Compressor', "
        "'Auto Filter', 'Saturator', 'Utility', 'Operator', 'Wavetable', 'Drift', 'Meld', 'Simpler', "
        "'Sampler', 'Drum Rack', 'Instrument Rack', 'Audio Effect Rack', 'Arpeggiator', 'Chord', ..."))],
    track: TrackRef | None = None,
    chain: Annotated[str | None, Field(description="Rack chain path to insert into instead of a track")] = None,
    index: Annotated[int, Field(description="Position in the chain; -1 = end")] = -1,
    replace_instrument: Annotated[bool, Field(description=(
        "For instruments: replace the track's existing instrument (a chain holds only one)"))] = False,
) -> dict:
    """Insert a native Live device by name directly (Track/Chain.insert_device) - instant, no
    browser search. Use load_device for presets, plug-ins and Max devices."""
    return await call("device.insert", name=name, track=track, chain=chain, index=index,
                      replace_instrument=replace_instrument)


@mcp.tool(annotations=RW)
async def move_device(
    device: Annotated[str, Field(description="Device path/ref")],
    target: Annotated[int | str, Field(description="Destination track index/name or chain path")],
    position: int = -1,
) -> dict:
    """Move a device to another track or rack chain (Song.move_device)."""
    return await call("device.move", device=device, target=target, position=position)


@mcp.tool(annotations=DESTRUCTIVE)
async def device_control(
    action: Literal["info", "activate", "deactivate", "collapse", "expand", "ab_save", "ab_toggle", "ab_set",
                    "select", "select_instrument", "store_bank", "max_banks", "delete", "duplicate"],
    track: TrackRef | None = None,
    device: DeviceSel = None,
    path: DevicePath = None,
    value: Annotated[bool | None, Field(description="ab_set: true = use preset B")] = None,
    bank: int | None = None,
    script_index: int = 0,
) -> dict:
    """Device-level operations: on/off, duplicate, collapse, A/B compare (save/toggle), latency info,
    Max for Live parameter banks, delete. ('select'/'select_instrument' change the visible UI.)"""
    return await call("device.control", action=action, device=path, track=track, index=device, value=value,
                      bank=bank, script_index=script_index)


@mcp.tool(annotations=RW)
async def device_special(
    action: Literal["info", "set", "call"] = "info",
    track: TrackRef | None = None,
    device: DeviceSel = None,
    path: DevicePath = None,
    values: Annotated[dict[str, Any] | None, Field(description=(
        "set: class-specific properties, e.g. Wavetable {oscillator_1_wavetable_index: 3, unison_mode: 2}, "
        "Drift {voice_mode_index: 1}, Plug-in {selected_preset_index: 5}, Hybrid Reverb {ir_category_index: 2}, "
        "EQ Eight {oversample: true}, Meld {selected_engine: 1}, Roar {routing_mode_index: 2}"))] = None,
    method: Annotated[str | None, Field(description=(
        "call: class-specific method, e.g. Wavetable 'set_modulation_value', 'add_parameter_to_modulation_matrix'"))
    ] = None,
    args: list[Any] | None = None,
) -> dict:
    """Class-specific device API beyond knobs: info lists every special property (with its
    *_list of choices), method and view property for this device type; set/call apply them."""
    return await call("device.special", action=action, device=path, track=track, index=device, values=values,
                      method=method, args=args)


@mcp.tool(annotations=DESTRUCTIVE)
async def rack(
    action: Literal["info", "add_macro", "remove_macro", "randomize_macros", "store_variation", "recall_variation",
                    "recall_last_variation", "delete_variation", "insert_chain", "copy_pad", "show_chains",
                    "hide_chains", "chain_set", "chain_insert_device", "chain_delete_device", "pad_set",
                    "pad_clear", "select_chain", "select_pad"],
    track: TrackRef | None = None,
    device: DeviceSel = None,
    path: DevicePath = None,
    chain: Annotated[int | str | None, Field(description="Chain index or path")] = None,
    pad: Annotated[int | None, Field(description="Drum pad MIDI note (36 = C1)")] = None,
    source: int | None = None,
    destination: int | None = None,
    variation: int | None = None,
    values: Annotated[dict[str, Any] | None, Field(description=(
        "chain_set: name, mute, solo, color, volume, pan, send_N, in_note, out_note, choke_group; "
        "pad_set: mute, solo, name"))] = None,
    device_name: Annotated[str | None, Field(description="chain_insert_device: native device name")] = None,
    position: int = -1,
) -> dict:
    """Instrument/Audio/MIDI/Drum Rack control: macros (add/remove/randomize), variations
    (store/recall/delete), chains (insert, per-chain mixer, devices, zones), drum pads
    (copy, mute/solo, choke groups, clear), selection."""
    return await call("rack", action=action, device=path, track=track, index=device, chain=chain, pad=pad,
                      source=source, destination=destination, variation=variation, values=values,
                      device_name=device_name, position=position)


@mcp.tool(annotations=DESTRUCTIVE)
async def simpler(
    action: Literal["info", "set", "crop", "reverse", "warp_as", "warp_double", "warp_half", "guess_length",
                    "replace_sample", "insert_slice", "move_slice", "remove_slice", "clear_slices", "reset_slices",
                    "select_slice"] = "info",
    track: TrackRef | None = None,
    device: DeviceSel = None,
    path: DevicePath = None,
    values: Annotated[dict[str, Any] | None, Field(description=(
        "set: playback_mode (0 Classic, 1 One-Shot, 2 Slicing), slicing_playback_mode, voices, retrigger, "
        "pad_slicing"))] = None,
    sample_values: Annotated[dict[str, Any] | None, Field(description=(
        "set: warping, warp_mode, gain, start_marker, end_marker, slicing_style, slicing_sensitivity, "
        "slicing_beat_division, slicing_region_count, beats_*/complex_pro_*/texture_*/tones_* warp settings"))
    ] = None,
    beats: float | None = None,
    time: Annotated[float | None, Field(description="Slice position in sample frames")] = None,
    to_time: float | None = None,
    file_path: str | None = None,
    slice_index: int | None = None,
) -> dict:
    """Simpler + its Sample: playback/slicing modes, warp settings, crop/reverse, warp as N beats,
    replace sample, add/move/remove slices."""
    return await call("simpler", action=action, device=path, track=track, index=device, values=values,
                      sample_values=sample_values, beats=beats, time=time, to_time=to_time, file_path=file_path,
                      slice_index=slice_index)


@mcp.tool(annotations=RW)
async def looper(
    action: Literal["info", "record", "overdub", "play", "stop", "clear", "undo", "double_speed", "half_speed",
                    "double_length", "half_length", "export", "set"],
    track: TrackRef | None = None,
    device: DeviceSel = None,
    path: DevicePath = None,
    clip_slot: Annotated[str | None, Field(description="export: clip slot path, e.g. 'track 1 clip_slots 0'")] = None,
    values: dict[str, Any] | None = None,
) -> dict:
    """Drive a Looper device: record/overdub/play/stop/clear/undo, speed & length, export to a clip slot."""
    return await call("looper", action=action, device=path, track=track, index=device, clip_slot=clip_slot,
                      values=values)


# --------------------------------------------------------------------------- recording / song

@mcp.tool(annotations=RW)
async def record(
    action: Literal["clip_slot", "session", "capture_midi", "capture_scene", "arrangement", "overdub",
                    "punch_in", "punch_out", "automation_arm", "count_in"],
    track: TrackRef | None = None,
    slot: int | None = None,
    length: Annotated[float | None, Field(description="Fixed record length in beats")] = None,
    launch_quantization: int | None = None,
    destination: Annotated[int, Field(description="capture_midi: 0 auto, 1 session, 2 arrangement")] = 0,
    value: Annotated[bool | None, Field(description="Explicit on/off for toggles (omit = toggle)")] = None,
) -> dict:
    """Recording: into a session clip slot (arms the track, optional fixed length), session
    record, Capture MIDI, capture & insert scene, arrangement record / overdub / punch / automation arm."""
    return await call("record", action=action, track=track, slot=slot, length=length,
                      launch_quantization=launch_quantization, destination=destination, value=value)


@mcp.tool(annotations=RW)
async def song_action(
    action: Literal["play_selection", "scrub_by", "jump_by", "nudge_up", "nudge_down", "back_to_arranger",
                    "force_link_beat_time", "stop_all_clips", "smpte", "beats_time", "loop_beats",
                    "re_enable_automation"],
    beats: float | None = None,
    format: Annotated[int, Field(description="smpte: 0=24fps,1=25,2=29.97,3=30,4=29.97 drop,5=30 drop")] = 0,
    quantized: bool | None = None,
) -> dict:
    """Song-level actions & time queries: play selection, scrub/jump, nudge, back to arrangement,
    Link beat sync, SMPTE / bars-beats time, loop in bars-beats."""
    return await call("song.action", action=action, beats=beats, format=format, quantized=quantized)


@mcp.tool(annotations=RW)
async def cue_points(
    action: Literal["list", "toggle", "jump", "rename", "next", "prev"] = "list",
    index: int | None = None,
    name: str | None = None,
    time: Annotated[float | None, Field(description="toggle: place/remove at this beat (default playhead)")] = None,
) -> Any:
    """Arrangement locators: list, add/remove (toggle), jump, rename, next/previous."""
    import asyncio

    for attempt in range(10):
        try:
            return await call("cue", action=action, index=index, name=name, time=time)
        except ToolError as error:
            # Live applies the playhead move on a later tick; the op refuses until it has.
            if "playhead has not reached" not in str(error) or attempt == 9:
                raise
            await asyncio.sleep(0.1)


@mcp.tool(annotations=RW)
async def take_lanes(
    action: Literal["list", "create", "rename", "create_midi_clip", "create_audio_clip"] = "list",
    track: TrackRef = 0,
    lane: int | None = None,
    name: str | None = None,
    start_time: float | None = None,
    length: float | None = None,
    file_path: str | None = None,
) -> Any:
    """Arrangement take lanes (comping): list, create, rename, create MIDI/audio clips in a lane."""
    return await call("take_lanes", action=action, track=track, lane=lane, name=name, start_time=start_time,
                      length=length, file_path=file_path)


@mcp.tool(annotations=RW)
async def groove_pool(
    action: Literal["list", "set", "assign", "global"] = "list",
    groove: Annotated[int | None, Field(description="Groove index (assign: omit to remove the clip's groove)")] = None,
    values: Annotated[dict[str, Any] | None, Field(description=(
        "set: base, quantization_amount, random_amount, timing_amount, velocity_amount, name; "
        "global: groove_amount, swing_amount"))] = None,
    track: TrackRef | None = None,
    slot: int | None = None,
    clip: ClipPath = None,
) -> dict:
    """Groove Pool: list grooves, edit groove parameters, assign a groove to a clip, global amount."""
    return await call("groove", action=action, groove=groove, values=values, track=track, slot=slot, clip=clip)


@mcp.tool(annotations=RW)
async def tuning(values: Annotated[dict[str, Any] | None, Field(description=(
        "name, lowest_note, highest_note, reference_pitch, note_tunings; omit to read"))] = None) -> dict:
    """Read or edit the Live Set's tuning system (microtonal tunings)."""
    return await call("tuning", values=values)


@mcp.tool(annotations=RW)
async def track_extra(
    action: Literal["meters", "group", "fold", "unfold", "delete_arrangement_clip", "jump_in_clip",
                    "duplicate_slot", "view", "mixer"],
    track: TrackRef,
    clip: ClipPath = None,
    beats: float | None = None,
    index: int | None = None,
    values: Annotated[dict[str, Any] | None, Field(description=(
        "mixer: crossfade_assign (0 A,1 none,2 B), panning_mode (0 stereo,1 split), left_split_stereo, "
        "right_split_stereo, track_activator, cue_volume/crossfader/song_tempo (master); "
        "view: device_insert_mode, is_collapsed"))] = None,
) -> dict:
    """Track extras: live meters & CPU impact & freeze state, group/fold, delete an arrangement
    clip, jump inside the running clip, duplicate a clip slot, crossfader/split-pan/cue mixer,
    track view state."""
    return await call("track.extra", action=action, track=track, clip=clip, beats=beats, index=index,
                      values=values)


@mcp.tool(annotations=RW)
async def launch_scene(index: int, force_legato: bool = False, as_selected: bool = False,
                       select: bool = True) -> dict:
    """Fire a scene with full options (legato, fire as selected, keep selection)."""
    return await call("scene.launch", index=index, force_legato=force_legato, as_selected=as_selected,
                      select=select)


# --------------------------------------------------------------------------- application

@mcp.tool(annotations=RW)
async def app_control(
    action: Literal["info", "press_dialog_button", "show_view", "hide_view", "focus_view", "is_view_visible",
                    "scroll_view", "zoom_view", "toggle_browse", "message"] = "info",
    view: Annotated[str | None, Field(description=(
        "Browser, Arranger, Session, Detail, Detail/Clip, Detail/DeviceChain; for 'message': the status-bar text"))
    ] = None,
    direction: Annotated[int, Field(description="0 up, 1 down, 2 left, 3 right")] = 0,
    modifier: bool = False,
    button: Annotated[int | None, Field(description="Dialog button index")] = None,
) -> dict:
    """Live application: version/edition, CPU load, open dialog message & buttons (press one),
    status-bar message. View actions (show/hide/focus/scroll/zoom/toggle_browse) change the visible
    UI - only use them when the user asks."""
    return await call("app", action=action, view=view, direction=direction, modifier=modifier, button=button)


@mcp.tool(annotations=RW)
async def view_state(values: Annotated[dict[str, Any], Field(description=(
        "draw_mode, follow_song, selected_chain, mod_mapping_device, highlighted_clip_slot, detail_clip, "
        "selected_track, selected_scene (objects as refs 'id N')"))]) -> dict:
    """Set Song.View state (draw mode, follow, selected chain/clip/slot, modulation mapping)."""
    return await call("view.set", values=values)


@mcp.tool(annotations=RW)
async def set_memory(key: str, value: Any = None, delete: bool = False,
                     track: Annotated[TrackRef | None, Field(description="Store on this track instead")] = None
                     ) -> dict:
    """Store JSON inside the Live Set (persists when the set is saved): plans, track roles,
    mix notes, anything the agent should remember about this song or a track."""
    return await call("set_data", key=key, value=value, delete=delete, track=track)


@mcp.tool(annotations=RO)
async def get_memory(key: str, track: TrackRef | None = None) -> dict:
    """Read JSON previously stored in this Live Set (or on a track) with set_memory."""
    return await call("get_data", key=key, track=track)


# --------------------------------------------------------------------------- catalog / audit

_catalog: dict | None = None


def _cat() -> dict:
    global _catalog
    if _catalog is None:
        _catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return _catalog


@mcp.tool(annotations=RO)
async def lom_catalog(
    query: Annotated[str | None, Field(description="Class or member name substring, e.g. 'warp', 'Simpler'")] = None,
    cls: Annotated[str | None, Field(description="Exact class, e.g. 'Clip', 'RackDevice', 'Song.View'")] = None,
) -> dict:
    """Official Live Object Model reference (45 classes / 588 members, Live 12.4): member kind,
    access (get/set/observe/call), type and argument names. Use with lom_get/lom_set/lom_call."""
    cat = _cat()
    classes = cat["classes"]
    if cls:
        if cls not in classes:
            raise ToolError(f"unknown class {cls!r}; classes: {sorted(classes)}")
        return {cls: classes[cls]}
    if not query:
        return {"source": cat["source"], "documented_live_version": cat["documented_live_version"],
                "classes": {k: {"parent": v["parent"], "members": len(v["members"])} for k, v in classes.items()}}
    q = query.lower()
    hits: dict[str, Any] = {}
    for cname, spec in classes.items():
        if q in cname.lower():
            hits[cname] = spec
            continue
        ms = {m: e for m, e in spec["members"].items() if q in m.lower()}
        if ms:
            hits[cname] = {"parent": spec["parent"], "members": ms}
    return hits


@mcp.tool(annotations=RO)
async def lom_audit(max_objects: int = 4000) -> dict:
    """Introspect the running Live set and compare every reachable object's Python API with the
    official catalog: documented & present, documented but missing in this build, and
    Python-only extras. Proves what this Live version actually exposes."""
    return await call("lom.audit", timeout=120.0, max_objects=max_objects)

