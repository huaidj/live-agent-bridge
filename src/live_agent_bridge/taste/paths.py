from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    configured = os.environ.get("AGENT_BRIDGE_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        # Preserve an existing development checkout's separate state without
        # assuming a drive or storing new data in a legacy location.
        checkout = Path(__file__).resolve().parents[3]
        previous_state = checkout.parent.parent / "State" / "live-agent-bridge"
        if (checkout / "pyproject.toml").is_file() and previous_state.is_dir():
            return previous_state
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "live-agent-bridge"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "live-agent-bridge"
