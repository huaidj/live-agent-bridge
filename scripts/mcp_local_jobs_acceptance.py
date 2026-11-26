"""Exercise real local audio jobs over stdio with no Live connection or library rescan.

Use the bridge virtualenv's Python. The run directory must be an unused absolute path.
Pass --clap-model and, if needed, --audio-python to test reference analysis with an
already installed, offline CLAP model. Checks and their statuses are saved as acceptance.json.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ACTIVE = {"queued", "running", "cancelling"}
FILE_LIMIT = 64 * 1024 * 1024


class AcceptanceError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceError(message)


def checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def child_environment(run_dir: Path, model: Path | None, audio_python: Path) -> dict[str, str]:
    paths = {
        "AGENT_BRIDGE_DATA_DIR": run_dir / "agent-data",
        "APPDATA": run_dir / "appdata",
        "LOCALAPPDATA": run_dir / "localappdata",
        "TMP": run_dir / "temp",
        "TEMP": run_dir / "temp",
        "TMPDIR": run_dir / "temp",
        "HF_HOME": run_dir / "cache" / "huggingface",
        "TORCH_HOME": run_dir / "cache" / "torch",
        "NUMBA_CACHE_DIR": run_dir / "cache" / "numba",
        "MPLCONFIGDIR": run_dir / "cache" / "matplotlib",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
        require(path.resolve(strict=True).is_relative_to(run_dir), f"Directory escaped the run: {path}")
    env = dict(os.environ)
    for name in ("AGENT_BRIDGE_CLAP_MODEL", "AGENT_BRIDGE_AUDIO_PYTHON", "TRANSFORMERS_CACHE"):
        env.pop(name, None)
    env.update({name: str(path) for name, path in paths.items()})
    env.update(
        PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
        HF_HUB_DISABLE_IMPLICIT_TOKEN="1", HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false",
        OMP_NUM_THREADS="2", OPENBLAS_NUM_THREADS="2", MKL_NUM_THREADS="2",
    )
    if model is not None:
        env.update(AGENT_BRIDGE_CLAP_MODEL=str(model), AGENT_BRIDGE_AUDIO_PYTHON=str(audio_python))
    return env


def serve_fixture(run_dir: Path) -> None:
    from mcp.server.fastmcp import FastMCP

    from live_agent_bridge.samples import SampleIndex
    from live_agent_bridge.taste.paths import data_dir
    from live_agent_bridge.taste.tools import register

    require(Path.cwd().resolve() == run_dir, "Unexpected stdio working directory")
    require(data_dir() == run_dir / "agent-data", "Agent data escaped the fixture")
    expected_temp = run_dir / "temp"
    require(Path(tempfile.gettempdir()).resolve() == expected_temp, "Worker TEMP escaped the fixture")
    require(all(Path(os.environ[name]).resolve() == expected_temp for name in ("TEMP", "TMP", "TMPDIR")),
            "Worker temp environment disagrees with the fixture")
    index = SampleIndex()
    require(Path(index.db_path).resolve() == data_dir() / "samples.db", "Unexpected sample index")
    # add_root registers the fixture privately, without triggering the production library scan.
    index.add_root(str(run_dir / "fixture"))

    async def fixture_index() -> SampleIndex:
        return index

    server = FastMCP("local-audio-fixture")
    register(server, index_getter=fixture_index)
    server.run(transport="stdio")


async def call(client: ClientSession, tool: str, **arguments) -> dict:
    response = await asyncio.wait_for(client.call_tool(tool, arguments), timeout=25)
    message = "".join(item.text for item in response.content if item.type == "text")
    if response.isError:
        raise AcceptanceError(f"{tool} {arguments['action']}: {message}")
    data = response.structuredContent or json.loads(message)
    require(isinstance(data, dict), f"Malformed {tool} result")
    return data


async def wait_for_terminal(client: ClientSession, tool: str, job: str, timeout: int) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        status = await call(client, tool, action="status", job_id=job)
        require(status.get("id") == job, f"{tool} returned a different job identity")
        if status.get("state") not in ACTIVE:
            return status
        await asyncio.sleep(0.2)
    raise AcceptanceError(f"Timed out waiting for {tool} job {job}")


class Checks:
    def __init__(self, timeout: int):
        self.timeout = timeout
        self.rows: list[dict] = []
        self.jobs: set[tuple[str, str]] = set()

    def record(self, name: str, status: str, **evidence) -> None:
        self.rows.append({"check": name, "status": status, **evidence})
        print(f"{status:7} {name}", flush=True)

    async def start(self, client: ClientSession, tool: str, **arguments) -> str:
        status = await call(client, tool, **arguments)
        job = status.get("id")
        require(isinstance(job, str) and bool(job), f"{tool} did not start a real job")
        require(status.get("state") in ACTIVE | {"complete"}, f"{tool} failed at start: {status}")
        self.jobs.add((tool, job))
        return job

    async def cancel(self, client: ClientSession, tool: str, job: str) -> dict:
        await call(client, tool, action="cancel", job_id=job)
        return await wait_for_terminal(client, tool, job, self.timeout)

    async def cleanup(self, client: ClientSession) -> None:
        for tool, job in sorted(self.jobs):
            try:
                status = await call(client, tool, action="status", job_id=job)
                if status.get("state") in ACTIVE:
                    await call(client, tool, action="cancel", job_id=job)
                    await wait_for_terminal(client, tool, job, min(self.timeout, 12))
            except Exception as error:  # Preserve the original result; report any remaining worker separately.
                self.record("job cleanup", "FAIL", tool=tool, job_id=job, reason=str(error))


async def sample_checks(client: ClientSession, checks: Checks, fixture: Path, expected_hash: str) -> None:
    tool = "sample_analyze"
    start = {"action": "start", "path": str(fixture), "limit": 1, "semantic": False}
    job = await checks.start(client, tool, **start)
    status = await wait_for_terminal(client, tool, job, checks.timeout)
    require(status.get("state") == "complete" and status.get("counts") == {"done": 1},
            f"Sample job did not finish cleanly: {status}")
    result = await call(client, tool, action="result", path=str(fixture))
    require(result.get("sha256") == expected_hash and result.get("sample_rate", 0) > 0
            and result.get("analyzed_seconds", 0) > 0 and math.isfinite(result.get("rms_dbfs", math.nan)),
            f"Sample result did not identify real decoded fixture audio: {result}")
    checks.record("sample start/status/result", "PASS", job_id=job, sha256=expected_hash,
                  sample_rate=result["sample_rate"], rms_dbfs=result["rms_dbfs"], bpm=result.get("bpm"))

    job = await checks.start(client, tool, **start)
    cancelled = await checks.cancel(client, tool, job)
    if cancelled.get("state") == "complete":
        checks.record("sample cancel/resume", "NOT_RUN", job_id=job, reason="The worker finished before cancellation.")
        return
    require(cancelled.get("state") == "cancelled", f"Sample cancel did not reach cancelled: {cancelled}")
    checks.record("sample cancel", "PASS", job_id=job, queue_counts=cancelled.get("counts"))
    if cancelled.get("counts") == {"done": 1}:
        checks.record("sample resume", "NOT_RUN", job_id=job, reason="Cancellation left no unfinished queue item.")
        return
    resumed = await call(client, tool, action="resume", job_id=job, semantic=False)
    require(resumed.get("id") == job, "Resume did not retain the original sample job identity")
    status = await wait_for_terminal(client, tool, job, checks.timeout)
    require(status.get("state") == "complete" and status.get("counts") == {"done": 1},
            f"Resumed sample job did not complete its queue: {status}")
    again = await call(client, tool, action="result", path=str(fixture))
    require(again.get("sha256") == expected_hash, "Resumed sample result did not match the fixture")
    checks.record("sample resume", "PASS", job_id=job, queue_counts=status["counts"])


async def reference_checks(client: ClientSession, checks: Checks, fixture: Path, expected_hash: str) -> None:
    tool = "music_reference"
    start = {"action": "analyze", "path": str(fixture), "label": "Disposable WAV fixture", "remember": False}
    job = await checks.start(client, tool, **start)
    status = await wait_for_terminal(client, tool, job, checks.timeout)
    require(status.get("state") == "complete", f"Reference job did not complete: {status}")
    result = await call(client, tool, action="result", job_id=job)
    analysis = result.get("analysis") or {}
    require(analysis.get("sha256") == expected_hash and analysis.get("original_unchanged") is True
            and analysis.get("audio_uploaded") is False and bool(analysis.get("model"))
            and bool(analysis.get("segments")), f"Reference result did not identify local fixture audio: {analysis}")
    listed = await call(client, tool, action="list")
    require(any(row.get("id") == job for row in listed.get("results", [])), "Completed job was absent from list")
    checks.record("reference start/status/result/list", "PASS", job_id=job, sha256=expected_hash,
                  segments=len(analysis["segments"]), model=analysis["model"],
                  bpm=analysis.get("measurements", {}).get("bpm"))
    cancel_job = await checks.start(client, tool, **start)
    require(cancel_job != job, "Reference cancellation reused the completed job identity")
    cancelled = await checks.cancel(client, tool, cancel_job)
    if cancelled.get("state") == "complete":
        checks.record("reference cancel", "NOT_RUN", job_id=cancel_job,
                      reason="The worker finished before cancellation.")
        return
    require(cancelled.get("state") == "cancelled", f"Reference cancel did not reach cancelled: {cancelled}")
    result = await call(client, tool, action="result", job_id=cancel_job)
    require(result.get("analysis") is None, "Cancelled reference unexpectedly returned an analysis")
    checks.record("reference cancel", "PASS", job_id=cancel_job)


def verify_private_databases(run_dir: Path, fixture: Path, has_reference: bool) -> None:
    data = run_dir / "agent-data"
    sample_db = data / "samples.db"
    with closing(sqlite3.connect(sample_db.as_uri() + "?mode=ro", uri=True)) as db:
        paths = db.execute("SELECT DISTINCT path FROM audio_queue").fetchall()
        roots = db.execute("SELECT path FROM extra_roots").fetchall()
    require(paths == [(str(fixture),)] and roots == [(str(fixture.parent),)],
            "Private sample database contains other paths")
    if has_reference:
        with closing(sqlite3.connect((data / "references.db").as_uri() + "?mode=ro", uri=True)) as db:
            references = db.execute("SELECT DISTINCT path,remember FROM reference_jobs").fetchall()
        require(references == [(str(fixture), 0)], "Reference job used another source or enabled preference writes")
    require(not (data / "taste.db").exists(), "A preference database was unexpectedly created")


async def run(args: argparse.Namespace) -> int:
    original = args.fixture_wav.resolve(strict=True)
    require(original.is_file() and original.suffix.lower() == ".wav" and 0 < original.stat().st_size <= FILE_LIMIT,
            "Use one existing WAV fixture no larger than 64 MiB")
    require(args.run_dir.is_absolute(), "--run-dir must be absolute")
    run_dir = args.run_dir.resolve()
    model = args.clap_model.resolve(strict=True) if args.clap_model else None
    if model is not None:
        require((model / "config.json").is_file() and (model / "verification.json").is_file()
                and ((model / "pytorch_model.bin").is_file() or any(model.glob("*.safetensors"))),
                "Use an already-installed, verified local CLAP model directory")
    audio_python = (args.audio_python or Path(sys.executable)).resolve(strict=True)
    require(audio_python.is_file() and audio_python.name.lower().startswith("python"), "Invalid --audio-python")
    run_dir.mkdir(parents=True, exist_ok=False)
    fixture = run_dir / "fixture" / "analysis.wav"
    fixture.parent.mkdir()
    source_hash = checksum(original)
    shutil.copyfile(original, fixture)
    require(checksum(fixture) == source_hash, "Fixture copy checksum changed")

    checks = Checks(args.timeout_s)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-B", str(Path(__file__).resolve()), "--serve-fixture", str(run_dir)],
        env=child_environment(run_dir, model, audio_python),
        cwd=str(run_dir),
    )
    try:
        with (run_dir / "stdio.log").open("w", encoding="utf-8") as logs:
            async with stdio_client(parameters, errlog=logs) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    await asyncio.wait_for(client.initialize(), timeout=25)
                    inventory = await asyncio.wait_for(client.list_tools(), timeout=25)
                    names = {item.name for item in inventory.tools}
                    require({"sample_analyze", "music_reference"} <= names and "transport" not in names,
                            "Unexpected stdio surface for the non-Live fixture")
                    try:
                        await sample_checks(client, checks, fixture, source_hash)
                        if model is None:
                            checks.record("reference start/status/result/cancel", "NOT_RUN",
                                          reason="No already-installed CLAP directory was supplied.")
                        else:
                            await reference_checks(client, checks, fixture, source_hash)
                    finally:
                        await checks.cleanup(client)
        verify_private_databases(run_dir, fixture, model is not None)
        require(checksum(original) == source_hash and checksum(fixture) == source_hash, "Input WAV changed")
        checks.record("private databases and unchanged input", "PASS", agent_data=str(run_dir / "agent-data"),
                      temp=str(run_dir / "temp"), fixture=str(fixture))
    except Exception as error:
        checks.record("local jobs acceptance", "FAIL", reason=f"{type(error).__name__}: {error}")
    report = {"run_dir": str(run_dir), "checks": checks.rows}
    receipt = run_dir / "acceptance.json"
    receipt.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Receipt: {receipt}", flush=True)
    return int(any(row["status"] == "FAIL" for row in checks.rows))


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--serve-fixture":
        serve_fixture(Path(sys.argv[2]).resolve(strict=True))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-wav", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--clap-model", type=Path)
    parser.add_argument("--audio-python", type=Path)
    parser.add_argument("--timeout-s", type=int, default=120, choices=range(5, 601), metavar="5..600")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
