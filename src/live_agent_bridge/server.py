"""MCP server: exposes Ableton Live to AI agents through the AgentBridge Remote Script.

Design notes
* ~30 focused tools instead of hundreds: smaller tool lists keep agents fast
  and accurate; the generic ``lom_*`` tools reach every Live API object,
  property and method that the high-level tools don't cover.
* ``batch`` performs many operations in one Live tick and one undo step.
* All tools are async and share one multiplexed connection, so parallel tool
  calls from the agent are answered in the same Live tick.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import music
from .client import LiveClient, LiveError
from .samples import SampleIndex
from .taste.tools import INSTRUCTIONS as TASTE_INSTRUCTIONS
from .taste.tools import register as _register_taste

INSTRUCTIONS = """\
Controls a running Ableton Live set. Start with `get_session` (instant, cached) to see tracks,
scenes and transport. Tracks are addressed by 0-based index, exact name, 'master', 'return N',
or a ref such as 'id 123' returned by any tool. Session clips are addressed by track + slot
(0-based scene row). Times are in beats (quarter notes). Prefer `batch` when doing several
edits: it runs them in one Live tick and one undo step. For anything not covered by a
dedicated tool, use `lom_describe` then `lom_get` / `lom_set` / `lom_call` on Live Object
Model paths like 'live_set tracks 0 devices 1 parameters 2'. Errors of type LiveBusy mean a
modal dialog is open in Live; ask the user to close it.

Work in the background: never change what the user is looking at unless asked. Address tracks,
clips and devices directly (all tools take explicit targets) instead of selecting them; only call
`select`, `app_control` view actions or `view_state` when the user explicitly wants the UI changed.
Loading devices/samples restores the user's selection automatically.

Fast paths for natural-language requests:
- Beats: write_drum_pattern with step strings ('x...x...'); chords: write_chords ('Am F C G' or
  roman numerals + key); melodies/basslines: write_melody ('A3:1 C4:.5 ...'). Each creates the
  clip if needed, in one call.
- Sounds: sample_search (all local samples/packs, ms latency, filter by bpm/key/tags/loop) ->
  load_sample / load_drum_kit. Instruments/effects/presets: browser_search -> load_device.
- Mixing/sound design: get_device + set_device_params (values as display text like '-6 dB',
  '2.5 kHz'); update_track for volume/pan/sends.
- Third-party plug-ins (VST2/VST3/AU, Max devices): plugin_inventory -> plugin_inspect (all parameters,
  modules, roles, what is controllable) -> plugin_set by name/role, relative moves allowed. Live only
  exposes configured parameters of large plug-ins: when a needed one is unexposed, tell the user
  which to add via the device's Configure button. Use plugin_snapshot before bold changes so the user
  can A/B and recall. Record meanings the user explains with plugin_learn(origin='user').
"""

mcp = FastMCP("ableton-live", instructions=INSTRUCTIONS + "\n" + TASTE_INSTRUCTIONS)

_client: LiveClient | None = None


def client() -> LiveClient:
    global _client
    if _client is None:
        _client = LiveClient()
    return _client


async def call(op: str, timeout: float = 20.0, **args: Any) -> Any:
    clean = {k: v for k, v in args.items() if v is not None}
    try:
        res = await client().request(op, clean, timeout=timeout)
    except LiveError as e:
        raise ToolError(str(e)) from None
    # A list result would be split into one content block per item; keep it one JSON object.
    return {"items": res, "count": len(res)} if isinstance(res, list) else res


RO = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
RW = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

TrackRef = Annotated[
    int | str,
    Field(description="Track: 0-based index, exact name, 'master', 'return N', or a ref like 'id 123'"),
]
Slot = Annotated[int, Field(ge=0, description="Clip slot / scene row, 0-based")]
ClipPath = Annotated[str | None, Field(description="Clip ref/path (e.g. 'id 123'); alternative to track+slot")]


class Note(BaseModel):
    pitch: int = Field(ge=0, le=127, description="MIDI pitch, 60 = C3")
    start_time: float = Field(ge=0, description="Start in beats from clip start")
    duration: float = Field(0.25, gt=0, description="Length in beats")
    velocity: float = Field(100, ge=0, le=127)
    mute: bool = False
    probability: float | None = Field(None, ge=0, le=1)
    velocity_deviation: float | None = Field(None, ge=-127, le=127)
    release_velocity: float | None = Field(None, ge=0, le=127)


def _notes(notes: list[Note] | None) -> list[dict] | None:
    return None if notes is None else [n.model_dump(exclude_none=True) for n in notes]


# --------------------------------------------------------------------------- session

@mcp.tool(annotations=RO)
async def live_status() -> dict:
    """Bridge health: Live version, tick rate, queue depth, round-trip latency, and
    slowest_ops (per-op Live-side mean/max ms since the bridge started) to see what is slow
    in this set - e.g. device insert/delete cost is Live's own and grows with set size."""
    res = await call("status")
    res["client_rtt_ms"] = round(client().last_rtt_ms or 0, 2)
    return res


@mcp.tool(annotations=RO)
async def get_session(fresh: bool = False) -> dict:
    """Overview of the whole set: transport, tempo, all tracks (mixer state, devices,
    playing slot), return tracks, master, scenes, selection. Served from a cache that
    Live refreshes several times per second; pass fresh=true to force a re-read."""
    return await call("overview", fresh=fresh or None)


@mcp.tool(annotations=RW)
async def transport(
    action: Literal["play", "stop", "continue", "stop_all_clips", "tap_tempo", "undo", "redo",
                    "record", "session_record", "metronome", "loop", "jump", "seek", "capture_midi",
                    "re_enable_automation", "jump_to_next_cue", "jump_to_prev_cue"],
    value: Annotated[float | None, Field(description="Beats for jump (relative) / seek (absolute)")] = None,
) -> dict:
    """Transport control. record/session_record/metronome/loop toggle their state.
    Global Song undo/redo is refused to preserve Live's undo history."""
    return await call("transport", action=action, value=value)


@mcp.tool(annotations=RW)
async def set_song(
    tempo: Annotated[float | None, Field(ge=20, le=999)] = None,
    signature_numerator: int | None = None,
    signature_denominator: int | None = None,
    loop: bool | None = None,
    loop_start: float | None = None,
    loop_length: float | None = None,
    metronome: bool | None = None,
    swing_amount: Annotated[float | None, Field(ge=0, le=1)] = None,
    groove_amount: Annotated[float | None, Field(ge=0, le=1.31)] = None,
    root_note: Annotated[int | None, Field(ge=0, le=11, description="Scale root, 0 = C")] = None,
    scale_name: str | None = None,
    scale_mode: bool | None = None,
    clip_trigger_quantization: Annotated[int | None, Field(description="0=None, 4=1 Bar, ...")] = None,
    extra: Annotated[dict[str, Any] | None, Field(description="Any other writable Song property")] = None,
) -> dict:
    """Set song-level properties (tempo, time signature, loop, scale, groove...)."""
    props = {k: v for k, v in dict(
        tempo=tempo, signature_numerator=signature_numerator, signature_denominator=signature_denominator,
        loop=loop, loop_start=loop_start, loop_length=loop_length, metronome=metronome,
        swing_amount=swing_amount, groove_amount=groove_amount, root_note=root_note, scale_name=scale_name,
        scale_mode=scale_mode, clip_trigger_quantization=clip_trigger_quantization).items() if v is not None}
    props.update(extra or {})
    if not props:
        raise ToolError("nothing to set")
    return await call("song.set", **props)


# --------------------------------------------------------------------------- tracks

@mcp.tool(annotations=RO)
async def get_track(track: TrackRef, include_clips: bool = True) -> dict:
    """Track details: mixer (volume/pan/sends with display values), device chain,
    session clips, routing and monitoring."""
    return await call("track.info", track=track, clips=include_clips)


@mcp.tool(annotations=RW)
async def create_track(
    kind: Literal["midi", "audio", "return"] = "midi",
    index: Annotated[int, Field(description="Insert position; -1 = end")] = -1,
    name: str | None = None,
    color: Annotated[int | None, Field(description="RGB as 0xRRGGBB integer")] = None,
) -> dict:
    """Create a MIDI, audio or return track. The result lists devices that came from the user's
    default track template (e.g. a default instrument)."""
    return await call("track.create", kind=kind, index=index, name=name, color=color)


def warmup() -> None:
    """Start indexing samples in the background as soon as the server starts."""
    global _samples
    try:
        if _samples is None:
            _samples = SampleIndex()
        _samples.ensure_scanned()
    except Exception as e:  # sample search must never stop the Live tools from starting
        print(f"live-agent-bridge: sample index unavailable: {e}", file=sys.stderr)


@mcp.tool(annotations=RW)
async def update_track(
    track: TrackRef,
    name: str | None = None,
    mute: bool | None = None,
    solo: bool | None = None,
    arm: bool | None = None,
    color: int | None = None,
    volume: Annotated[float | str | None, Field(
        description="Raw 0..1 (0.85 = 0 dB) or display text like '-6 dB'")] = None,
    pan: Annotated[float | str | None, Field(description="-1..1 or text like '20L'")] = None,
    sends: Annotated[dict[int, float | str] | None, Field(
        description="Send index -> raw 0..1 or text like '-12 dB'")] = None,
    monitoring: Annotated[int | None, Field(description="0=In, 1=Auto, 2=Off")] = None,
) -> dict:
    """Change track name/mute/solo/arm/color, mixer volume/pan/sends and monitoring."""
    return await call("track.set", track=track, name=name, mute=mute, solo=solo, arm=arm, color=color,
                      volume=volume, pan=pan, sends=sends, current_monitoring_state=monitoring)


@mcp.tool(annotations=DESTRUCTIVE)
async def track_action(track: TrackRef, action: Literal["delete", "duplicate", "stop_clips"]) -> dict:
    """Delete or duplicate a track, or stop all its clips."""
    if action == "delete":
        return await call("track.delete", track=track)
    if action == "duplicate":
        return await call("track.duplicate", track=track)
    return await call("clip.stop", track=track)


# --------------------------------------------------------------------------- scenes

@mcp.tool(annotations=DESTRUCTIVE)
async def scene(
    action: Literal["create", "delete", "duplicate", "fire", "set"],
    index: Annotated[int, Field(description="Scene index; for create, insert position (-1 = end)")] = -1,
    name: str | None = None,
    tempo: float | None = None,
    color: int | None = None,
) -> dict:
    """Create / delete / duplicate / fire a scene, or set its name, tempo or color."""
    if action == "create":
        return await call("scene.create", index=index, name=name)
    if action == "delete":
        return await call("scene.delete", index=index)
    if action == "duplicate":
        return await call("scene.duplicate", index=index)
    if action == "fire":
        return await call("scene.fire", index=index)
    props = {k: v for k, v in dict(name=name, tempo=tempo, color=color).items() if v is not None}
    if tempo is not None:
        props["tempo_enabled"] = True
    return await call("scene.set", index=index, **props)


# --------------------------------------------------------------------------- clips

@mcp.tool(annotations=RW)
async def create_clip(
    track: TrackRef,
    slot: Slot,
    length: Annotated[float, Field(gt=0, description="Length in beats (4 = one 4/4 bar)")] = 4.0,
    name: str | None = None,
    notes: list[Note] | None = None,
    color: int | None = None,
) -> dict:
    """Create a session MIDI clip, optionally filled with notes, in one step."""
    return await call("clip.create", track=track, slot=slot, length=length, name=name,
                      notes=_notes(notes), color=color)


@mcp.tool(annotations=RO)
async def get_clip(track: TrackRef | None = None, slot: int | None = None, clip: ClipPath = None) -> dict:
    """Clip properties: length, loop, markers, warp, launch settings."""
    return await call("clip.info", track=track, slot=slot, clip=clip)


@mcp.tool(annotations=RW)
async def update_clip(
    track: TrackRef | None = None,
    slot: int | None = None,
    clip: ClipPath = None,
    props: Annotated[dict[str, Any] | None, Field(description=(
        "Any of: name, color, looping, loop_start, loop_end, start_marker, end_marker, muted, "
        "launch_mode, launch_quantization, legato, velocity_amount, warping, warp_mode, gain_db, gain, "
        "pitch_coarse, pitch_fine, signature_numerator, signature_denominator, ram_mode, "
        "root_note (0-11, C=0), scale_name (e.g. 'Minor', 'Dorian'), scale_mode (bool; Live 12.4.15+). "
        "AUDIO clip level: use gain_db (e.g. -6). Raw gain is NOT unity-based: 0.4 = 0 dB, 1.0 = +24 dB, so "
        "'gain': 1.0 boosts the clip by 24 dB"))] = None,
) -> dict:
    """Set clip properties (loop range is applied in a safe order)."""
    return await call("clip.set", track=track, slot=slot, clip=clip, **(props or {}))


@mcp.tool(annotations=DESTRUCTIVE)
async def clip_action(
    track: TrackRef,
    slot: Slot,
    action: Literal["fire", "stop", "delete", "duplicate"],
    to_track: Annotated[TrackRef | None, Field(description="duplicate target track")] = None,
    to_slot: Annotated[int | None, Field(description="duplicate target slot; omit = next free slot")] = None,
) -> dict:
    """Launch, stop, delete or duplicate a session clip."""
    if action == "fire":
        return await call("clip.fire", track=track, slot=slot)
    if action == "stop":
        return await call("clip.stop", track=track, slot=slot)
    if action == "delete":
        return await call("clip.delete", track=track, slot=slot)
    return await call("clip.duplicate", track=track, slot=slot, to_track=to_track, to_slot=to_slot)


# --------------------------------------------------------------------------- notes

@mcp.tool(annotations=RO)
async def get_notes(
    track: TrackRef | None = None,
    slot: int | None = None,
    clip: ClipPath = None,
    from_pitch: int = 0,
    pitch_span: int = 128,
    from_time: float = 0.0,
    time_span: float | None = None,
) -> dict:
    """Read MIDI notes (with note_id, velocity, probability, ...) sorted by time."""
    return await call("notes.get", track=track, slot=slot, clip=clip, from_pitch=from_pitch,
                      pitch_span=pitch_span, from_time=from_time, time_span=time_span)


@mcp.tool(annotations=RW)
async def write_notes(
    notes: list[Note],
    track: TrackRef | None = None,
    slot: int | None = None,
    clip: ClipPath = None,
    mode: Annotated[Literal["add", "replace"], Field(
        description="replace clears the given pitch/time range first (default: whole clip)")] = "add",
    from_pitch: int = 0,
    pitch_span: int = 128,
    from_time: float = 0.0,
    time_span: float | None = None,
) -> dict:
    """Add notes to a MIDI clip (session or arrangement), or replace a range of notes."""
    if mode == "add":
        return await call("notes.add", notes=_notes(notes), track=track, slot=slot, clip=clip)
    return await call("notes.replace", notes=_notes(notes), track=track, slot=slot, clip=clip,
                      from_pitch=from_pitch, pitch_span=pitch_span, from_time=from_time, time_span=time_span)


@mcp.tool(annotations=DESTRUCTIVE)
async def edit_notes(
    track: TrackRef | None = None,
    slot: int | None = None,
    clip: ClipPath = None,
    changes: Annotated[list[dict[str, Any]] | None, Field(description=(
        "Modify existing notes in place: [{note_id, pitch?, start_time?, duration?, velocity?, "
        "mute?, probability?, velocity_deviation?, release_velocity?}]"))] = None,
    remove_ids: Annotated[list[int] | None, Field(description="note_ids to delete")] = None,
) -> dict:
    """Modify notes by note_id (keeps identity, no re-creation) and/or delete notes by id."""
    out: dict[str, Any] = {}
    if changes:
        out["modify"] = await call("notes.modify", changes=changes, track=track, slot=slot, clip=clip)
    if remove_ids:
        out["remove"] = await call("notes.remove", note_ids=remove_ids, track=track, slot=slot, clip=clip)
    if not out:
        raise ToolError("give changes and/or remove_ids")
    return out


# --------------------------------------------------------------------------- arrangement

@mcp.tool(annotations=RW)
async def arrangement(
    action: Literal["list", "create_midi_clip", "create_audio_clip", "duplicate_from_session"],
    track: TrackRef,
    start_time: Annotated[float | None, Field(description="Arrangement position in beats")] = None,
    length: float | None = None,
    slot: Annotated[int | None, Field(description="Session slot for duplicate_from_session")] = None,
    file_path: Annotated[str | None, Field(description="Absolute audio file path for create_audio_clip")] = None,
    name: str | None = None,
    notes: list[Note] | None = None,
) -> Any:
    """Arrangement view: list clips on a track, create MIDI/audio clips at a position,
    or copy a session clip into the timeline."""
    if action == "list":
        return await call("arrangement.clips", track=track)
    if start_time is None:
        raise ToolError("start_time is required")
    if action == "create_midi_clip":
        if length is None:
            raise ToolError("length is required")
        return await call("arrangement.create_midi_clip", track=track, start_time=start_time,
                          length=length, name=name, notes=_notes(notes))
    if action == "create_audio_clip":
        return await call("arrangement.create_audio_clip", track=track, file_path=file_path,
                          start_time=start_time)
    return await call("arrangement.duplicate_clip", track=track, slot=slot, start_time=start_time)


# --------------------------------------------------------------------------- devices

DeviceSel = Annotated[int | str | None, Field(description="Device index on the track, or exact device name")]
DevicePath = Annotated[str | None, Field(description="Device ref/path (e.g. 'id 77' or "
                                                     "'track 0 devices 0 chains 1 devices 0')")]


@mcp.tool(annotations=RO)
async def get_device(
    track: TrackRef | None = None,
    device: DeviceSel = None,
    path: DevicePath = None,
    filter: Annotated[str | None, Field(description="Only parameters whose name contains this")] = None,
    include_chains: bool = False,
) -> dict:
    """Device parameters (value, range, display text, quantized items) and optionally
    rack chains / drum pads."""
    res = await call("device.params", device=path, track=track, index=device, filter=filter)
    if include_chains:
        res["chains"] = await call("device.chains", device=path, track=track, index=device)
    return res


class ParamSet(BaseModel):
    name: str | None = Field(None, description="Parameter name (exact, case-insensitive or substring)")
    index: int | None = Field(None, description="Parameter index (alternative to name)")
    value: float | None = Field(None, description="Raw value within min..max")
    normalized: float | None = Field(None, ge=0, le=1, description="0..1 across the range")
    display: str | None = Field(None, description="Display text, e.g. '-6 dB', '1.2 kHz', '50 %', or an item name")


@mcp.tool(annotations=RW)
async def set_device_params(
    params: list[ParamSet],
    track: TrackRef | None = None,
    device: DeviceSel = None,
    path: DevicePath = None,
) -> dict:
    """Set one or many device parameters in a single tick. Each entry picks the parameter
    by name or index and sets it by raw value, normalized 0..1, or display text."""
    return await call("device.set_params", params=[p.model_dump(exclude_none=True) for p in params],
                      device=path, track=track, index=device)


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_device(track: TrackRef, device: int) -> dict:
    """Remove a device from a track's chain by index."""
    return await call("device.delete", track=track, index=device)


# --------------------------------------------------------------------------- browser

Category = Literal["instruments", "audio_effects", "midi_effects", "drums", "sounds", "plugins",
                   "max_for_live", "user_library", "current_project", "samples", "clips", "packs",
                   "user_folders"]


@mcp.tool(annotations=RO)
async def browser_search(
    query: str,
    categories: list[Category] | None = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 25,
    refresh: bool = False,
) -> dict:
    """Search Live's browser (devices, presets, drum kits, plugins, samples...). The first
    search of a category builds an index in the background without freezing Live; later
    searches are instant. Default categories exclude samples/clips/packs (large)."""
    return await call("browser.search", timeout=120.0, query=query, categories=categories, limit=limit,
                      refresh=refresh or None)


@mcp.tool(annotations=RW)
async def load_device(
    track: TrackRef | None = None,
    uri: Annotated[str | None, Field(description="Exact item uri from browser_search")] = None,
    query: Annotated[str | None, Field(description="Name to search; best loadable match is used")] = None,
    category: Category | None = None,
) -> dict:
    """Load an instrument, effect, preset, drum kit or sample onto a track (default:
    selected track). Devices are appended after the selected device."""
    if not uri and not query:
        raise ToolError("give uri or query")
    return await call("browser.load", timeout=120.0, track=track, uri=uri, query=query, category=category)


# --------------------------------------------------------------------------- view

@mcp.tool(annotations=RW)
async def select(
    track: TrackRef | None = None,
    scene: int | None = None,
    slot: Annotated[int | None, Field(description="Highlight this clip slot on the given track")] = None,
    device: DevicePath = None,
    show_view: Annotated[Literal["Session", "Arranger", "Detail", "Detail/Clip", "Detail/DeviceChain",
                                 "Browser"] | None, Field(description="Also focus this view")] = None,
) -> dict:
    """Change Live's visible selection (track / scene / clip slot / device) and optionally the view.
    Foreground action: only use when the user asks to see something; other tools never need it."""
    res = await call("view.select", track=track, scene=scene, slot=slot, device=device)
    if show_view:
        res["view"] = await call("view.show", view=show_view)
    return res


# --------------------------------------------------------------------------- batch

class BatchOp(BaseModel):
    op: str = Field(description="Bridge op name, e.g. 'track.create', 'clip.create', 'notes.add', "
                                "'device.set_params', 'track.set', 'lom.set', 'lom.call', 'scene.fire'")
    args: dict[str, Any] = Field(default_factory=dict)


@mcp.tool(annotations=DESTRUCTIVE)
async def batch(
    ops: list[BatchOp],
    undo_group: Annotated[bool, Field(description="Make the whole batch a single undo step")] = True,
    stop_on_error: bool = True,
) -> dict:
    """Run many bridge ops in ONE Live tick (fastest way to build or edit a lot at once).
    Op names and args mirror the tools: track.create{kind,index,name}, track.set{track,volume,...},
    clip.create{track,slot,length,notes}, notes.add{track,slot,notes}, notes.replace{...},
    clip.set{track,slot,...props}, scene.create{index,name}, scene.set{index,...},
    device.set_params{track,index,params}, browser.load{track,query}, transport{action},
    song.set{tempo,...}, lom.get{path,props}, lom.set{path,values}, lom.call{path,method,args}.
    Use the `bridge_ops` tool to list all op names."""
    return await call("batch", timeout=60.0, ops=[o.model_dump() for o in ops], undo_group=undo_group,
                      stop_on_error=stop_on_error)


@mcp.tool(annotations=RO)
async def bridge_ops() -> dict:
    """List all op names usable in `batch`."""
    c = client()
    if not c.connected:
        await c.connect()
    ops = (c.hello or {}).get("ops", [])
    return {"ops": ops, "count": len(ops)}


# --------------------------------------------------------------------------- generic Live Object Model

LomPath = Annotated[str, Field(description=(
    "LOM path, e.g. 'live_set', 'live_set tracks 0 devices 1', 'live_set view selected_track', "
    "'track 2 clip_slots 0 clip', 'master', 'return 0', 'live_app', or a ref 'id 123'"))]


@mcp.tool(annotations=RO)
async def lom_describe(path: LomPath) -> dict:
    """Introspect any Live object: its type, properties (with writability), methods
    (with signatures) and observable properties."""
    return await call("lom.describe", path=path)


@mcp.tool(annotations=RO)
async def lom_get(
    path: LomPath,
    props: Annotated[list[str] | None, Field(description="Properties to read; omit for all with values")] = None,
) -> dict:
    """Read properties of any Live object. Objects come back as refs {$ref:'id N', type, name, path}."""
    return await call("lom.get", path=path, props=props)


@mcp.tool(annotations=RO)
async def lom_children(path: LomPath, attr: str, offset: int = 0, limit: int = 100) -> dict:
    """Page through a list property of any Live object (e.g. attr='tracks', 'devices', 'parameters')."""
    return await call("lom.children", path=path, attr=attr, offset=offset, limit=limit)


@mcp.tool(annotations=RW)
async def lom_set(
    path: LomPath,
    values: Annotated[dict[str, Any], Field(description="property -> value; refs as 'id N' are resolved")],
) -> dict:
    """Write properties on any Live object; returns the values read back."""
    return await call("lom.set", path=path, values=values)


@mcp.tool(annotations=DESTRUCTIVE)
async def lom_call(
    path: LomPath,
    method: str,
    args: list[Any] | None = None,
    kwargs: dict[str, Any] | None = None,
) -> Any:
    """Call any method of a Live object (see lom_describe for signatures). Refs 'id N' in
    args are resolved to objects."""
    return await call("lom.call", path=path, method=method, args=args, kwargs=kwargs)


# --------------------------------------------------------------------------- observation

@mcp.tool(annotations=RO)
async def observe(path: LomPath, prop: str) -> dict:
    """Subscribe to changes of an observable property (see lom_describe 'observable').
    Changes are coalesced per Live tick; collect them with `poll_events`."""
    return await call("observe", path=path, prop=prop)


@mcp.tool(annotations=RO)
async def unobserve(sub: Annotated[int | None, Field(description="Subscription id; omit = all")] = None) -> dict:
    """Cancel a subscription (or all)."""
    return await call("unobserve", sub=sub)


@mcp.tool(annotations=RO)
async def poll_events(
    wait: Annotated[float, Field(ge=0, le=30, description="Seconds to wait if nothing is queued")] = 0,
    limit: int = 200,
    sub: int | None = None,
) -> dict:
    """Return queued change events from `observe` subscriptions."""
    c = client()
    if wait:
        await c.wait_events(wait, sub)
    events = c.take_events(limit, sub)
    return {"events": events, "count": len(events)}


# --------------------------------------------------------------------------- sample library

_samples: SampleIndex | None = None


async def samples() -> SampleIndex:
    global _samples
    if _samples is None:
        _samples = SampleIndex()
    ix = _samples
    ix.ensure_scanned()
    if ix.count == 0 and ix.scanning:
        await asyncio.to_thread(ix.wait, 30.0)
    return ix


@mcp.tool(annotations=RO)
async def sample_search(
    query: Annotated[str, Field(description="Words matched against file name, folder and tags, "
                                            "e.g. 'punchy kick', '808', 'vocal chop', 'amen break'")] = "",
    bpm: Annotated[float | None, Field(description="Target tempo; half/double time also match")] = None,
    bpm_tolerance: float = 3.0,
    key: Annotated[str | None, Field(description="e.g. 'Am', 'F#', 'C minor' (relative key also matches)")] = None,
    loop: Annotated[bool | None, Field(description="true = loops only, false = one-shots only")] = None,
    tags: Annotated[list[str] | None, Field(description=(
        "Required tags: kick snare clap rim hihat open_hat cymbal tom perc 808 bass lead pad chord keys "
        "guitar strings brass vocal fx riser impact texture fill break top loop one_shot splice"))] = None,
    folder: Annotated[str | None, Field(description="Substring of the folder/pack path")] = None,
    max_duration: Annotated[float | None, Field(description="Seconds (WAV only)")] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 20,
    offset: int = 0,
    description: Annotated[str | None, Field(max_length=500, description="Semantic sound description; needs local CLAP")] = None,
    similar_to: Annotated[str | None, Field(description="Reference audio path; needs local CLAP")] = None,
    detail: Annotated[bool, Field(description="Full per-file analysis (default: compact summary)")] = False,
) -> dict:
    """Millisecond search over every sample on disk that Live knows about: Places / user
    folders, installed packs, User Library and Core Library (from Live's Library.cfg).
    Results distinguish decoded-audio measurements from low-confidence filename hints.
    Semantic description/similar_to search needs an existing local CLAP model and analyzed embeddings.
    Use the returned `path` with load_sample or load_drum_kit."""
    ix = await samples()
    if description or similar_to:
        if any((query, bpm is not None, key, loop is not None, tags, folder, max_duration is not None, offset)):
            raise ToolError("Semantic search currently accepts only description or similar_to and limit")
        from .audio.library import AudioLibrary
        try:
            return await asyncio.to_thread(AudioLibrary(ix.db_path).semantic_search,
                                           description=description, similar_to=similar_to, limit=limit)
        except (ValueError, OSError, ImportError) as error:
            raise ToolError(str(error)) from None
    return await asyncio.to_thread(ix.search, query, bpm, bpm_tolerance, key, loop, tags, folder,
                                   max_duration, limit, offset, detail)


@mcp.tool(annotations=RW)
async def sample_library(
    action: Literal["status", "rescan", "add_folder", "remove_folder"] = "status",
    folder: str | None = None,
) -> dict:
    """Manage the sample index: show roots/status, rescan (incremental, runs in background),
    or add/remove an extra sample folder."""
    ix = await samples()
    if action in ("add_folder", "remove_folder"):
        if not folder:
            raise ToolError("folder is required")
        try:
            (ix.add_root if action == "add_folder" else ix.remove_root)(folder)
        except ValueError as e:
            raise ToolError(str(e)) from None
        action = "rescan"
    if action == "rescan":
        ix.start_scan()
        await asyncio.to_thread(ix.wait, 20.0)
    return ix.status()


@mcp.tool(annotations=RW)
async def load_sample(
    path: Annotated[str, Field(description="Absolute file path (from sample_search)")],
    target: Annotated[Literal["simpler", "drum_pad", "session", "arrangement"], Field(description=(
        "simpler: new Simpler on a MIDI track | drum_pad: into pad `pad` of the track's Drum Rack | "
        "session: audio clip in clip slot `slot` of an audio track | arrangement: audio clip at start_time"))
    ] = "simpler",
    track: TrackRef | None = None,
    slot: int | None = None,
    start_time: float | None = None,
    pad: Annotated[int | str | None, Field(description="Drum pad note or name, e.g. 36, 'C1', 'snare'")] = None,
    replace_instrument: Annotated[bool, Field(description=(
        "simpler: replace the track's existing instrument (e.g. from the default MIDI template)"))] = False,
) -> dict:
    """Load any audio file into Live as a Simpler instrument, a Drum Rack pad, or an audio clip.
    Works for files anywhere on disk (Splice downloads included) - no browser Places needed."""
    ix = await samples()
    hint = await asyncio.to_thread(ix.hint_for, path)
    pad_note = music.pitch(pad, 1) if pad is not None else None
    return await call("sample.load", timeout=120.0, path=path, target=target, track=track, slot=slot,
                      start_time=start_time, pad_note=pad_note, hint=hint, replace_instrument=replace_instrument)


@mcp.tool(annotations=RO)
async def splice_status() -> dict:
    """Splice-in-Live status from local files: integration installed, signed in, recent premium
    downloads, inferred subscription status (with evidence; not authoritative), and how many
    Splice-downloaded samples are on disk. Downloaded Splice samples are searchable with
    sample_search(tags=['splice']) and loadable with load_sample / load_drum_kit."""
    from . import splice
    return await asyncio.to_thread(splice.status)


@mcp.tool(annotations=RO)
async def preview(
    uri: Annotated[str | None, Field(description="Browser item uri from browser_search")] = None,
    stop: bool = False,
) -> dict:
    """Audition a browser item through Live's preview, or stop previewing."""
    return await call("browser.preview", uri=uri, stop=stop or None)


@mcp.tool(annotations=RW)
async def load_drum_kit(
    track: TrackRef,
    pads: Annotated[dict[str, str], Field(description=(
        "pad -> sample. Pad: note/name ('kick', 'snare', 'hat', 'openhat', 'clap', 'C1', 36). "
        "Sample: absolute path OR a search query like 'punchy kick' (best match is used)"))],
    create_rack: Annotated[bool, Field(description="Insert an empty Drum Rack first if the track has none")] = True,
    replace_instrument: Annotated[bool, Field(description=(
        "If the track already has another instrument (e.g. from the default MIDI track template), "
        "replace it with the Drum Rack"))] = False,
) -> dict:
    """Build a drum kit in one go: resolves each sample (path or search), adds a Drum Rack
    if needed, and loads every sample onto its pad in a single Live tick."""
    ix = await samples()
    chosen: dict[int, str] = {}
    for pad_name, spec in pads.items():
        note = music.pitch(pad_name, 1)
        if os.path.isfile(spec):
            chosen[note] = spec
            continue
        tag = next((t for t in ("kick", "snare", "clap", "rim", "hihat", "open_hat", "cymbal", "tom", "perc")
                    if t.replace("_", "") in pad_name.lower().replace("_", "")), None)
        res = await asyncio.to_thread(ix.search, spec, None, 3.0, None, False, [tag] if tag else None,
                                      None, None, 1, 0)
        if not res["results"]:
            res = await asyncio.to_thread(ix.search, spec, None, 3.0, None, None, None, None, None, 1, 0)
        if not res["results"]:
            raise ToolError(f"no sample matches {spec!r} for pad {pad_name!r}")
        chosen[note] = res["results"][0]["path"]
    ops: list[dict] = []
    info = await call("track.info", track=track, clips=False)
    if not any(d.get("class_name") == "DrumGroupDevice" for d in info.get("devices", [])):
        if not create_rack:
            raise ToolError("track has no Drum Rack; use create_rack=true")
        ops.append({"op": "device.insert", "args": {"track": track, "name": "Drum Rack",
                                                    "replace_instrument": replace_instrument}})
    for note, p in chosen.items():
        hint = await asyncio.to_thread(ix.hint_for, p)
        ops.append({"op": "sample.load", "args": {"path": p, "target": "drum_pad", "track": track,
                                                  "pad_note": note, "hint": hint}})
    res = await call("batch", timeout=180.0, ops=ops, undo_group=True)
    failed = [r for r in res["results"] if not r["ok"]]
    if failed:
        raise ToolError("drum kit incomplete (%d/%d steps done): %s"
                        % (res["completed"], res["total"], failed[0]["error"]["message"]))
    res["pads"] = {str(n): os.path.basename(p) for n, p in chosen.items()}
    return res


# --------------------------------------------------------------------------- musical shorthand

async def _write(notes: list[dict], length: float, track, slot, clip, name, mode) -> dict:
    res = await call("notes.write", notes=notes, track=track, slot=slot, clip=clip, length=length,
                     name=name, mode=mode)
    res["notes"] = len(notes)
    return res


def _music(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except music.MusicError as e:
        raise ToolError(str(e)) from None


@mcp.tool(annotations=RW)
async def write_drum_pattern(
    rows: Annotated[dict[str, str], Field(description=(
        "Row -> steps. Row: kick snare clap rim hat openhat ride crash tom perc shaker, a note "
        "name ('C1') or number. Steps: x hit, X accent, o ghost, 1-9 velocity, . rest; "
        "spaces/| ignored. E.g. {'kick':'x...x...x...x...','hat':'..x...x...x...x.'}"))],
    track: TrackRef | None = None,
    slot: int | None = None,
    clip: ClipPath = None,
    step: Annotated[float, Field(gt=0, description="Beats per step: 0.25 = 16ths, 0.5 = 8ths")] = 0.25,
    swing: Annotated[float, Field(ge=0, le=1)] = 0.0,
    bars_repeat: Annotated[int, Field(ge=1, le=64, description="Repeat the pattern N times")] = 1,
    velocity: int = 100,
    name: str | None = None,
    mode: Literal["replace", "add"] = "replace",
) -> dict:
    """Write a step-sequenced drum pattern into a clip (created if the slot is empty)."""
    notes, length = _music(music.pattern_to_notes, rows, step, velocity, swing)
    notes = [dict(n, start_time=n["start_time"] + k * length) for k in range(bars_repeat) for n in notes]
    return await _write(notes, length * bars_repeat, track, slot, clip, name, mode)


@mcp.tool(annotations=RW)
async def write_chords(
    progression: Annotated[str, Field(description=(
        "Chord symbols 'Am F C G', 'Cmaj7 Dm7 G7', slash chords 'C/E', or roman numerals with key "
        "'i VI III VII', 'ii7 V7 Imaj7'. Per-chord beats with ':' e.g. 'Am:2 F:2 C:4'"))],
    track: TrackRef | None = None,
    slot: int | None = None,
    clip: ClipPath = None,
    key: Annotated[str | None, Field(description="Needed for roman numerals, e.g. 'A minor'")] = None,
    beats_per_chord: float = 4.0,
    octave: Annotated[int, Field(ge=-1, le=7, description="Ableton octave (C3 = middle C)")] = 3,
    style: Literal["block", "stab", "pulse8", "arp_up", "arp_down", "arp_updown"] = "block",
    voicing: Annotated[Literal["smooth", "root"], Field(description="smooth = minimal voice movement")] = "smooth",
    bass: Annotated[bool, Field(description="Add the root (or slash bass) an octave below")] = False,
    velocity: int = 90,
    name: str | None = None,
    mode: Literal["replace", "add"] = "replace",
) -> dict:
    """Turn a chord progression into MIDI in a clip (created if the slot is empty)."""
    notes, length = _music(music.chords_to_notes, progression, key, beats_per_chord, octave, style, voicing,
                           bass, velocity)
    return await _write(notes, length, track, slot, clip, name, mode)


@mcp.tool(annotations=RW)
async def write_melody(
    melody: Annotated[str, Field(description=(
        "Space-separated 'note:beats' tokens, e.g. 'A3:1 C4:.5 E4:.5 r:1 [A2 E3]:2 G4:1@80'. "
        "r = rest, [..] = chord, @n = velocity. Notes use Ableton octaves (C3 = 60)"))],
    track: TrackRef | None = None,
    slot: int | None = None,
    clip: ClipPath = None,
    transpose: int = 0,
    default_beats: float = 0.5,
    velocity: int = 100,
    name: str | None = None,
    mode: Literal["replace", "add"] = "replace",
) -> dict:
    """Write a melody / bassline from compact text into a clip (created if the slot is empty)."""
    notes, length = _music(music.melody_to_notes, melody, default_beats, velocity)
    for n in notes:
        n["pitch"] = max(0, min(127, n["pitch"] + transpose))
    return await _write(notes, length, track, slot, clip, name, mode)


@mcp.tool(annotations=RO)
async def scale(key: Annotated[str, Field(description="e.g. 'A minor', 'D dorian', 'F# minor_pentatonic'")],
                low: str = "C2", high: str = "C5") -> dict:
    """Pitches of a scale in a range (Ableton note names) - helps keep melodies in key."""
    pitches = _music(music.scale_notes, key, low, high)
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    return {"key": key, "pitches": pitches, "names": [f"{names[p % 12]}{p // 12 - 2}" for p in pitches]}


async def _live_audio_source(track, clip) -> dict:
    """Resolve the audio file behind a Live audio clip (explicit clip, or the longest audio clip on a track)."""
    try:
        if clip:
            info = await client().request("clip.info", {"clip": clip})
            candidates = [info]
        else:
            items = await client().request("arrangement.clips", {"track": track})
            candidates = []
            for c in items:
                if c.get("is_midi"):
                    continue
                candidates.append(await client().request("clip.info", {"clip": c["ref"]["$ref"]}))
            if not candidates:
                t = await client().request("track.info", {"track": track, "clips": True})
                for c in t.get("clips", []):
                    if not c.get("is_midi"):
                        candidates.append(await client().request("clip.info", {"track": track, "slot": c["slot"]}))
    except LiveError as e:
        raise ValueError(str(e)) from None
    files = [c for c in candidates if c.get("file_path")]
    if not files:
        raise ValueError("No audio clip with a file was found there")
    best = max(files, key=lambda c: c.get("length") or 0)
    distinct = sorted({c["file_path"] for c in files})
    return {"path": best["file_path"], "label": os.path.splitext(os.path.basename(best["file_path"]))[0],
            "clip": best["ref"]["$ref"], "clip_name": best.get("name"), "other_files_on_track": distinct[:5]}


# Full official API surface (set AGENT_BRIDGE_TOOLSET=core for a minimal tool list)
_register_taste(mcp, samples, _live_audio_source)

if os.environ.get("AGENT_BRIDGE_TOOLSET", "full").lower() != "core":
    from . import (
        server_acestep,  # noqa: E402,F401
        server_ext,  # noqa: E402,F401
        server_m4l,  # noqa: E402,F401
        server_plugins,  # noqa: E402,F401
    )


def main() -> None:
    warmup()
    mcp.run()


if __name__ == "__main__":
    main()
