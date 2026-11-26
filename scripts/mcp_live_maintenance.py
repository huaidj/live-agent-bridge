"""Verify Remote Script maintenance on an already open, disposable ZZ- Live Set.

Pass the exact .als path and a new JSON receipt path. Keep Live stopped and the
fixture open until the script finishes. --timer-config is optional and must
identify the config of the running AgentBridge installation, verified separately.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from live_agent_bridge.client import LiveClient, LiveError  # noqa: E402

SONG_FLAGS = ("is_playing", "record_mode", "session_record", "punch_in", "punch_out")
SONG_KEYS = ("name", "file_path", "start_time", "back_to_arranger", *SONG_FLAGS)
BROWSER_LIMIT = 8
REQUEST_TIMEOUT = 4.0
SOCKET_DROP_SECONDS = 7.0
RECONNECT_SECONDS = 12.0


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def fixture_path(path: Path) -> Path:
    path = path.resolve(strict=True)
    require(path.is_file() and path.suffix.lower() == ".als" and path.stem.startswith("ZZ-"),
            "--fixture must identify an existing disposable ZZ- .als file")
    return path


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def state_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def without_refs(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: without_refs(item) for key, item in value.items() if key not in {"ref", "$ref"}}
    if isinstance(value, list):
        return [without_refs(item) for item in value]
    return value


async def request(live: LiveClient, op: str, **args: Any) -> Any:
    return await live.request(op, args, timeout=REQUEST_TIMEOUT)


async def guard(live: LiveClient, fixture: Path) -> tuple[dict, dict]:
    song = await request(live, "lom.get", path="live_set", props=list(SONG_KEYS))
    filename = song.get("file_path")
    require(isinstance(filename, str) and Path(filename).is_absolute()
            and Path(filename).resolve(strict=True) == fixture and song.get("name") == fixture.stem,
            "Live must have the exact named ZZ- fixture open")
    require(all(song.get(flag) is False for flag in SONG_FLAGS),
            "Transport, recording and both punch switches must be off")
    overview = await request(live, "overview", fresh=True)
    require(all(overview.get(flag) is False for flag in SONG_FLAGS[:3]),
            "Fresh overview disagrees with the stopped fixture")
    for index, track in enumerate(overview["tracks"]):
        if track.get("arm") is False:
            continue
        arm = await request(live, "lom.get", path=f"live_set tracks {index}", props=["can_be_armed", "arm"])
        require(arm.get("arm") is False or (arm.get("can_be_armed") is False and arm.get("arm") is not True),
                f"Fixture track {index} must be demonstrably unarmed")
    return song, overview


async def snapshot(live: LiveClient, fixture: Path) -> dict:
    song, overview = await guard(live, fixture)
    tracks = []
    for index, summary in enumerate(overview["tracks"]):
        track = await request(live, "track.info", track=index, clips=True)
        arrangement = [] if summary.get("is_foldable") else await request(live, "arrangement.clips", track=index)
        clip_args = [{"track": index, "slot": clip["slot"]} for clip in track["clips"]]
        clip_args.extend({"clip": clip["ref"]["$ref"]} for clip in arrangement)
        clips = []
        for args in clip_args:
            info = await request(live, "clip.info", **args)
            notes = await request(live, "notes.get", **args) if info.get("is_midi_clip") else None
            clips.append({"info": without_refs(info), "notes": notes})
        tracks.append({"track": without_refs(track), "arrangement": without_refs(arrangement), "clips": clips})
    song_end, overview_end = await guard(live, fixture)
    overview.pop("as_of", None)
    overview_end.pop("as_of", None)
    require(song == song_end and overview == overview_end, "Fixture state moved during the fresh snapshot")
    return {"song": song, "overview": overview, "tracks": tracks}


def browser_receipt(result: Any) -> dict:
    require(isinstance(result, list) and 0 < len(result) <= BROWSER_LIMIT,
            "browser.tree must return a nonempty raw list within the requested limit")
    for entry in result:
        require(isinstance(entry, dict) and isinstance(entry.get("name"), str) and bool(entry["name"])
                and "uri" in entry and isinstance(entry["uri"], (str, type(None)))
                and type(entry.get("loadable")) is bool and type(entry.get("folder")) is bool,
                "browser.tree returned a malformed browser row")
    require(any(entry["uri"] for entry in result), "browser.tree returned no addressable browser item")
    return {"raw_type": "list", "count": len(result), "sha256": state_digest(result),
            "sample_names": [entry["name"] for entry in result[:3]]}


async def reconnect(live: LiveClient, receipt: dict) -> None:
    clock = asyncio.get_running_loop().time
    stop = clock() + RECONNECT_SECONDS
    while clock() < stop:
        try:
            async with asyncio.timeout(stop - clock()):
                await live.connect()
                status = await request(live, "status")
            require(status.get("pong") is True, "New connection did not return healthy status")
            receipt["new_tick_count"] = status["tick_count"]
            receipt["new_bridge"] = status["bridge"]
            receipt["reconnected"] = True
            return
        except LiveError as exc:
            receipt["last_reconnect_error"] = exc.etype
            await live.close()
            await asyncio.sleep(0.15)
        except TimeoutError:
            break
    raise RuntimeError("AgentBridge did not reconnect within the configured deadline")


async def reload_bridge(live: LiveClient, fixture: Path, receipt: dict) -> None:
    await guard(live, fixture)
    status = await request(live, "status")
    receipt["old_bridge"] = status["bridge"]
    receipt["old_tick_count"] = status["tick_count"]
    receipt["reload_requested"] = True
    try:
        receipt["reply"] = await request(live, "bridge.reload")
    except LiveError as exc:
        if exc.etype != "ConnectionLost":
            raise
        receipt["reload_reply_error"] = exc.etype

    stop = asyncio.get_running_loop().time() + SOCKET_DROP_SECONDS
    while live.connected and asyncio.get_running_loop().time() < stop:
        await asyncio.sleep(0.03)
    receipt["old_socket_dropped"] = not live.connected
    require(receipt["old_socket_dropped"], "Original connection did not drop after bridge.reload")
    await reconnect(live, receipt)
    await guard(live, fixture)
    require(receipt.get("reply") == {"reloading": True}, "Reload socket dropped without the expected reply")


def read_timer_config(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict) and type(value.get("timer_interval_ms")) is int
            and value["timer_interval_ms"] > 0, "Owning config has no exact positive timer interval")
    return value


def timer_baseline(path: Path | None) -> tuple[Path, dict]:
    require(path is not None, "No independently verified owning --timer-config was supplied")
    path = path.resolve(strict=True)
    require(path.is_file() and path.name == "config.json" and path.parent.name == "AgentBridge"
            and path.parent.parent.name == "Remote Scripts" and (path.parent / "__init__.py").is_file(),
            "The timer config is not colocated with an installed AgentBridge Remote Script")
    return path, read_timer_config(path)


async def timer_probe(live: LiveClient, fixture: Path, path: Path, original: dict, receipt: dict) -> None:
    before = original["timer_interval_ms"]
    trial = 2 if before == 1 else before - 1
    receipt.update(original_ms=before, trial_ms=trial)
    await guard(live, fixture)
    require(read_timer_config(path) == original, "Timer config changed since baseline")
    receipt["trial_requested"] = True
    try:
        reply = await request(live, "bridge.timer", interval_ms=trial)
        receipt["trial_reply"] = reply
        expected = {**original, "timer_interval_ms": trial}
        require(reply == {"timer_active": True, "interval_ms": trial} and read_timer_config(path) == expected,
                "Timer trial did not activate and persist in the supplied owning config")
    finally:
        await guard(live, fixture)
        receipt["restore_requested"] = True
        reply = await request(live, "bridge.timer", interval_ms=before)
        receipt["restore_reply"] = reply
        status = await request(live, "status")
        receipt["restored"] = (reply == {"timer_active": True, "interval_ms": before}
                               and status["tick_mode"] == "timer" and read_timer_config(path) == original)
        require(receipt["restored"], "Could not verify the previous active timer and owning config were restored")


def record(report: dict, check: str, status: str, **details: Any) -> None:
    report["checks"].append({"check": check, "status": status, **details})
    print(f"{status:7} {check}", flush=True)


async def run(fixture: Path, timer_config: Path | None, report: dict) -> None:
    live = LiveClient(host="127.0.0.1", connect_timeout=0.8)
    try:
        await live.connect()
        required = {"lom.get", "overview", "track.info", "arrangement.clips", "clip.info", "notes.get",
                    "status", "browser.tree", "bridge.reload"}
        available = set((live.hello or {}).get("ops", []))
        missing = sorted(required - available)
        require(not missing, f"Running Remote Script lacks required operations: {missing}")
        before = await snapshot(live, fixture)
        before_disk = file_digest(fixture)
        record(report, "fixture baseline", "PASS", state_sha256=state_digest(before), file_sha256=before_disk,
               tracks=len(before["tracks"]))

        data = await request(live, "browser.tree", category="instruments", limit=BROWSER_LIMIT)
        record(report, "browser.tree raw list", "PASS", **browser_receipt(data))

        reload_data: dict = {}
        report["reload"] = reload_data
        await reload_bridge(live, fixture, reload_data)
        after = await snapshot(live, fixture)
        after_disk = file_digest(fixture)
        unchanged = before == after and before_disk == after_disk
        reload_data["fixture_unchanged"] = unchanged
        record(report, "bridge.reload and fresh comparison", "PASS" if unchanged else "FAIL",
               state_sha256=state_digest(after), file_sha256=after_disk,
               changed_state_sections=[key for key in before if before[key] != after[key]])
        if not unchanged:
            return

        status = await request(live, "status")
        try:
            require("bridge.timer" in set((live.hello or {}).get("ops", [])), "Running bridge has no timer operation")
            require(status.get("tick_mode") == "timer", "Running bridge has no active fast timer")
            config, original = timer_baseline(timer_config)
        except (OSError, ValueError, RuntimeError) as exc:
            record(report, "bridge.timer", "BOUNDED", reason=str(exc))
        else:
            report["timer"] = timer_data = {}
            await timer_probe(live, fixture, config, original, timer_data)
            record(report, "bridge.timer trial and restore", "PASS", **timer_data)

        final = await snapshot(live, fixture)
        final_disk = file_digest(fixture)
        unchanged = final == before and final_disk == before_disk
        record(report, "final fixture comparison", "PASS" if unchanged else "FAIL",
               state_sha256=state_digest(final), file_sha256=final_disk,
               changed_state_sections=[key for key in before if before[key] != final[key]])
    finally:
        await live.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--json", type=Path, required=True, help="New JSON receipt path")
    parser.add_argument("--timer-config", type=Path, help="Independently verified active AgentBridge config.json")
    args = parser.parse_args()
    require(args.json.suffix.lower() == ".json", "--json must identify a .json receipt file")
    report: dict[str, Any] = {"fixture": str(args.fixture), "checks": []}
    with args.json.open("x", encoding="utf-8") as output:
        completed = False
        try:
            fixture = fixture_path(args.fixture)
            report["fixture"] = str(fixture)
            asyncio.run(run(fixture, args.timer_config, report))
            completed = True
        except (RuntimeError, LiveError, OSError, ValueError, TypeError, KeyError) as exc:
            record(report, "maintenance stopped", "FAIL", error_type=type(exc).__name__, reason=str(exc))
        finally:
            report["passed"] = completed and not any(row["status"] == "FAIL" for row in report["checks"])
            output.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
