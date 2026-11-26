"""Run the one-command MCP acceptance on a COPY of the user's set, then reopen the original (PLAN MCP-03).

Steps: refuse unless the open set is saved (Live's window title has no '*') -> copy SET.als next to itself as
"<name> ZZ-acceptance-copy.als" (same folder, so relative sample paths still resolve) -> open the copy in the
running Live -> wait until Live shows it and the bridge answers -> scripts/mcp_acceptance.py -> reopen the
original -> delete the copy. The original .als is never written.

Usage (Windows, Live already running):
  .venv\\Scripts\\python.exe scripts\\acceptance_on_copy.py "F:\\path\\to\\Set.als" [--keep-copy] [--json PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COPY_SUFFIX = " ZZ-acceptance-copy"


def live_title() -> str | None:
    """Main window title of Ableton Live, e.g. 'ai1* - Ableton Live 12 Suite' (None when Live is not running)."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-Process | Where-Object { $_.MainWindowTitle -like '*Ableton Live*' } | "
         "Select-Object -First 1).MainWindowTitle"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
    title = out.stdout.strip()
    return title or None


def parse_title(title: str | None) -> tuple[str | None, bool]:
    """('ai1', unsaved?) from a Live window title; (None, False) when unknown."""
    if not title or " - Ableton Live" not in title:
        return None, False
    name = title.split(" - Ableton Live", 1)[0].strip()
    unsaved = name.endswith("*")
    return name.rstrip("*").strip(), unsaved


def copy_path(original: Path) -> Path:
    return original.with_name(original.stem + COPY_SUFFIX + original.suffix)


def preflight(original: Path, title: str | None, allow_dirty: bool = False) -> str | None:
    """Reason to refuse, or None when it is safe to switch sets. ``allow_dirty`` is for the case where the caller has
    VERIFIED that the open set equals the saved file (e.g. snapshot diff = 0) and only the '*' flag is set."""
    if not original.is_file() or original.suffix.lower() != ".als":
        return f"not an .als file: {original}"
    name, unsaved = parse_title(title)
    if name is None:
        return "Ableton Live is not running (or its window title is unreadable)"
    if unsaved and not allow_dirty:
        return (f"the open set '{name}' has unsaved changes (title shows '*'); save it in Live first - "
                "opening another set would prompt or discard them")
    if name != original.stem:
        return f"Live has '{name}' open, not '{original.stem}'"
    return None


def wait_for_set(stem: str, timeout: float = 120.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        name, _ = parse_title(live_title())
        if name == stem:
            return True
        time.sleep(1.0)
    return False


async def bridge_ready(timeout: float = 60.0) -> bool:
    from live_agent_bridge.client import LiveClient

    end = time.time() + timeout
    while time.time() < end:
        c = LiveClient()
        try:
            await c.connect()
            await c.request("overview", {"fresh": True}, timeout=10)
            await c.close()
            return True
        except Exception:  # noqa: BLE001 - Live is still loading the set
            await asyncio.sleep(1.0)
    return False


async def dismiss_save_dialog(timeout: float = 25.0) -> str:
    """After the copy was modified by the suite, opening another set makes Live ask "Save changes to <copy>?".
    Live's API gives the dialog text and the button COUNT but no labels. The dialog only concerns the disposable copy,
    so either answer is harmless; press button 1 (Don't Save in Live's usual Save / Don't Save / Cancel order) and
    report what was seen."""
    from live_agent_bridge.client import LiveClient

    end = time.time() + timeout
    while time.time() < end:
        c = LiveClient()
        try:
            await c.connect()
            info = await c.request("app", {"action": "info"}, timeout=8)
            if info.get("open_dialog_count") and "Save changes" in str(info.get("current_dialog_message")):
                msg = info["current_dialog_message"]
                try:
                    await c.request("app", {"action": "press_dialog_button", "button": 1}, timeout=8)
                except Exception:  # noqa: BLE001 - the connection drops while Live switches sets
                    pass
                await c.close()
                return "answered dialog %r with button 1 (of %s)" % (msg, info.get("current_dialog_button_count"))
            await c.close()
        except Exception:  # noqa: BLE001
            await asyncio.sleep(0.5)
        await asyncio.sleep(0.5)
    return "no save dialog appeared"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("set", type=Path)
    ap.add_argument("--keep-copy", action="store_true")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="the open set shows '*' but was VERIFIED equal to the saved file; Live's save prompt is "
                         "answered automatically")
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--script", type=Path, default=None,
                    help="run this script on the copy instead of mcp_acceptance.py (extra args after --)")
    a, extra = ap.parse_known_args()
    extra = [x for x in extra if x != "--"]
    original = a.set.resolve()
    reason = preflight(original, live_title(), a.allow_dirty)
    if reason:
        print("REFUSED:", reason)
        return 2
    dup = copy_path(original)
    shutil.copy2(original, dup)
    print("copy:", dup)
    code = 1
    try:
        os.startfile(str(dup))  # handed to the running Live
        if a.allow_dirty:
            print("save dialog (original):", asyncio.run(dismiss_save_dialog()))
        if not wait_for_set(dup.stem) or not asyncio.run(bridge_ready()):
            print("FAILED: Live did not open the copy in time")
            return 1
        if a.script:
            cmd = [sys.executable, str(a.script), *extra]
            if a.json:
                cmd += ["--json", str(a.json)]
        else:
            cmd = [sys.executable, str(ROOT / "scripts" / "mcp_acceptance.py")]
            if a.json:
                cmd += ["--json", str(a.json)]
        env = dict(os.environ, ZZ_TEST_NEW_ACTIONS="1", ZZ_TEST_FIXTURE_FILE=str(dup))
        code = subprocess.run(cmd, cwd=str(ROOT), env=env).returncode
    finally:
        name, _ = parse_title(live_title())
        if name != original.stem:
            os.startfile(str(original))
            print("save dialog:", asyncio.run(dismiss_save_dialog()))
            reopened = wait_for_set(original.stem)
            print("original reopened:", reopened)
        if not a.keep_copy and parse_title(live_title())[0] == original.stem:
            dup.unlink(missing_ok=True)
            print("copy removed")
    return code


if __name__ == "__main__":
    sys.exit(main())
