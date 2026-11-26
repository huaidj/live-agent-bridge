"""Verify real Max LiveAPI writes on an already open, disposable ZZ- Live Set.

Run with the repository venv and pass the exact fixture .als path. This script
neither opens nor saves any Set. Every successful write needs fresh readback
through Max and the Remote Script. Keep the fixture open and idle while it runs.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from live_agent_bridge.client import LiveClient, LiveError  # noqa: E402
from live_agent_bridge.m4l import M4LClient, M4LError  # noqa: E402

SONG = "live_set"
SAFETY = ["name", "file_path", "is_playing", "record_mode", "session_record", "punch_in", "punch_out"]
SONG_STATE = SAFETY + [
    "tempo", "loop", "loop_start", "loop_length", "start_time", "current_song_time",
    "clip_trigger_quantization", "midi_recording_quantization", "groove_amount",
    "signature_numerator", "signature_denominator",
]
UNSUPPORTED = {"unsupported", "unsupported_member", "write_not_supported", "member_unavailable"}
READBACK_DELAYS = (0, 0.03, 0.06, 0.12, 0.18, 0.30)


class UnsafeFixture(RuntimeError):
    """The target or its state no longer permits mutation."""


class BridgeReply(RuntimeError):
    def __init__(self, reply: dict[str, Any]):
        super().__init__(str(reply))
        self.reply = reply


@dataclass(frozen=True)
class Target:
    path: str
    kind: str


def same(a: Any, b: Any) -> bool:
    if isinstance(a, (bool, int, float)) and isinstance(b, (bool, int, float)):
        return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6)
    return a == b


def parameter_rounding(requested: Any, values: tuple, metadata: dict) -> float | None:
    """Return a two-float32-step allowance for a continuous parameter only."""
    if metadata.get("is_quantized") is not False:
        return None
    if any(type(value) is not float or not math.isfinite(value) for value in (requested, *values)):
        return None
    low, high = metadata.get("min"), metadata.get("max")
    if any(type(bound) not in (int, float) or not math.isfinite(bound) for bound in (low, high)):
        return None
    if not all(low <= value <= high for value in (requested, *values)):
        return None
    exponent = max(-149, math.frexp(requested)[1] - 24) if requested else -149
    allowance = 2 * math.ldexp(1.0, exponent)
    return allowance if all(abs(value - requested) <= allowance for value in values) else None


def sha256(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def checked(reply: dict, statuses: tuple[str, ...] = ("ok",)) -> dict:
    if reply.get("status") not in statuses:
        raise BridgeReply(reply)
    return reply


def unexposed(reply: dict) -> bool:
    return not reply.get("write_may_have_occurred") and (
        reply.get("code") in UNSUPPORTED or reply.get("status") == "unsupported"
    )


def native_notes(value: Any) -> list[dict]:
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict) and "notes" in value[0]:
        value = value[0]
    if isinstance(value, dict):
        value = value["notes"]
    if not isinstance(value, list) or any(not isinstance(note, dict) for note in value):
        raise ValueError("LiveAPI returned an unrecognized note dictionary")
    return value


def note_rows(value: Any) -> list[tuple]:
    return sorted(
        (n["pitch"], round(n["start_time"], 6), round(n["duration"], 6), round(n["velocity"], 4))
        for n in native_notes(value)
    )


def first_difference(left: Any, right: Any, path: str = "$") -> str | None:
    if type(left) is not type(right):
        return path
    if isinstance(left, dict):
        if left.keys() != right.keys():
            return path
        for key in left:
            mismatch = first_difference(left[key], right[key], f"{path}.{key}")
            if mismatch:
                return mismatch
        return None
    if isinstance(left, (tuple, list)):
        if len(left) != len(right):
            return f"{path}.length"
        for i, (a, b) in enumerate(zip(left, right, strict=True)):
            mismatch = first_difference(a, b, f"{path}[{i}]")
            if mismatch:
                return mismatch
        return None
    return None if same(left, right) else path


def empty_reference_sentinel(target: Target, member: str, frame: dict) -> bool:
    if frame.get("total") != 1 or frame.get("next_offset") is not None:
        return False
    value = frame.get("value")
    if value == [None]:
        return True
    # Confirmed on Live 12.4.16: an empty Track.arrangement_clips is [""].
    return target.kind == "Track" and member == "arrangement_clips" and value == [""]


def stable_selection(refs: dict) -> dict:
    normalized = {}
    for member, ref in refs.items():
        path = ref.get("path") if isinstance(ref, dict) else None
        if isinstance(path, str) and re.fullmatch(
            r"live_set (?:master_track|(?:tracks|return_tracks|scenes) \d+)", path,
        ):
            normalized[member] = {key: ref[key] for key in ("path", "name", "type") if key in ref}
        else:
            # Keep the opaque identity if no stable canonical form was supplied.
            normalized[member] = ref
    return normalized


class Suite:
    def __init__(self, fixture: Path, sample: Path | None):
        self.fixture = fixture
        self.sample = sample
        self.live = LiveClient(host="127.0.0.1")
        self.max = M4LClient()
        self.prefix = "ZZ-M4LW-" + uuid4().hex[:8]
        self.tracks: set[str] = set()
        self.scenes: set[str] = set()
        self.rows: list[dict] = []
        self.song = Target(SONG, "Song")
        self.anchor: tuple[Target, str] | None = None
        self.report: dict[str, Any] = {"fixture": str(fixture), "run": self.prefix, "rows": self.rows}

    def row(self, label: str, status: str, **detail: Any) -> None:
        self.rows.append({"label": label, "status": status, **detail})
        print(f"{status:8} {label}", flush=True)

    async def rs(self, path: str, *props: str) -> dict:
        return await self.live.request("lom.get", {"path": path, "props": list(props)})

    async def overview(self) -> dict:
        return await self.live.request("overview", {"fresh": True})

    async def guard(self, *, verify_peer: bool = True) -> dict:
        song = await self.rs(SONG, *SAFETY)
        filename = song.get("file_path")
        if (
            not filename
            or not Path(filename).is_absolute()
            or Path(filename).resolve(strict=True) != self.fixture
            or song.get("name") != self.fixture.stem
        ):
            raise UnsafeFixture("Live must have the exact ZZ- fixture Set open")
        if any(song.get(flag) is not False for flag in SAFETY[2:]):
            raise UnsafeFixture("Transport, recording, and both punch switches must be off")
        overview = await self.overview()
        if any(track.get("arm") is True for track in overview["tracks"]):
            raise UnsafeFixture("Disarm all tracks before the suite can write")
        if self.anchor and verify_peer:
            anchor, expected_name = self.anchor
            observed, rs_name = await self.pair(anchor, "name")
            if observed["value"] != expected_name or rs_name != expected_name:
                raise UnsafeFixture("The two bridges must see the same newly created fixture marker")
        return overview

    async def max_get(self, target: Target, member: str, **page: Any) -> dict:
        return checked(await self.max.get(target.path, target.kind, member, **page))

    async def pair(self, target: Target, member: str) -> tuple[dict, Any]:
        max_value = await self.max_get(target, member)
        rs_value = (await self.rs(target.path, member))[member]
        return max_value, rs_value

    async def settled_pair(
        self, target: Target, member: str, identity: str, expected: Any,
    ) -> tuple[dict, Any, list[dict]]:
        # Max can read its cached value immediately after the native set. Poll
        # fresh getters only. A partial reply never causes the write to be sent again.
        start = time.perf_counter()
        samples = []
        for delay in READBACK_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            max_frame, remote_value = await self.pair(target, member)
            max_value = max_frame["value"]
            observed_identity = max_frame["target"]["identity"]
            samples.append({
                "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
                "m4l": max_value,
                "remote_script": remote_value,
                "identity_unchanged": observed_identity == identity,
            })
            if observed_identity != identity or (same(max_value, expected) and same(remote_value, expected)):
                break
        return max_frame, remote_value, samples

    async def set(self, target: Target, member: str, value: Any, baseline: dict, *, preview: bool) -> dict:
        return await self.max.request(
            "set", path=target.path, object_type=target.kind, member=member, value=value,
            expected_identity=baseline["target"]["identity"], expected_value=baseline["value"], dry_run=preview,
        )

    async def scalar(self, label: str, target: Target, member: str, choices: Any) -> None:
        await self.guard()
        try:
            baseline, initial_rs = await self.pair(target, member)
        except BridgeReply as exc:
            if unexposed(exc.reply):
                self.row(label, "EXPECTED", stage="baseline", reply=exc.reply)
            else:
                self.row(label, "FAIL", stage="baseline", reply=exc.reply)
            return
        initial = baseline["value"]
        if not same(initial, initial_rs):
            self.row(label, "FAIL", stage="baseline", m4l=initial, remote_script=initial_rs)
            return
        candidates = choices(initial) if callable(choices) else choices
        alternate = next((v for v in candidates if not same(v, initial)), None)
        if alternate is None:
            raise ValueError(f"{label}: no distinct bounded test value")
        preview = await self.set(target, member, alternate, baseline, preview=True)
        if unexposed(preview):
            self.row(label, "EXPECTED", stage="preview", reply=preview)
            return
        if preview.get("status") != "preview":
            self.row(label, "FAIL", stage="preview", reply=preview)
            return
        evidence: dict[str, Any] = {"property": member, "path": target.path, "before": initial, "requested": alternate}
        try:
            evidence["write"] = await self.set(target, member, alternate, baseline, preview=False)
        except M4LError as exc:
            # Read both sides before deciding if a compensating write is required.
            evidence["transport_error"] = str(exc)
        identity = baseline["target"]["identity"]
        changed_max, changed_rs, samples = await self.settled_pair(target, member, identity, alternate)
        evidence["change_readbacks"] = samples
        observed = changed_max["value"]
        evidence["changed"] = {"m4l": observed, "remote_script": changed_rs}
        if changed_max["target"]["identity"] != identity or not same(observed, changed_rs):
            self.row(label, "FAIL", reason="backend mismatch or target replacement after bounded readback", **evidence)
            raise UnsafeFixture(f"{label}: residual mismatch after {len(samples)} reads; no compensation sent")
        if not same(observed, initial):
            await self.guard()
            try:
                evidence["compensation"] = await self.set(target, member, initial, changed_max, preview=False)
            except M4LError as exc:
                evidence["compensation_transport_error"] = str(exc)
        restored_max, restored_rs, samples = await self.settled_pair(target, member, identity, initial)
        evidence["restoration_readbacks"] = samples
        evidence["restored"] = {"m4l": restored_max["value"], "remote_script": restored_rs}
        if (
            restored_max["target"]["identity"] != identity
            or not same(restored_max["value"], initial) or not same(restored_rs, initial)
        ):
            self.row(label, "FAIL", reason="restoration mismatch", **evidence)
            raise UnsafeFixture(f"{label}: both backends must confirm restoration")
        response = evidence.get("write", {})
        immediate_success = response.get("status") == "ok"
        settled_partial = (
            response.get("status") == "partial"
            and response.get("verification_kind") == "no_observable_change"
            and response.get("write_may_have_occurred") is True
        )
        verification = "dual_after_native_partial" if settled_partial else "dual"
        tolerance = None
        if (
            target.kind == "DeviceParameter" and member == "value"
            and response.get("status") == "partial" and response.get("verification_kind") == "mismatch"
            and response.get("write_may_have_occurred") is True
        ):
            metadata = await self.rs(target.path, "min", "max", "is_quantized")
            tolerance = parameter_rounding(alternate, (response.get("value"), observed, changed_rs), metadata)
            if tolerance is not None:
                verification = "dual_with_float32_rounding"
                evidence["rounding_tolerance"] = tolerance
        if (
            (immediate_success or settled_partial or tolerance is not None)
            and same(observed, alternate) and not same(observed, initial)
        ):
            self.row(label, "PASS", category="real_set", verification=verification, **evidence)
        elif unexposed(response) and same(observed, initial):
            self.row(label, "EXPECTED", stage="write", **evidence)
        elif response.get("status") == "partial" and same(observed, initial):
            self.row(label, "FAIL", reason="both backends retained the baseline after the partial write", **evidence)
        else:
            self.row(label, "FAIL", reason="requested state was not verified by both backends", **evidence)
            if "transport_error" in evidence or response.get("status") == "partial":
                raise UnsafeFixture(f"{label}: ambiguous write, stopped without retry")

    async def call(self, label: str, target: Target, member: str, args: list, inspect, expected: Any) -> bool:
        await self.guard()
        try:
            before = await inspect()
        except BridgeReply as exc:
            self.row(label, "EXPECTED" if unexposed(exc.reply) else "FAIL", stage="before", reply=exc.reply)
            return False
        if before[0] != before[1] or before[0] == expected:
            self.row(label, "FAIL", stage="before", before=before, expected=expected)
            return False
        resolved = checked(await self.max.resolve(target.path, target.kind))
        fields = {
            "path": target.path, "object_type": target.kind, "member": member, "arguments": args,
            "expected_identity": resolved["target"]["identity"],
        }
        preview = await self.max.request("call", dry_run=True, **fields)
        if unexposed(preview):
            self.row(label, "EXPECTED", stage="preview", reply=preview)
            return False
        if preview.get("status") != "preview":
            self.row(label, "FAIL", stage="preview", reply=preview)
            return False
        try:
            response = await self.max.request("call", dry_run=False, **fields)
        except M4LError as exc:
            response = {"status": "unknown", "detail": str(exc), "write_may_have_occurred": True}
        try:
            after = await inspect()
        except Exception as exc:
            self.row(label, "FAIL", stage="after", before=before, expected=expected, reply=response,
                     readback_error=str(exc))
            raise UnsafeFixture(f"{label}: readback failed after the native call") from exc
        if response.get("status") == "ok" and after == (expected, expected):
            self.row(label, "PASS", category="real_call", before=before, after=after, reply=response)
            return True
        if unexposed(response) and after == before:
            self.row(label, "EXPECTED", stage="call", reply=response)
            return False
        self.row(label, "FAIL", before=before, after=after, expected=expected, reply=response)
        raise UnsafeFixture(f"{label}: native call or postcondition failed, stopped without retry")

    async def count(self, target: Target, member: str) -> tuple[int, int]:
        frame = await self.max_get(target, member, limit=1)
        rs = await self.live.request("lom.children", {"path": target.path, "attr": member, "limit": 1})
        if empty_reference_sentinel(target, member, frame):
            self.report.setdefault("empty_collection_sentinels", []).append({
                "path": target.path, "member": member, "reported_total": frame["total"], "value": frame["value"],
            })
            return 0, rs["total"]
        return frame["total"], rs["total"]

    async def clip_present(self, slot: Target) -> tuple[bool, bool]:
        frame, value = await self.pair(slot, "has_clip")
        return bool(frame["value"]), bool(value)

    async def selection(self) -> dict:
        return stable_selection(await self.rs("live_set view", "selected_track", "selected_scene"))

    async def fingerprint(self) -> dict:
        overview = await self.overview()
        tracks = []
        for i, track in enumerate(overview["tracks"]):
            details = await self.live.request("track.info", {"track": i, "clips": True})
            arrangement = [] if track.get("is_foldable") else await self.live.request(
                "arrangement.clips", {"track": i},
            )
            tracks.append({"track": details, "arrangement": arrangement})
        return {
            "song": await self.rs(SONG, *SONG_STATE),
            "tracks": tracks,
            "scenes": overview["scenes"],
            "returns": overview["return_tracks"],
            "master": overview["master"],
            "selection": await self.selection(),
        }

    async def track(self, kind: str, suffix: str) -> int:
        overview = await self.guard()
        name = f"{self.prefix}-{suffix}"
        ref = await self.live.request("track.create", {"kind": kind, "name": name})
        identifier = ref["$ref"]
        self.tracks.add(identifier)
        details = await self.live.request("track.info", {"track": identifier, "clips": False})
        if details.get("arm"):
            await self.live.request("track.set", {"track": identifier, "arm": False})
        fresh = await self.overview()
        if len(fresh["tracks"]) != len(overview["tracks"]) + 1 or fresh["tracks"][-1]["name"] != name:
            raise UnsafeFixture("An appended fixture track cannot be identified")
        return len(fresh["tracks"]) - 1

    async def scene(self, suffix: str) -> int:
        overview = await self.guard()
        name = f"{self.prefix}-{suffix}"
        scene = await self.live.request("scene.create", {"index": -1, "name": name})
        self.scenes.add(scene["$ref"])
        fresh = await self.overview()
        if len(fresh["scenes"]) != len(overview["scenes"]) + 1 or fresh["scenes"][-1]["name"] != name:
            raise UnsafeFixture("An appended fixture scene cannot be identified")
        return len(fresh["scenes"]) - 1

    async def ready(self) -> None:
        def running(reply: dict) -> bool:
            return reply.get("status") == "ok" and reply.get("bootstrap", reply).get("runtime_ready") is True

        try:
            reply = await self.max.readiness()
        except M4LError:
            idx = await self.track("midi", "Bridge")
            await self.live.request("browser.load", {"track": idx, "query": "LOM_API_Bridge"}, timeout=60)
        else:
            if running(reply):
                return
            raise UnsafeFixture(f"A Max listener exists but is not ready: {reply!r}")
        for _ in range(40):
            try:
                reply = await self.max.readiness()
                if running(reply):
                    return
            except M4LError:
                pass  # Read-only readiness polling.
            await asyncio.sleep(0.25)
        raise UnsafeFixture("The fixture Max for Live device did not become ready")

    async def cleanup(self, selection: dict) -> None:
        await self.guard(verify_peer=False)
        for kind, identifiers, delete_op in (
            ("tracks", self.tracks, "delete_track"),
            ("scenes", self.scenes, "delete_scene"),
        ):
            refs = (await self.rs(SONG, kind))[kind]
            for index, ref in reversed(list(enumerate(refs))):
                if ref["$ref"] in identifiers:
                    await self.live.request("lom.call", {"path": SONG, "method": delete_op, "args": [index]})
        current = await self.selection()
        restore = {}
        for key, before in selection.items():
            if current.get(key) == before or not isinstance(before, dict):
                continue
            reference = before.get("$ref") or before.get("path")
            if reference:
                if "$ref" not in before and "path" in before and "name" in before:
                    target = await self.rs(reference, "name")
                    if target.get("name") != before["name"]:
                        raise UnsafeFixture("The selected object's canonical path now points to a different target")
                restore[key] = {"$ref": reference}
        if restore:
            await self.live.request("lom.set", {"path": "live_set view", "values": restore})

    async def prime_scene_color(self, scene: Target, index: int) -> bool:
        await self.guard()
        label = "Scene color fixture precondition"
        try:
            baseline, _ = await self.pair(scene, "color_index")
        except BridgeReply as exc:
            self.row(label, "EXPECTED" if unexposed(exc.reply) else "FAIL", reply=exc.reply)
            return False
        palette_index = 12 if baseline["value"] != 12 else 13
        setter_error = None
        try:
            await self.live.request("scene.set", {"index": index, "color_index": palette_index})
        except LiveError as exc:
            setter_error = str(exc)
        max_color, rs_color, samples = await self.settled_pair(
            scene, "color_index", baseline["target"]["identity"], palette_index,
        )
        if not samples[-1]["identity_unchanged"]:
            raise UnsafeFixture("The fixture scene changed during color setup")
        ready = setter_error is None and max_color["value"] == rs_color == palette_index
        self.row(label, "PASS" if ready else "FAIL", requested=palette_index, readbacks=samples,
                 setter_error=setter_error)
        return ready

    async def scalar_families(self, track: Target, scene: Target, slot: Target, midi: Target, audio: Target | None,
                              device: Target, *, scene_color_ready: bool) -> None:
        cases = [
            (self.song, "tempo", lambda v: [float(v) - 0.5 if v > 20.5 else float(v) + 0.5]),
            (self.song, "clip_trigger_quantization", [4, 5]),
            (self.song, "midi_recording_quantization", [1, 0]),
            (self.song, "groove_amount", [0.25, 0.5]),
            (self.song, "loop", [True, False]),
            (self.song, "loop_start", lambda v: [max(0, v - 0.25), v + 0.25]),
            (self.song, "loop_length", lambda v: [v + 0.5]),
            (self.song, "signature_numerator", [5, 7]),
            (self.song, "signature_denominator", [8, 4]),
            (track, "name", [self.prefix + "-Renamed"]),
            (track, "mute", [True, False]),
            (track, "solo", [True, False]),
            (track, "color_index", [3, 5]),
            (Target(track.path + " mixer_device", "MixerDevice"), "crossfade_assign", [0, 2]),
            (scene, "name", [self.prefix + "-SceneRenamed"]),
            (scene, "tempo", [132.0, 138.0]),
            (scene, "time_signature_numerator", [5, 7]),
            (scene, "time_signature_denominator", [8, 4]),
            (slot, "has_stop_button", [True, False]),
            (midi, "name", [self.prefix + "-ClipRenamed"]),
            (midi, "muted", [True, False]),
            (midi, "color_index", [7, 9]),
            (midi, "looping", [True, False]),
            (midi, "loop_start", [0.25, 0.5]),
            (midi, "loop_end", [7.5, 7.0]),
            (midi, "start_marker", [0.125, 0.25]),
            (midi, "end_marker", [7.75, 7.5]),
            (midi, "legato", [True, False]),
            (midi, "launch_mode", [1, 0]),
            (midi, "launch_quantization", [4, 5]),
            (midi, "velocity_amount", [0.5, 0.75]),
            (midi, "signature_numerator", [5, 7]),
            (midi, "signature_denominator", [8, 4]),
        ]
        if scene_color_ready:
            cases.append((scene, "color_index", [4, 6]))
        if audio is not None:
            cases += [
                (audio, "gain", [0.45, 0.35]),
                (audio, "pitch_coarse", [3, -2]),
                (audio, "pitch_fine", [12.0, -11.0]),
                (audio, "muted", [True, False]),
            ]
        else:
            self.row("Audio clip fixture", "EXPECTED", reason="No local --sample provided")
        for target, member, variants in cases:
            await self.scalar(f"{target.kind}.{member} ({target.path})", target, member, variants)

        mixer = track.path + " mixer_device"
        for member, variants in (
            ("volume", [0.71, 0.65]),
            ("panning", [-0.21, 0.21]),
            ("sends 0", [0.31, 0.21]),
            ("sends 1", [0.29, 0.19]),
        ):
            path = mixer + " " + member
            if member.startswith("sends"):
                sends = (await self.rs(mixer, "sends"))["sends"]
                if int(member[-1]) >= len(sends):
                    self.row("MixerDevice " + member, "EXPECTED", reason="Test Set has no such return")
                    continue
            await self.scalar("MixerDevice " + member, Target(path, "DeviceParameter"), "value", variants)

        for index, _ in enumerate((await self.rs(device.path, "parameters"))["parameters"][:5]):
            path = device.path + f" parameters {index}"
            props = await self.rs(path, "name", "min", "max", "is_quantized")
            low, high = props["min"], props["max"]
            variants = [low, high] if props["is_quantized"] else [low + (high - low) * f for f in (0.42, 0.64)]
            await self.scalar("Reverb " + props["name"], Target(path, "DeviceParameter"), "value", variants)

    async def read_notes(self, clip: Target) -> tuple[list[tuple], list[tuple]]:
        frame = checked(await self.max.call(clip.path, clip.kind, "get_notes_extended", [0, 128, 0.0, 8.0]))
        readback = await self.live.request("notes.get", {"clip": clip.path, "time_span": 8.0})
        return note_rows(frame["result"]), note_rows(readback)

    async def clip_methods(self, track: Target, source: Target, destination: Target, empty: Target) -> None:
        async def empty_present() -> tuple[bool, bool]:
            return await self.clip_present(empty)

        if await self.call("ClipSlot.create_clip", empty, "create_clip", [4.0], empty_present, True):
            await self.call("ClipSlot.delete_clip (created)", empty, "delete_clip", [], empty_present, False)

        async def destination_notes() -> tuple[list[tuple] | None, list[tuple] | None]:
            present = await self.clip_present(destination)
            if present == (False, False):
                return None, None
            return await self.read_notes(Target(destination.path + " clip", "Clip"))

        async def destination_present() -> tuple[bool, bool]:
            return await self.clip_present(destination)

        async def arrangement_count() -> tuple[int, int]:
            return await self.count(track, "arrangement_clips")

        if await self.call("Track.create_midi_clip", track, "create_midi_clip", [0.0, 4.0], arrangement_count, 1):
            path = track.path + " arrangement_clips 0"
            await self.call("Track.delete_clip", track, "delete_clip", [{"$ref": path, "object_type": "Clip"}],
                            arrangement_count, 0)

        try:
            source_notes = await self.read_notes(Target(source.path + " clip", "Clip"))
        except BridgeReply as exc:
            self.row("Session clip duplication (notes)", "EXPECTED" if unexposed(exc.reply) else "FAIL",
                     reply=exc.reply)
            return
        if source_notes[0] != source_notes[1]:
            self.row("Session clip duplication (notes)", "FAIL", source=source_notes)
            return
        if await self.call("ClipSlot.duplicate_clip_to", source, "duplicate_clip_to",
                           [{"$ref": destination.path, "object_type": "ClipSlot"}],
                           destination_notes, source_notes[0]):
            await self.call("ClipSlot.delete_clip (duplicate)", destination, "delete_clip", [],
                            destination_present, False)

    async def note_methods(self, clip: Target) -> None:
        async def notes() -> tuple[list[tuple], list[tuple]]:
            return await self.read_notes(clip)

        try:
            before = await notes()
        except BridgeReply as exc:
            self.row("MIDI note methods", "EXPECTED" if unexposed(exc.reply) else "FAIL", reply=exc.reply)
            return
        if before[0] != before[1]:
            self.row("MIDI note methods", "FAIL", before=before)
            return
        initial = before[1]
        added = {"pitch": 72, "start_time": 3.25, "duration": 0.375, "velocity": 83}
        changed = sorted(initial + [(72, 3.25, 0.375, 83)])
        if await self.call("Clip.add_new_notes", clip, "add_new_notes", [{"notes": [added]}], notes, changed):
            frame = checked(await self.max.call(clip.path, clip.kind, "get_notes_extended", [72, 1, 3.25, 0.5]))
            note_id = native_notes(frame["result"])[0]["note_id"]
            edited = sorted(initial + [(72, 3.25, 0.375, 91)])
            await self.call("Clip.apply_note_modifications", clip, "apply_note_modifications",
                            [{"notes": [{"note_id": note_id, "velocity": 91}]}], notes, edited)
            await self.call("Clip.remove_notes_by_id", clip, "remove_notes_by_id", [[note_id]], notes, initial)

    async def device_methods(self, track: Target) -> None:
        async def devices() -> tuple[int, int]:
            return await self.count(track, "devices")

        baseline = (await devices())[1]
        if await self.call("Track.insert_device", track, "insert_device", ["Utility", baseline],
                           devices, baseline + 1):
            inserted = Target(track.path + f" devices {baseline}", "Device")
            frame, rs_name = await self.pair(inserted, "name")
            if frame["value"] != "Utility" or rs_name != "Utility":
                raise UnsafeFixture("Both bridges must identify the appended fixture Utility")
            await self.call("Track.delete_device", track, "delete_device", [baseline], devices, baseline)

    async def song_methods(self) -> None:
        for kind, member, collection, args in (
            ("Track", "create_midi_track", "tracks", [-1]),
            ("Scene", "create_scene", "scenes", [-1]),
        ):
            async def count(member: str = collection) -> tuple[int, int]:
                return await self.count(self.song, member)

            baseline = (await count())[1]
            if not await self.call(f"Song.{member}", self.song, member, args, count, baseline + 1):
                continue
            refs = (await self.rs(SONG, collection))[collection]
            owners = self.tracks if collection == "tracks" else self.scenes
            identifier = refs[baseline]["$ref"]
            owners.add(identifier)
            if kind == "Track":
                info = await self.live.request("track.info", {"track": identifier, "clips": False})
                if info.get("arm"):
                    await self.live.request("track.set", {"track": identifier, "arm": False})
            if kind == "Scene":
                if await self.call("Song.duplicate_scene", self.song, "duplicate_scene", [baseline],
                                   count, baseline + 2):
                    copied = (await self.rs(SONG, "scenes"))["scenes"][baseline + 1]["$ref"]
                    owners.add(copied)
                    await self.call("Song.delete_scene (duplicate)", self.song, "delete_scene", [baseline + 1],
                                    count, baseline + 1)
            await self.call(f"Song.delete_{kind.lower()}", self.song, f"delete_{kind.lower()}", [baseline],
                            count, baseline)

    async def run(self) -> None:
        initial = None
        disk_before = sha256(self.fixture)
        self.report["saved_file_sha256_before"] = disk_before
        try:
            await self.guard()
            initial = await self.fingerprint()
            await self.ready()
            ti = await self.track("midi", "MIDI")
            ai = await self.track("audio", "Audio") if self.sample else None
            si = await self.scene("A")
            di = await self.scene("B")
            ei = await self.scene("Empty")
            track = Target(f"live_set tracks {ti}", "Track")
            scene = Target(f"live_set scenes {si}", "Scene")
            source = Target(f"{track.path} clip_slots {si}", "ClipSlot")
            destination = Target(f"{track.path} clip_slots {di}", "ClipSlot")
            empty = Target(f"{track.path} clip_slots {ei}", "ClipSlot")
            midi = Target(f"{source.path} clip", "Clip")
            self.anchor = (Target(f"live_set scenes {di}", "Scene"), f"{self.prefix}-B")
            await self.guard()
            await self.live.request("scene.set", {"index": si, "tempo": 131.0, "tempo_enabled": True,
                                                   "time_signature_numerator": 4, "time_signature_denominator": 4,
                                                   "time_signature_enabled": True})
            await self.live.request("clip.create", {
                "track": ti, "slot": si, "length": 8, "name": self.prefix,
                "notes": [{"pitch": 60, "start_time": 0, "duration": 1, "velocity": 80},
                          {"pitch": 67, "start_time": 2, "duration": 0.5, "velocity": 76}],
            })
            scene_color_ready = await self.prime_scene_color(scene, si)
            audio = None
            if ai is not None and self.sample is not None:
                await self.live.request("sample.load", {
                    "path": str(self.sample), "target": "session", "track": ai, "slot": si,
                })
                audio = Target(f"live_set tracks {ai} clip_slots {si} clip", "Clip")
            device = await self.live.request("device.insert", {"name": "Reverb", "track": ti})
            reverb = Target(f"{track.path} devices {device['index']}", "Device")
            await self.scalar_families(track, scene, empty, midi, audio, reverb,
                                       scene_color_ready=scene_color_ready)
            await self.song_methods()
            await self.clip_methods(track, source, destination, empty)
            await self.device_methods(track)
            await self.note_methods(midi)
        except Exception as exc:
            self.row("Suite stopped", "FAIL", reason=str(exc))
        finally:
            if initial is not None:
                try:
                    await self.cleanup(initial["selection"])
                    after = await self.fingerprint()
                    difference = first_difference(initial, after)
                    self.report["cleanup_difference"] = difference
                    self.report["selection"] = {"before": initial["selection"], "after": after["selection"]}
                    self.row("Original fixture state", "FAIL" if difference else "PASS", difference=difference)
                except Exception as exc:
                    self.row("Cleanup", "FAIL", reason=str(exc))
            disk_after = sha256(self.fixture)
            self.report["saved_file_sha256_after"] = disk_after
            self.row("Saved fixture bytes", "PASS" if disk_before == disk_after else "FAIL")
            await self.max.close()
            await self.live.close()


def local_file(path: Path, suffixes: set[str], parser: argparse.ArgumentParser) -> Path:
    if not path.is_absolute() or not path.is_file() or path.suffix.lower() not in suffixes:
        parser.error(f"Provide an absolute existing {', '.join(sorted(suffixes))} file: {path}")
    return path.resolve(strict=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True, help="Exact path of the open disposable ZZ- .als")
    parser.add_argument("--sample", type=Path, help="Absolute local fixture audio for the audio-clip cases")
    parser.add_argument("--json", type=Path, required=True, help="New evidence JSON path")
    args = parser.parse_args()
    fixture = local_file(args.fixture, {".als"}, parser)
    if not fixture.stem.startswith("ZZ-"):
        parser.error("The resolved fixture name must start with ZZ-")
    sample = local_file(args.sample, {".wav", ".aif", ".aiff", ".flac"}, parser) if args.sample else None
    if args.json.suffix.lower() != ".json" or args.json.exists() or not args.json.parent.is_dir():
        parser.error("--json must point to a new .json file in an existing directory")
    suite = Suite(fixture, sample)
    with args.json.open("x", encoding="utf-8") as evidence:
        asyncio.run(suite.run())
        counts = Counter(row["status"] for row in suite.rows)
        phases = Counter(row["category"] for row in suite.rows if row["status"] == "PASS" and "category" in row)
        suite.report["counts"] = dict(counts)
        suite.report["verified_writes"] = dict(phases)
        suite.report["minimum_verified_writes"] = 35
        json.dump(suite.report, evidence, indent=2, ensure_ascii=False)
        evidence.write("\n")
    return 1 if counts["FAIL"] or sum(phases.values()) < 35 or not phases["real_call"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
