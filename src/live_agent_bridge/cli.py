"""Command line: serve (MCP over stdio/HTTP), install, doctor, bench."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import shutil
import statistics
import sys
import time
from pathlib import Path

from .client import DEFAULT_HOST, DEFAULT_PORT, LiveClient, LiveError

SCRIPT_NAME = "AgentBridge"
SCRIPT_SRC = Path(__file__).parent / "remote_script" / SCRIPT_NAME


def default_user_library() -> Path:
    env = os.environ.get("ABLETON_USER_LIBRARY")
    if env:
        return Path(env)
    return Path.home() / "Documents" / "Ableton" / "User Library"


def _tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(x for x in root.iterdir() if x.suffix in (".py", ".json") and x.name != "config.json"):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


async def _reload_running() -> str:
    c = LiveClient()
    try:
        await c.connect()
    except LiveError:
        return "Live is not connected; select AgentBridge as Control Surface (or restart Live) to load it."
    try:
        old = c.hello.get("ops", [])
        if "bridge.reload" not in old:
            return "The running script predates hot reload; re-select AgentBridge in Live once to load it."
        await c.request("bridge.reload")
    finally:
        await c.close()
    for _ in range(40):
        await asyncio.sleep(0.25)
        c = LiveClient()
        try:
            await c.connect()
            st = await c.request("status")
            return f"Reloaded in Live: bridge {st['bridge']}, tick {st['tick_mode']} {st['tick_hz']} Hz."
        except LiveError:
            continue
        finally:
            await c.close()
    return "Reload requested but the bridge did not come back; check `live-agent-bridge doctor`."


def _backup_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "live-agent-bridge" / "backups"
    base.mkdir(parents=True, exist_ok=True)
    return base


def cmd_install(args: argparse.Namespace) -> int:
    lib = Path(args.user_library) if args.user_library else default_user_library()
    if not lib.exists():
        print(f"User Library not found: {lib}\nPass --user-library <path to 'User Library'>.")
        return 1
    dest = lib / "Remote Scripts" / SCRIPT_NAME
    if dest.exists():
        if _tree_hash(dest) == _tree_hash(SCRIPT_SRC):
            print(f"Already up to date: {dest}  ({_tree_hash(dest)})")
            return 0
        # Backups live outside Remote Scripts so Live doesn't list them as surfaces.
        bak = _backup_dir() / f"{SCRIPT_NAME}-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copytree(dest, bak, ignore=shutil.ignore_patterns("__pycache__"))
        print(f"Previous version backed up to {bak}")
    dest.mkdir(parents=True, exist_ok=True)
    # Update in place: keeps the local config.json (timer tuning) and the folder Live has loaded.
    for src in SCRIPT_SRC.iterdir():
        if src.is_file() and src.suffix in (".py", ".json"):
            shutil.copy2(src, dest / src.name)
    shutil.rmtree(dest / "__pycache__", ignore_errors=True)
    print(f"Installed {SCRIPT_NAME} -> {dest}  ({_tree_hash(dest)})")
    if args.no_reload:
        print("Re-select AgentBridge in Live (or restart Live) to load the new version.")
    else:
        print(asyncio.run(_reload_running()))
    return 0


def _live_log() -> Path | None:
    base = Path(os.environ.get("APPDATA", "")) / "Ableton"
    logs = sorted(base.glob("Live */Preferences/Log.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


async def _doctor(host: str, port: int) -> int:
    ok = True
    dest = default_user_library() / "Remote Scripts" / SCRIPT_NAME
    if dest.exists():
        same = _tree_hash(dest) == _tree_hash(SCRIPT_SRC)
        print(f"[{'ok' if same else '!!'}] script installed at {dest}"
              + ("" if same else "  (differs from this package; run `live-agent-bridge install`)"))
        ok &= same
    else:
        print(f"[!!] script not installed ({dest}); run `live-agent-bridge install`")
        ok = False
    c = LiveClient(host, port)
    try:
        await c.connect()
        print(f"[ok] connected to {host}:{port}  hello={c.hello.get('bridge')} live={c.hello.get('live')}")
        st = await c.request("status")
        print(f"[ok] tick mode={st['tick_mode']} rate={st['tick_hz']} Hz  python={st['python']}  "
              f"rtt={c.last_rtt_ms:.1f} ms")
    except LiveError as e:
        print(f"[!!] {e.message}")
        ok = False
    finally:
        await c.close()
    log = _live_log()
    if log:
        lines = [ln.rstrip() for ln in log.read_text(encoding="utf-8", errors="replace").splitlines()
                 if "AgentBridge" in ln]
        print(f"--- {log} (AgentBridge lines, last 10)")
        for ln in lines[-10:]:
            print("   ", ln[-200:])
    return 0 if ok else 1


def _pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))]


async def _bench(host: str, port: int, n: int) -> int:
    c = LiveClient(host, port)
    await c.connect()

    async def timed(op, args=None):
        t = time.perf_counter()
        await c.request(op, args)
        return (time.perf_counter() - t) * 1000

    def report(label, xs):
        print(f"{label:<44} p50 {statistics.median(xs):7.2f} ms   p95 {_pct(xs, .95):7.2f} ms   "
              f"max {max(xs):7.2f} ms")

    report("ping (one tick round trip)", [await timed("ping") for _ in range(n)])
    report("overview (cached snapshot)", [await timed("overview") for _ in range(n)])
    report("lom.get tempo (main thread, sequential)",
           [await timed("lom.get", {"path": "live_set", "props": ["tempo"]}) for _ in range(n)])
    t = time.perf_counter()
    await asyncio.gather(*[c.request("lom.get", {"path": "live_set", "props": ["tempo"]}) for _ in range(n)])
    total = (time.perf_counter() - t) * 1000
    print(f"{n} lom.get pipelined concurrently{'':<14} total {total:7.2f} ms  ({total / n:.2f} ms/op)")
    ops = [{"op": "lom.get", "args": {"path": "live_set", "props": ["tempo"]}}] * n
    t = time.perf_counter()
    await c.request("batch", {"ops": ops, "undo_group": False})
    total = (time.perf_counter() - t) * 1000
    print(f"batch of {n} lom.get{'':<24} total {total:7.2f} ms  ({total / n:.2f} ms/op)")
    await c.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="live-agent-bridge", description="Ableton Live <-> AI agent MCP bridge")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--data-dir", help="shared local memory/cache directory, independent of the model")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="run the MCP server (default)")
    s.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    s.add_argument("--http-port", type=int, default=8765)
    i = sub.add_parser("install", help="copy the Remote Script into Live's User Library")
    i.add_argument("--user-library", help="path to the 'User Library' folder")
    i.add_argument("--no-reload", action="store_true", help="don't hot-reload the script in a running Live")
    sub.add_parser("doctor", help="check installation and connection")
    b = sub.add_parser("bench", help="measure latency against the running Live")
    b.add_argument("-n", type=int, default=50)
    from .taste.cli import add_commands
    add_commands(sub)
    args = p.parse_args(argv)

    os.environ["AGENT_BRIDGE_HOST"] = args.host
    os.environ["AGENT_BRIDGE_PORT"] = str(args.port)
    if args.data_dir:
        os.environ["AGENT_BRIDGE_DATA_DIR"] = args.data_dir
    if args.cmd in ("reference", "taste", "taste-serve", "samples"):
        from .taste.cli import run
        try:
            return run(args)
        except (ValueError, OSError, TimeoutError) as error:
            print(str(error), file=sys.stderr)
            return 1
    if args.cmd == "install":
        return cmd_install(args)
    if args.cmd == "doctor":
        return asyncio.run(_doctor(args.host, args.port))
    if args.cmd == "bench":
        return asyncio.run(_bench(args.host, args.port, args.n))
    from .server import mcp, warmup
    warmup()
    transport = getattr(args, "transport", "stdio")
    if transport != "stdio":
        mcp.settings.port = args.http_port
    mcp.run(transport=transport)
    return 0


if __name__ == "__main__":
    sys.exit(main())
