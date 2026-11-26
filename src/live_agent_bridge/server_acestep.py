"""ACE-Step tools: generate a new audio part that fits a reference (lego task) and drop it into
Live in the background. Standard library only (see acestep.py)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from . import acestep
from .client import LiveError
from .server import RW, call, client, mcp
from .taste.locking import file_lock


def _err(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except acestep.AceStepError as e:
        raise ToolError(str(e)) from None


async def _song_defaults() -> dict:
    """Tempo / signature / scale from the open Live set, for conditioning."""
    try:
        s = await client().request("lom.get", {"path": "live_set", "props": [
            "tempo", "signature_numerator", "root_note", "scale_name", "scale_mode"]})
    except LiveError:
        return {}
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    out = {"bpm": round(float(s["tempo"]), 3), "time_signature": str(s["signature_numerator"])}
    scale = str(s.get("scale_name") or "")
    if s.get("scale_mode") and scale:
        mode = "minor" if scale.lower().startswith("minor") or scale.lower() == "aeolian" else \
            "major" if scale.lower() in ("major", "ionian") else scale.lower()
        out["key_scale"] = f"{names[int(s['root_note']) % 12]} {mode}"
    return out


def _path_identity(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _same_file(a: str | None, b: str) -> bool:
    if not a:
        return False
    if _path_identity(a) == _path_identity(b):
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


async def _set_identity() -> tuple[str, str]:
    song = await call("lom.get", path="live_set", props=["file_path", "view"])
    path = song.get("file_path")
    view = song.get("view")
    ref = view.get("$ref", "") if isinstance(view, dict) else ""
    if not ref and not (isinstance(path, str) and path):
        raise ToolError("Could not identify the open Live Set for the ACE-Step import.")
    return (_path_identity(path) if isinstance(path, str) and path else "", ref)


async def _check_set(identity: tuple[str, str]) -> None:
    if await _set_identity() != identity:
        raise ToolError("The Live Set changed during the ACE-Step import. Check the open Set before continuing.")


@asynccontextmanager
async def _import_lock():
    lock = file_lock(acestep.OUTPUT_DIR / "receipts" / ".live-import.lock")
    acquire = asyncio.create_task(asyncio.to_thread(lock.__enter__))
    try:
        await asyncio.shield(acquire)
    except asyncio.CancelledError:
        # A cancelled waiter must not leave the file locked after its thread succeeds.
        def release(future):
            if not future.cancelled() and future.exception() is None:
                lock.__exit__(None, None, None)

        acquire.add_done_callback(release)
        raise
    except TimeoutError as exc:
        raise ToolError("Another ACE-Step import is using Live. Retry after it finishes.") from exc
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


async def _verified_write(identity, verify, op: str, **args):
    """Check the observed result even when the mutation did not answer."""
    await _check_set(identity)
    failure = None
    try:
        await call(op, **args)
    except ToolError as exc:
        failure = exc
    await _check_set(identity)
    result = await verify()
    if result is not None:
        return result
    if failure is not None:
        raise failure
    raise ToolError(f"Could not verify {op} in the open Live Set.")


async def _imported_clip(track: dict, path: str, target: str, position: float | int) -> dict | None:
    track_ref = track["ref"]["$ref"]
    if target == "session":
        if any(detail["slot"] == position for detail in track["clips"]):
            clip = await call("clip.info", track=track_ref, slot=position)
            if _same_file(clip.get("file_path"), path):
                return clip["ref"]
        return None
    clips = await call("arrangement.clips", track=track_ref)
    for detail in clips["items"]:
        if detail["is_midi"] or not math.isclose(detail["start_time"], position, rel_tol=0, abs_tol=1e-6):
            continue
        clip = await call("clip.info", clip=detail["ref"]["$ref"])
        if _same_file(clip.get("file_path"), path):
            return clip["ref"]
    return None


async def _find_import(
    name: str, pending_name: str, path: str, target: str, position: float | int,
) -> tuple[dict | None, dict | None]:
    session = await call("overview", fresh=True)
    pending = None
    for index, candidate in enumerate(session["tracks"]):
        if candidate.get("name") not in (name, pending_name) or candidate.get("is_foldable"):
            continue
        track = await call("track.info", track=index, clips=True)
        if track["name"] != candidate["name"] or not track.get("has_audio_input"):
            continue
        if track["name"] == pending_name:
            if pending is not None:
                raise ToolError("More than one interrupted ACE-Step import track exists. Inspect them before retrying.")
            pending = track
        else:
            clip = await _imported_clip(track, path, target, position)
            if clip is not None:
                return {"track": track["name"], "clip": clip, "file": path}, pending
    return None, pending


async def _import_output(identity, pending_name, name, path, target, position):
    existing, track = await _find_import(name, pending_name, path, target, position)
    if existing is not None:
        await _check_set(identity)
        return existing
    if track is None:
        async def find_pending():
            return (await _find_import(name, pending_name, path, target, position))[1]

        track = await _verified_write(identity, find_pending, "track.create", kind="audio", name=pending_name)
    track_ref = track["ref"]["$ref"]

    async def read_clip():
        current = await call("track.info", track=track_ref, clips=True)
        if current["name"] != pending_name:
            raise ToolError("The interrupted ACE-Step track changed. Inspect it before retrying.")
        clip = await _imported_clip(current, path, target, position)
        if clip is None and (current["clips"] or current.get("arrangement_clips")):
            raise ToolError("The interrupted ACE-Step track already contains a different clip.")
        return clip

    clip = await read_clip()
    if clip is None:
        if target == "arrangement":
            clip = await _verified_write(identity, read_clip, "arrangement.create_audio_clip",
                                         track=track_ref, file_path=path, start_time=position)
        else:
            clip = await _verified_write(identity, read_clip, "sample.load",
                                         path=path, target="session", track=track_ref, slot=position)

    async def read_renamed():
        current = await call("track.info", track=track_ref, clips=False)
        return current["name"] if current["name"] == name else None

    await _verified_write(identity, read_renamed, "track.set", track=track_ref, name=name)
    return {"track": name, "clip": clip, "file": path}


async def _import(receipt: dict, target: str, track_name: str | None, start_time: float | None,
                  slot: int | None) -> list:
    paths = [output["use_path"] for output in receipt.get("outputs", [])]
    if target == "none" or not paths:
        return []
    name = track_name or f"ACE {receipt.get('request', {}).get('track_name', 'part')}"
    async with _import_lock():
        identity = await _set_identity()
        if target == "arrangement":
            position = float(start_time or 0.0)
            if not math.isfinite(position) or position < 0:
                raise ToolError("The ACE-Step Arrangement position must be a finite, non-negative beat.")
        else:
            scenes = len((await call("overview", fresh=True))["scenes"])
            position = slot or 0
            if position < 0:
                position += scenes
            if not 0 <= position < scenes:
                raise ToolError(f"ACE-Step Session slot {slot} is not available in the open Set.")
        imported = []
        for index, path in enumerate(paths):
            label = name if len(paths) == 1 else f"{name} {index + 1}"
            key = [identity[0] or identity[1], receipt.get("task_id"), index, _path_identity(path),
                   target, label, position]
            pending = "ACE import pending " + hashlib.sha256(json.dumps(key).encode()).hexdigest()[:24]
            await _check_set(identity)
            imported.append(await _import_output(identity, pending, label, path, target, position))
        return imported


@mcp.tool(annotations=RW)
async def acestep_status() -> dict:
    """Local ACE-Step XL Base service: ready?, model, where references are staged and outputs saved."""
    st = await asyncio.to_thread(acestep.ready)
    st.update({"output_dir": str(acestep.OUTPUT_DIR), "staging_dir": str(acestep.PROCESS_TEMP / "inputs"),
               "enabled_task": "lego (generate one new instrument part that fits a reference mix)"})
    return st


@mcp.tool(annotations=RW)
async def acestep_service(action: Literal["start", "stop"]) -> dict:
    """Start (loads the XL model onto the GPU, can take a while) or stop (frees VRAM) the local
    ACE-Step service using the deployment's own scripts. Only when the user asks."""
    return await asyncio.to_thread(_err, acestep.service, action)


@mcp.tool(annotations=RW)
async def acestep_generate_part(
    reference: Annotated[str, Field(description=(
        "Absolute path of a WAV with the music the new part must fit (e.g. a section exported from Live, "
        "a stem, or a loop). 10-120 s after cropping"))],
    track_name: Annotated[str, Field(description="Instrument of the new part: 'synth', 'bass', 'keys', "
                                                 "'pad', 'guitar', 'strings', 'lead', 'arp', ...")],
    prompt: Annotated[str, Field(description=(
        "Musical brief in English: role, character, rhythm, register, density, genre. Say what NOT to "
        "include (no drums/bass/vocals) so the part stays isolated"))],
    start_s: Annotated[float, Field(ge=0, description="Crop start in the reference, seconds")] = 0.0,
    end_s: Annotated[float | None, Field(description="Crop end in seconds (omit = to the end)")] = None,
    bpm: Annotated[float | None, Field(description="Default: Live's tempo")] = None,
    key_scale: Annotated[str | None, Field(description="e.g. 'A minor'. Default: Live's scale if enabled")] = None,
    steps: Annotated[int, Field(ge=8, le=200)] = 50,
    guidance: Annotated[float, Field(ge=1, le=15)] = 7.0,
    seed: Annotated[int | None, Field(description="Fixed seed for reproducibility; omit = random")] = None,
    variations: Annotated[int, Field(ge=1, le=4, description="Candidates generated in one batch")] = 1,
    import_to: Annotated[Literal["arrangement", "session", "none"], Field(description=(
        "Put each result on a new audio track (background, selection untouched)"))] = "arrangement",
    start_time: Annotated[float | None, Field(description="Arrangement position in beats for the import")] = 0.0,
    slot: int | None = None,
    wait_s: Annotated[float, Field(ge=0, le=600, description="How long to wait for the result")] = 180.0,
    remove_dc: Annotated[bool, Field(description="Also write a DC-offset-corrected copy and import that "
                                                 "(original kept)")] = True,
) -> dict:
    """Generate a NEW audio part with the local ACE-Step XL Base model (lego task) that fits the
    reference music, then import it into Live. Output is audio, not MIDI; adherence to key/BPM is
    conditioning, not a guarantee - listen before keeping. If the wait ends first, the result
    contains a task_id: continue with acestep_result (never resubmit)."""
    ref = Path(reference)
    defaults = await _song_defaults()
    notes = []
    if bpm is not None and defaults.get("bpm") and abs(bpm - defaults["bpm"]) > 0.01:
        notes.append(f"bpm {bpm} given; Live set is at {defaults['bpm']}")
    if key_scale and defaults.get("key_scale") and key_scale.lower() != defaults["key_scale"].lower():
        notes.append(f"key {key_scale!r} given; Live scale is {defaults['key_scale']!r}")
    if bpm is None and defaults.get("bpm") and abs(defaults["bpm"] - round(defaults["bpm"])) > 0.01:
        notes.append(f"Live tempo {defaults['bpm']} is sent rounded to {round(defaults['bpm'])} (service needs an integer)")
    key_source = "user" if key_scale else ("live_scale" if defaults.get("key_scale") else "none")
    bpm = bpm if bpm is not None else defaults.get("bpm")
    key_scale = key_scale or defaults.get("key_scale")
    ts = defaults.get("time_signature", "4")
    staged, info = await asyncio.to_thread(_err, acestep.stage_reference, ref, start_s, end_s)
    meta = {"reference": str(ref), "crop": [start_s, end_s], "reference_info": info,
            "import": {"to": import_to, "start_time": start_time, "slot": slot}}
    receipt = await asyncio.to_thread(_err, acestep.submit_lego, staged, info["seconds"], track_name, prompt,
                                      bpm, key_scale, ts, steps, guidance, seed, variations, meta)
    return await _wait_and_import(receipt["task_id"], wait_s, remove_dc, import_to, None, start_time, slot,
                                  extra={"conditioning": {"bpm": bpm, "key_scale": key_scale,
                                                          "key_source": key_source, "seconds": info["seconds"],
                                                          "seed": seed, "conflicts": notes}})


async def _wait_and_import(task_id, wait_s, remove_dc, import_to, track_name, start_time, slot, extra=None):
    t0 = time.time()
    while True:
        r = await asyncio.to_thread(_err, acestep.poll, task_id, remove_dc)
        if r["status"] in ("succeeded", "failed") or time.time() - t0 >= wait_s:
            break
        await asyncio.sleep(1.0)
    out: dict[str, Any] = {"task_id": task_id, "status": r["status"]}
    if extra:
        out.update(extra)
    if r["status"] == "failed":
        out["error"] = r.get("error")
        return out
    if r["status"] != "succeeded":
        out["next"] = "still running - call acestep_result with this task_id (do not resubmit)"
        return out
    out["seconds_since_submit"] = r.get("seconds_since_submit")
    out["outputs"] = [{"file": o["use_path"], "original": o["path"], "analysis": o["analysis"]}
                      for o in r["outputs"]]
    out["imported"] = await _import(r, import_to, track_name, start_time, slot)
    out["listen"] = "Generated audio is a candidate: audition it against the song before keeping it."
    return out


@mcp.tool(annotations=RW)
async def acestep_result(
    task_id: str,
    wait_s: Annotated[float, Field(ge=0, le=600)] = 60.0,
    import_to: Literal["arrangement", "session", "none"] = "arrangement",
    track_name: str | None = None,
    start_time: float | None = 0.0,
    slot: int | None = None,
    remove_dc: bool = True,
) -> dict:
    """Resume an ACE-Step task by id. Repeating an import reuses matching audio in the same Set and position."""
    return await _wait_and_import(task_id, wait_s, remove_dc, import_to, track_name, start_time, slot)


