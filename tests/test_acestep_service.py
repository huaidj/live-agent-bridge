"""The Windows deployment launcher runs without inheriting MCP protocol streams."""

import os
import subprocess
from types import SimpleNamespace

from live_agent_bridge import acestep


def test_service_launcher_uses_noninteractive_input_and_file_backed_output(tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "Start-Model.ps1").touch()
    monkeypatch.setattr(acestep, "PROJECT", tmp_path)
    states = iter(({"ready": False}, {"ready": True, "model_ok": True}))
    monkeypatch.setattr(acestep, "ready", lambda: next(states))

    def run(command, **options):
        assert "-NonInteractive" in command and command[-1] == str(scripts / "Start-Model.ps1")
        assert options["stdin"] == subprocess.DEVNULL
        assert options["stderr"] == subprocess.STDOUT
        assert options["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert isinstance(options["stdout"].fileno(), int)
        os.write(options["stdout"].fileno(), b"ACE service launched\n")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(acestep.subprocess, "run", run)
    result = acestep.service("start")
    assert result["exit_code"] == 0 and result["status"]["ready"]
    assert result["output"] == "ACE service launched\n"


def test_readiness_after_service_disconnect_is_unavailable(monkeypatch):
    def disconnected(*args, **kwargs):
        raise ConnectionResetError(10054, "service shut down during readiness check")

    monkeypatch.setattr(acestep, "_HTTP", SimpleNamespace(open=disconnected))
    status = acestep.ready()
    assert status["ready"] is False and status["reachable"] is False
    assert "connection lost" in status["error"]
