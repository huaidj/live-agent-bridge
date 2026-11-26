"""Explicit local inference runtime selection; no package or model auto-download."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ..taste.paths import data_dir


def settings() -> dict:
    path = data_dir() / "audio-model.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def model_directory() -> Path:
    value = os.environ.get("AGENT_BRIDGE_CLAP_MODEL") or settings().get("model_directory")
    if not value:
        raise ValueError("Configure an existing local CLAP model directory with taste model-config or AGENT_BRIDGE_CLAP_MODEL")
    return Path(value).resolve(strict=True)


def python_executable() -> str:
    value = os.environ.get("AGENT_BRIDGE_AUDIO_PYTHON") or settings().get("python") or sys.executable
    path = Path(value).resolve(strict=True)
    if not path.is_file() or not path.name.lower().startswith("python"):
        raise ValueError("Configure a local Python inference executable")
    return str(path)


def configure(model: str, python: str | None = None) -> dict:
    directory = Path(model).resolve(strict=True)
    if not (directory / "config.json").is_file() or not (directory / "verification.json").is_file():
        raise ValueError("Choose the prepared and hash-verified local CLAP directory")
    executable = Path(python or sys.executable).resolve(strict=True)
    document = {"model_directory": str(directory), "python": str(executable), "device": "cpu"}
    target = data_dir() / "audio-model.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return document
