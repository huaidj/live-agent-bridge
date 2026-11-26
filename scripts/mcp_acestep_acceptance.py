"""Run the local ACE-Step MCP lifecycle on a saved, stopped disposable Live set.

The deployment's own launcher records its PID and logs. This probe keeps its
downloads, task receipts, synthetic reference and report in a new run directory.
Start and stop require the explicit --start-local-service flag. No service or Live
tools run for --help. Use --lifecycle-only to recheck service start, readiness,
and shutdown without generating or importing audio.
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

STAGES = (
    "fixture", "initial_service_status", "service_start", "service_ready", "generation_submission",
    "result_by_task_id", "decoded_audio", "live_import", "live_cleanup", "service_stop", "fixture_file_unchanged",
)
GENERATION_STAGES = {"generation_submission", "result_by_task_id", "decoded_audio", "live_import", "live_cleanup"}
TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
REFERENCE_SECONDS = 12.0
DEPLOYMENT_URL = "http://127.0.0.1:18011"
DEPLOYMENT_MODEL = "acestep-v15-xl-base"


class AcceptanceError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceError(message)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def configured_path(value: Path | None, env_name: str) -> Path:
    supplied = value or os.environ.get(env_name)
    require(bool(supplied), f"Supply a local directory or set {env_name}")
    path = Path(supplied).resolve(strict=True)
    require(path.is_dir(), f"Expected a directory: {path}")
    return path


class Report:
    def __init__(self, directory: Path, lifecycle_only: bool):
        self.path = directory / "acceptance.json"
        self.mode = "lifecycle-only" if lifecycle_only else "full"
        self.skipped = GENERATION_STAGES if lifecycle_only else set()
        self.stages = {stage: {"status": "NOT_RUN", "reason": "Stage not reached."} for stage in STAGES}
        for stage in self.skipped:
            self.stages[stage]["reason"] = "Skipped by --lifecycle-only; no generation or Live import requested."
        self.write()

    def write(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"mode": self.mode, "stages": self.stages}, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def mark(self, stage: str, status: str, **evidence) -> None:
        self.stages[stage] = {"status": status, **evidence}
        self.write()
        print(f"{status:7} {stage}", flush=True)

    def passed(self) -> bool:
        return all(row["status"] == ("NOT_RUN" if stage in self.skipped else "PASS")
                   for stage, row in self.stages.items())


async def call(client: ClientSession, tool: str, timeout: float = 20, **arguments) -> dict:
    result = await asyncio.wait_for(client.call_tool(tool, arguments), timeout=timeout)
    message = "".join(item.text for item in result.content if item.type == "text")
    if result.isError:
        raise AcceptanceError(f"{tool}: {message}")
    parsed = result.structuredContent or json.loads(message)
    require(isinstance(parsed, dict), f"Invalid {tool} response")
    return parsed


async def stopped_fixture(client: ClientSession, expected: Path) -> tuple[dict, list[str]]:
    song = await call(client, "lom_get", path="live_set",
                      props=["name", "file_path", "is_playing", "record_mode", "session_record",
                             "tempo", "signature_numerator"])
    actual = song.get("file_path")
    require(bool(actual) and Path(actual).resolve() == expected, f"Live does not have the requested fixture: {actual}")
    require(all(song.get(flag) is False for flag in ("is_playing", "record_mode", "session_record")),
            "The exact fixture must be stopped with both record modes off")
    session = await call(client, "get_session", fresh=True)
    require(session.get("is_playing") is False and isinstance(session.get("tracks"), list),
            "Live transport or session changed during preflight")
    return song, [track["name"] for track in session["tracks"]]


def fresh_launch_record(receipt: Path, project: Path, url: str, before_hash: str | None,
                        attempted_at: float) -> dict:
    require(receipt.is_file() and digest(receipt) != before_hash, "Deployment PID receipt did not change")
    record = json.loads(receipt.read_text(encoding="utf-8-sig"))
    pid = record.get("pid")
    started = datetime.strptime(record.get("started_utc", ""), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    require(int(attempted_at) <= started.timestamp() <= time.time(), "PID receipt is outside this launch attempt")
    require(type(pid) is int and pid > 0 and record.get("url", "").rstrip("/") == url
            and Path(record.get("launcher", "")).resolve() == (project / "runtime" / "serve.py").resolve()
            and Path(record.get("python", "")).resolve()
            == (project / "runtime" / ".venv" / "Scripts" / "python.exe").resolve(),
            "New PID receipt does not identify the configured model deployment")
    return record


def proof_of_fresh_launch(result: dict, receipt: Path, project: Path, url: str,
                          before_hash: str | None, attempted_at: float) -> dict:
    require(result.get("exit_code") == 0 and not result.get("skipped"), "ACE-Step launcher did not report a new start")
    record = fresh_launch_record(receipt, project, url, before_hash, attempted_at)
    parsed = []
    # The existing launcher prints a preflight line, then a JSON record for a new process.
    output = result.get("output", "")
    for match in re.finditer(r"\{", output):
        try:
            candidate, _ = json.JSONDecoder().raw_decode(output[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            parsed.append(candidate)
    require(record in parsed,
            "New launcher output did not identify the PID written by the configured deployment")
    return record


def windows_process(pid: int) -> dict:
    require(type(pid) is int and pid > 0, "Invalid listener PID")
    command = (f'Get-CimInstance Win32_Process -Filter "ProcessId={pid}" '
               "| Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress")
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, text=True, timeout=12, check=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    details = json.loads(result.stdout.strip()) if result.stdout.strip() else {}
    require(isinstance(details, dict) and details.get("ProcessId") == pid and details.get("ExecutablePath")
            and details.get("CommandLine"), "Could not independently read the model process identity")
    return details


def windows_argv(command_line: str) -> list[str]:
    from ctypes import POINTER, WinDLL, byref, c_int, c_void_p, cast
    from ctypes.wintypes import LPCWSTR, LPWSTR

    shell_api = WinDLL("shell32", use_last_error=True)
    kernel_api = WinDLL("kernel32", use_last_error=True)
    parse = shell_api.CommandLineToArgvW
    parse.argtypes = (LPCWSTR, POINTER(c_int))
    parse.restype = POINTER(LPWSTR)
    release = kernel_api.LocalFree
    release.argtypes = (c_void_p,)
    release.restype = c_void_p
    count = c_int()
    arguments = parse(command_line, byref(count))
    require(bool(arguments), "Could not parse the Windows model command line")
    try:
        return [arguments[index] for index in range(count.value)]
    finally:
        release(cast(arguments, c_void_p))


def process_has_launcher(process: dict, launcher: Path) -> bool:
    arguments = windows_argv(process["CommandLine"])
    if len(arguments) != 2:
        return False
    supplied = Path(arguments[1])
    return supplied.is_absolute() and supplied.resolve() == launcher


def confirm_listener_ownership(pid: int, owner: dict) -> None:
    # A Windows venv shim can be the recorded PID while the listener is its direct child.
    launcher = Path(owner["launcher"]).resolve()
    parent = windows_process(owner["pid"])
    require(Path(parent["ExecutablePath"]).resolve() == Path(owner["python"]).resolve()
            and process_has_launcher(parent, launcher), "Recorded launcher identity no longer matches")
    if pid == owner["pid"]:
        return
    listener = windows_process(pid)
    require(listener["ParentProcessId"] == owner["pid"]
            and Path(listener["ExecutablePath"]).name.casefold() == "python.exe"
            and process_has_launcher(listener, launcher),
            "The ready listener is not the recorded launcher or its exact Python child")


def require_listener_off(port: int) -> None:
    try:
        # Winsock can report the explicit refusal just after two seconds.
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            raise AcceptanceError(f"Loopback port {port} already accepts connections")
    except OSError as error:
        if (isinstance(error, ConnectionRefusedError) or error.errno in {errno.ECONNREFUSED, 10061}
                or getattr(error, "winerror", None) == 10061):
            return
        raise AcceptanceError(f"Loopback port {port} was not confirmed closed: {error}") from error


async def wait_for_shutdown(port: int) -> None:
    for attempt in range(10):
        try:
            require_listener_off(port)
            return
        except AcceptanceError:
            if attempt == 9:
                raise
            await asyncio.sleep(0.5)


async def wait_ready(client: ClientSession, owner: dict, timeout: int) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        status = await call(client, "acestep_status", timeout=10)
        if status.get("ready") is True and status.get("reachable") is True:
            require(status.get("model_ok") is True and status.get("lm_loaded") is False,
                    "The ready service does not match the configured local inference model")
            pid = status.get("server_pid")
            require(type(pid) is int and pid > 0, "ACE-Step readiness did not identify the listener PID")
            await asyncio.to_thread(confirm_listener_ownership, pid, owner)
            return status
        await asyncio.sleep(1)
    raise AcceptanceError("Owned ACE-Step launcher did not become ready before the deadline")


def reference_wav(path: Path, supplied: Path | None, bpm: int) -> Path:
    import numpy as np
    import soundfile as sf

    if supplied is not None:
        source = supplied.resolve(strict=True)
        require(source.is_file() and source.suffix.lower() == ".wav", "The optional synthetic fixture must be a WAV")
        with sf.SoundFile(source) as stream:
            require(8000 <= stream.samplerate <= 192000 and 10 <= len(stream) / stream.samplerate
                    and stream.channels in (1, 2),
                    "Reference fixture must contain at least 10 seconds of mono or stereo audio")
            rate = stream.samplerate
            samples = stream.read(round(REFERENCE_SECONDS * rate), dtype="float32", always_2d=True)
    else:
        rate = 48000
        time = np.arange(round(REFERENCE_SECONDS * rate), dtype=np.float64) / rate
        beat = time * bpm / 60
        note = np.array([110.0, 130.8128, 164.8138, 195.9977])[(beat // 2).astype(int) % 4]
        tone = 0.13 * np.sin(2 * np.pi * note * time) * np.exp(-1.65 * (beat % 2))
        pulse = 0.09 * np.sin(2 * np.pi * 55 * time) * np.exp(-20 * (beat % 1))
        samples = np.column_stack((tone + pulse, 0.92 * tone + pulse))
        ramp = np.linspace(0, 1, 1200)[:, None]
        samples[:len(ramp)] *= ramp
        samples[-len(ramp):] *= ramp[::-1]
    require(np.isfinite(samples).all() and bool(np.any(samples)), "Synthetic fixture must contain finite signal")
    sf.write(path, samples, rate, subtype="PCM_16", format="WAV")
    return path


def recover_receipt_id(downloads: Path, reference: Path) -> str | None:
    matches = []
    for receipt in (downloads / "receipts").glob("*.json"):
        try:
            data = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        task = data.get("task_id", "")
        if (isinstance(task, str) and task == receipt.stem and TASK_ID.fullmatch(task)
                and data.get("meta", {}).get("reference") == str(reference)):
            matches.append(task)
    require(len(matches) <= 1, "More than one task receipt unexpectedly matches the single submission")
    return matches[0] if matches else None


async def submit_once(client: ClientSession, reference: Path, bpm: int, key: str, downloads: Path) -> str:
    try:
        answer = await call(client, "acestep_generate_part", timeout=105, reference=str(reference),
                            track_name="synth", prompt="A quiet monophonic synth response, no drums, bass or voice.",
                            bpm=bpm, key_scale=key, steps=8, seed=41, variations=1, wait_s=0, import_to="none")
    except (AcceptanceError, TimeoutError) as error:
        task = recover_receipt_id(downloads, reference)
        if task is None:
            raise AcceptanceError("Submission result is unknown and no matching task receipt exists. No retry was sent.") from error
        return task
    task = answer.get("task_id", "")
    require(isinstance(task, str) and bool(TASK_ID.fullmatch(task)), "No safe task ID was returned; do not resubmit")
    require(recover_receipt_id(downloads, reference) == task, "Task ID did not match its private submission receipt")
    require(not answer.get("imported"), "The generate-only request unexpectedly imported audio")
    return task


async def result_by_id(client: ClientSession, task: str, timeout: int) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while (remaining := deadline - asyncio.get_running_loop().time()) > 0:
        result = await call(client, "acestep_result", timeout=remaining, task_id=task,
                            wait_s=min(8, remaining), import_to="none")
        require(result.get("task_id") == task, "Result identity changed")
        if result.get("status") == "succeeded":
            return result
        require(result.get("status") in {"submitted", "running"}, f"ACE-Step generation failed: {result}")
        await asyncio.sleep(min(1, max(0, deadline - asyncio.get_running_loop().time())))
    raise AcceptanceError(f"Timed out awaiting the existing task {task}; no replacement task was submitted")


def check_audio(result: dict, downloads: Path, reference: Path) -> tuple[Path, dict]:
    import numpy as np
    import soundfile as sf

    outputs = result.get("outputs", [])
    require(len(outputs) == 1 and not result.get("imported"), "Expected one completed generation with no import")
    output = outputs[0]
    original, aligned = (Path(output[name]).resolve(strict=True) for name in ("original", "file"))
    require(all(path.is_relative_to(downloads) and path.is_file() and path.suffix.lower() == ".wav"
                for path in (original, aligned)), "Output WAV escaped the private download directory")
    expected_seconds = sf.info(reference).duration
    measurements = {}
    for name, path in (("original", original), ("aligned", aligned)):
        with sf.SoundFile(path) as audio:
            require(audio.format == "WAV" and audio.channels in (1, 2) and audio.samplerate > 0
                    and 0 < len(audio) / audio.samplerate <= 30, f"Unexpected output WAV layout for {name}")
            samples = audio.read(dtype="float32", always_2d=True)
            require(np.isfinite(samples).all() and bool(np.any(samples)), f"{name} audio is silent or non-finite")
            measurements[name] = {"file": str(path), "sha256": digest(path), "rate": audio.samplerate,
                                  "channels": audio.channels, "seconds": len(samples) / audio.samplerate,
                                  "sample_peak": float(np.max(np.abs(samples))),
                                  "rms": float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))}
    require(abs(measurements["aligned"]["seconds"] - expected_seconds) <= 2 / measurements["aligned"]["rate"],
            "Download alignment did not match the reference length")
    require(measurements["original"]["sha256"] != digest(reference), "Generated output is the exact reference file")
    return aligned, measurements


async def verify_import(client: ClientSession, expected: Path, baseline: list[str], name: str, path: Path) -> dict:
    _, tracks = await stopped_fixture(client, expected)
    require(tracks.count(name) == 1 and [track for track in tracks if track != name] == baseline,
            "Live did not acquire exactly one test-owned track")
    track = await call(client, "get_track", track=name, include_clips=False)
    track_ref = track.get("ref", {}).get("$ref")
    require(bool(track_ref) and track.get("has_audio_input") is True, "Test import did not create an audio track")
    arrangement = await call(client, "arrangement", action="list", track=track_ref)
    items = arrangement.get("items", [])
    require(len(items) == 1 and items[0].get("is_midi") is False, "Expected one Arrangement audio clip")
    clip = await call(client, "get_clip", clip=items[0]["ref"]["$ref"])
    require(Path(clip["file_path"]).resolve() == path and clip.get("is_arrangement_clip") is True
            and abs(clip["start_time"]) <= 1e-6 and clip["end_time"] > clip["start_time"],
            "Imported clip was not read back at the requested position with the generated file")
    return {"track": name, "track_ref": track_ref, "clip_ref": items[0]["ref"]["$ref"],
            "file": str(path), "start_beat": clip["start_time"], "end_beat": clip["end_time"]}


async def clean_import(client: ClientSession, expected: Path, baseline: list[str], name: str,
                       expected_ref: str | None) -> str:
    _, tracks = await stopped_fixture(client, expected)
    if tracks == baseline:
        return "No acceptance track was present."
    require(tracks.count(name) == 1 and [track for track in tracks if track != name] == baseline,
            "Fixture changed concurrently; no tracks were deleted")
    track = await call(client, "get_track", track=name, include_clips=False)
    track_ref = track.get("ref", {}).get("$ref")
    require(bool(track_ref) and (expected_ref is None or track_ref == expected_ref), "Owned track identity changed")
    await call(client, "track_action", action="delete", track=track_ref)
    _, restored = await stopped_fixture(client, expected)
    require(restored == baseline, "The fixture track list did not match its original state")
    return f"Removed only {name} and verified the original track list."


async def run_checks(client: ClientSession, args: argparse.Namespace, report: Report, expected: Path,
                     project: Path, process_temp: Path, downloads: Path, run_dir: Path, url: str, port: int) -> None:
    stage = "fixture"
    receipt = project / "runtime" / "service-process.json"
    owner = None
    before_receipt = None
    baseline: list[str] = []
    import_attempted = False
    imported = None
    start_attempted = False
    token = uuid.uuid4().hex[:12]
    name = f"ZZ ACE Acceptance {token}"
    file_hash = digest(expected)
    try:
        song, baseline = await stopped_fixture(client, expected)
        require(name not in baseline, "The generated acceptance track name is already in use")
        fixture_info = {"file": str(expected), "track_count": len(baseline), "tempo": song["tempo"]}
        if not args.lifecycle_only:
            reference = reference_wav(run_dir / f"reference-{token}.wav", args.fixture_wav, args.bpm)
            fixture_info["reference"] = str(reference)
        report.mark(stage, "PASS", **fixture_info)

        stage = "initial_service_status"
        status = await call(client, "acestep_status", timeout=10)
        require(status.get("ready") is False and status.get("reachable") is False,
                "A loopback ACE-Step service already exists; this run will not use or stop it")
        require(Path(status["output_dir"]).resolve() == downloads
                and Path(status["staging_dir"]).resolve() == (process_temp / "inputs").resolve(),
                "Stdio did not use the isolated download directory or configured deployment input directory")
        require_listener_off(port)
        report.mark(stage, "PASS", url=url, output_dir=str(downloads), staging_dir=status["staging_dir"])

        stage = "service_start"
        before_receipt = digest(receipt) if receipt.is_file() else None
        start_attempted = True
        attempted_at = time.time()
        try:
            started = await call(client, "acestep_service", timeout=args.ready_timeout_s, action="start")
        except TimeoutError as error:
            try:
                candidate = fresh_launch_record(receipt, project, url, before_receipt, attempted_at)
                ready = await wait_ready(client, candidate, min(args.ready_timeout_s, 15))
            except Exception as identity_error:
                raise AcceptanceError(f"MCP start timed out, and launch ownership could not be proven: {identity_error}") from error
            owner = candidate
            report.mark("service_ready", "PASS", model=ready.get("model"), listener_pid=ready.get("server_pid"),
                        evidence="Verified after the MCP start call timed out; cleanup will stop this launch.")
            raise AcceptanceError("MCP start did not return before the deadline despite the verified owned service") from error
        owner = proof_of_fresh_launch(started, receipt, project, url, before_receipt, attempted_at)
        report.mark(stage, "PASS", launcher_pid=owner["pid"], started_utc=owner["started_utc"])

        stage = "service_ready"
        ready = await wait_ready(client, owner, args.ready_timeout_s)
        report.mark(stage, "PASS", model=ready.get("model"), listener_pid=ready.get("server_pid"))
        if args.lifecycle_only:
            return

        stage = "generation_submission"
        task = await submit_once(client, reference, args.bpm, args.key, downloads)
        report.mark(stage, "PASS", task_id=task, reference=str(reference), reference_sha256=digest(reference))

        stage = "result_by_task_id"
        result = await result_by_id(client, task, args.generation_timeout_s)
        report.mark(stage, "PASS", task_id=task)

        stage = "decoded_audio"
        path, measured = check_audio(result, downloads, reference)
        report.mark(stage, "PASS", **measured)

        stage = "live_import"
        _, current_tracks = await stopped_fixture(client, expected)
        require(current_tracks == baseline, "Fixture tracks changed before import")
        import_attempted = True
        response = await call(client, "acestep_result", timeout=30, task_id=task, wait_s=0,
                              import_to="arrangement", track_name=name, start_time=0.0)
        receipts = response.get("imported", [])
        require(response.get("task_id") == task and len(receipts) == 1 and receipts[0].get("track") == name
                and Path(receipts[0]["file"]).resolve() == path, "MCP did not confirm the single fixture import")
        imported = await verify_import(client, expected, baseline, name, path)
        report.mark(stage, "PASS", **imported)
    except Exception as error:
        report.mark(stage, "FAIL", reason=f"{type(error).__name__}: {error}")
    finally:
        if import_attempted:
            try:
                detail = await clean_import(client, expected, baseline, name, imported and imported["track_ref"])
                report.mark("live_cleanup", "PASS", detail=detail)
            except Exception as error:
                report.mark("live_cleanup", "FAIL", reason=str(error), track=name)
        if owner is not None:
            try:
                require(json.loads(receipt.read_text(encoding="utf-8-sig")) == owner,
                        "The deployment's recorded process changed; leaving it untouched")
                stopped = await call(client, "acestep_service", timeout=40, action="stop")
                require(stopped.get("exit_code") == 0, "The deployment did not confirm its own process stopped")
                await wait_for_shutdown(port)
                report.mark("service_stop", "PASS", launcher_pid=owner["pid"], url=url)
            except Exception as error:
                report.mark("service_stop", "FAIL", reason=str(error), launcher_pid=owner["pid"])
        elif start_attempted:
            report.mark("service_stop", "NOT_RUN", reason="No verified fresh launch identity; service left untouched.")
        if digest(expected) == file_hash:
            report.mark("fixture_file_unchanged", "PASS", sha256=file_hash)
        else:
            report.mark("fixture_file_unchanged", "FAIL", reason="The fixture's on-disk checksum changed.")


async def run(args: argparse.Namespace) -> int:
    require(args.start_local_service, "The acceptance requires explicit --start-local-service")
    expected = args.expected_set.resolve(strict=True)
    require(expected.is_file() and expected.suffix.lower() == ".als" and expected.name.upper().startswith("ZZ"),
            "Pass the exact saved disposable ZZ .als fixture")
    require(args.run_dir.is_absolute(), "--run-dir must be absolute and new")
    project = configured_path(args.deployment_project, "ACESTEP_PROJECT")
    process_temp = configured_path(args.process_temp, "ACESTEP_PROCESS_TEMP")
    require(all((project / "scripts" / script).is_file() for script in ("Start-Model.ps1", "Stop-Model.ps1")),
            "Configured ACE-Step deployment is missing its guarded launch scripts")
    url = args.ace_url.rstrip("/")
    parts = urlsplit(url)
    require(parts.scheme == "http" and parts.hostname == "127.0.0.1" and parts.port is not None
            and not any((parts.username, parts.password, parts.path, parts.query, parts.fragment)),
            "ACE-Step must use an explicit IPv4 loopback HTTP port")
    require(url == DEPLOYMENT_URL and args.model == DEPLOYMENT_MODEL,
            "The guarded XL Base deployment uses the pinned loopback port and model")
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    downloads, agent_data, temporary = (run_dir / name for name in ("downloads", "agent-data", "temp"))
    for path in (downloads, agent_data, temporary):
        path.mkdir()
    report = Report(run_dir, args.lifecycle_only)
    environment = dict(os.environ, ACESTEP_URL=url, ACESTEP_MODEL=args.model, ACESTEP_PROJECT=str(project),
                       ACESTEP_PROCESS_TEMP=str(process_temp), ACESTEP_OUTPUT_DIR=str(downloads),
                       AGENT_BRIDGE_HOST="127.0.0.1", AGENT_BRIDGE_PORT=str(args.live_port), AGENT_BRIDGE_TOOLSET="full",
                       AGENT_BRIDGE_DATA_DIR=str(agent_data), TMP=str(temporary), TEMP=str(temporary),
                       TMPDIR=str(temporary), PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    # Use the real MCP registry without the normal CLI's unrelated sample-index warmup.
    command = "from live_agent_bridge.server import mcp; mcp.run(transport='stdio')"
    parameters = StdioServerParameters(command=sys.executable, args=["-B", "-c", command],
                                       env=environment, cwd=str(run_dir))
    try:
        with (run_dir / "stdio.log").open("w", encoding="utf-8") as logs:
            async with stdio_client(parameters, errlog=logs) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    await asyncio.wait_for(client.initialize(), timeout=20)
                    await run_checks(client, args, report, expected, project, process_temp,
                                     downloads, run_dir, url, parts.port)
    except Exception as error:
        report.mark("stdio_session", "FAIL", reason=f"{type(error).__name__}: {error}")
    print(f"Receipt: {report.path}", flush=True)
    return int(not report.passed())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-set", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--start-local-service", action="store_true")
    parser.add_argument("--lifecycle-only", action="store_true", help="Check start, readiness and stop only.")
    parser.add_argument("--deployment-project", type=Path)
    parser.add_argument("--process-temp", type=Path)
    parser.add_argument("--fixture-wav", type=Path, help="Optional local synthetic fixture; defaults to generated audio.")
    parser.add_argument("--bpm", type=int, default=124, choices=range(30, 301), metavar="30..300")
    parser.add_argument("--key", default="A minor")
    parser.add_argument("--ace-url", default=os.environ.get("ACESTEP_URL", DEPLOYMENT_URL))
    parser.add_argument("--model", default=os.environ.get("ACESTEP_MODEL", DEPLOYMENT_MODEL))
    parser.add_argument("--live-port", type=int, default=int(os.environ.get("AGENT_BRIDGE_PORT", "9890")))
    parser.add_argument("--ready-timeout-s", type=int, default=150, choices=range(5, 601), metavar="5..600")
    parser.add_argument("--generation-timeout-s", type=int, default=240, choices=range(5, 601), metavar="5..600")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except (AcceptanceError, OSError, ValueError) as error:
        parser.exit(2, f"REFUSED: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
