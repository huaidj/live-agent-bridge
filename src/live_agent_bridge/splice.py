"""Splice integration status, read from local files only.

Live 12's built-in Splice browser is a plug-in talking to Splice's private,
authenticated API; it exposes nothing to the Live API, and Splice has no public
account API. So this module never touches credentials (``info.cache`` is
encrypted and left alone) and never calls Splice servers. It infers state from:

* the installed plug-in and its onboarding / EULA markers,
* the integration's own log (signed-in user seen, premium downloads that
  succeeded, connection errors),
* samples already downloaded to disk (indexed for search and loading).

Subscription status is therefore a *heuristic*, reported with its evidence.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

AUDIO_EXT = {".wav", ".aif", ".aiff", ".flac", ".mp3", ".ogg"}
_TS = re.compile(r"^\[(\d\d/\d\d/\d\d \d\d:\d\d:\d\d)")


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))


def _localappdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))


def sample_roots() -> list[tuple[str, str]]:
    """(path, label) of folders where Splice puts downloaded samples."""
    roots = []
    for p in (Path.home() / "Documents" / "Splice" / "Samples", Path.home() / "Splice" / "Samples"):
        if p.is_dir():
            roots.append((str(p), "Splice"))
    for p in sorted((_localappdata() / "Ableton" / "Splice").glob("*/Download")):
        if p.is_dir():
            roots.append((str(p), "Splice in Live"))
    return roots


def _ts(line: str) -> datetime | None:
    m = _TS.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%m/%d/%y %H:%M:%S")
    except ValueError:
        return None


def _scan_logs(log_dir: Path) -> dict[str, Any]:
    out = {"signed_in_last_seen": None, "premium_downloads": 0, "premium_download_last": None,
           "fetch_errors_24h": 0, "last_activity": None}
    files = sorted(log_dir.glob("log*.txt"), key=lambda p: p.stat().st_mtime)
    now = datetime.now()
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            t = _ts(line)
            if t is None:
                continue
            out["last_activity"] = max(filter(None, [out["last_activity"], t]))
            if "Subscribing to pubnub events for user" in line:
                out["signed_in_last_seen"] = max(filter(None, [out["signed_in_last_seen"], t]))
            elif "premium_sample_dl" in line:
                out["premium_downloads"] += 1
                out["premium_download_last"] = max(filter(None, [out["premium_download_last"], t]))
            elif "Failed to load audio resource" in line and now - t < timedelta(hours=24):
                out["fetch_errors_24h"] += 1
    return out


def status() -> dict[str, Any]:
    base = _appdata() / "Splice" / "Ableton Live"
    plugin = sorted((_localappdata() / "Ableton" / "Splice").glob("*/SpliceAbletonLive.vst3"))
    state = base / "local-state"
    res: dict[str, Any] = {
        "integration_installed": bool(plugin),
        "plugin": str(plugin[-1]) if plugin else None,
        "eula_agreed": (state / "ableton_live_eula_agreed_v0").exists(),
        "onboarding_completed": (state / "ableton_live_integration_onboarding_completed").exists(),
        "splice_desktop_app_folder": (Path.home() / "Documents" / "Splice").is_dir(),
    }
    logs = _scan_logs(base / "logs") if (base / "logs").is_dir() else {}
    downloaded = 0
    for root, _label in sample_roots():
        for _dirpath, _dirs, files in os.walk(root):
            downloaded += sum(1 for f in files if os.path.splitext(f)[1].lower() in AUDIO_EXT)
    res["downloaded_samples_on_disk"] = downloaded
    res["sample_folders"] = [r for r, _ in sample_roots()]

    now = datetime.now()
    signed = logs.get("signed_in_last_seen")
    dl_last = logs.get("premium_download_last")
    if not res["integration_installed"]:
        sub, why = "unavailable", "Splice integration is not installed in this Live"
    elif signed is None:
        sub, why = "not_signed_in", "no signed-in Splice user in the integration log"
    elif dl_last is not None and now - dl_last < timedelta(days=35):
        sub, why = "likely_active", f"premium sample downloads succeeded, last on {dl_last:%Y-%m-%d}"
    elif dl_last is not None:
        sub, why = "possibly_lapsed", f"signed in, but the last premium download was {dl_last:%Y-%m-%d}"
    else:
        sub, why = "signed_in_plan_unknown", "signed in; no premium download recorded yet"
    res["subscription"] = {
        "status": sub, "evidence": why, "authoritative": False,
        "note": "Inferred from local logs; Splice offers no public account API. Check the Splice panel "
                "in Live's browser for the exact plan and credits.",
    }
    res["activity"] = {k: (v.isoformat(sep=" ") if isinstance(v, datetime) else v) for k, v in logs.items()}
    res["capabilities"] = {
        "search_and_load_downloaded_samples": True,
        "search_splice_catalog_or_spend_credits": False,
        "official_mcp_url": "https://mcp.splice.com/mcp",
        "official_mcp_integration": "separate host connection; not authenticated or controlled by this bridge",
        "why_not": "This bridge only handles local downloads. Use Splice's official MCP through your host, "
                   "or the Splice panel in Live, for catalog search. Authorize each credit-spending download.",
    }
    return res
