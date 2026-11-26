"""Offline edge coverage for the human-facing taste CLI."""

import argparse
import json

import pytest

from live_agent_bridge.taste import cli


def _parser():
    parser = argparse.ArgumentParser(prog="agent-bridge")
    sub = parser.add_subparsers(dest="cmd", required=True)
    cli.add_commands(sub)
    return parser


def _run(argv, capsys):
    args = _parser().parse_args(argv)
    code = cli.run(args)
    out = capsys.readouterr().out
    return code, out


def _ints(value):
    if isinstance(value, bool):
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, dict):
        return [n for v in value.values() for n in _ints(v)]
    if isinstance(value, list):
        return [n for v in value for n in _ints(v)]
    return []


def test_remember_recall_and_filtered_show(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_BRIDGE_DATA_DIR", str(tmp_path))
    code, out = _run(["taste", "remember", "Prefer dry punchy kicks", "--domain", "drums"], capsys)
    assert code == 0
    json.loads(out)
    assert "Prefer dry punchy kicks" in out
    assert "confirmed" in out
    code, out = _run(["taste", "recall", "punchy kicks"], capsys)
    assert code == 0
    json.loads(out)
    assert "Prefer dry punchy kicks" in out
    code, out = _run(["taste", "show", "--domain", "drums"], capsys)
    assert code == 0
    shown = json.loads(out)
    assert any(p["statement"] == "Prefer dry punchy kicks" for p in shown["preferences"])


def test_export_refuses_existing_and_import_restores(tmp_path, monkeypatch, capsys):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("AGENT_BRIDGE_DATA_DIR", str(first))
    _run(["taste", "remember", "Prefer warm tape snares", "--domain", "drums"], capsys)
    target = tmp_path / "out" / "memory.json"
    code, out = _run(["taste", "export", str(target)], capsys)
    assert code == 0
    exported = json.loads(out)
    assert exported["json"].endswith("memory.json")
    assert exported["markdown"].endswith("memory.md")
    assert len(exported["sha256"]) == 64
    assert target.exists() and target.with_suffix(".md").exists()
    _, original_profile = _run(["taste", "show"], capsys)
    before = target.read_bytes()
    with pytest.raises(ValueError):
        _run(["taste", "export", str(target)], capsys)
    assert target.read_bytes() == before
    monkeypatch.setenv("AGENT_BRIDGE_DATA_DIR", str(second))
    code, _ = _run(["taste", "import", str(target)], capsys)
    assert code == 0
    code, out = _run(["taste", "export", str(tmp_path / "again.json")], capsys)
    assert code == 0
    assert json.loads(out)["sha256"] == exported["sha256"]
    _, restored_profile = _run(["taste", "show"], capsys)
    assert json.loads(restored_profile) == json.loads(original_profile)


def test_forget_requires_confirmation_and_scope(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_BRIDGE_DATA_DIR", str(tmp_path))
    _run(["taste", "remember", "Prefer open hats", "--domain", "drums"], capsys)
    with pytest.raises(ValueError):
        _run(["taste", "forget", "--all"], capsys)
    _, out = _run(["taste", "show"], capsys)
    assert "Prefer open hats" in out
    code, out = _run(["taste", "forget", "--all", "--yes"], capsys)
    assert code == 0
    assert sum(_ints(json.loads(out))) > 0
    code, out = _run(["taste", "forget", "--all", "--yes"], capsys)
    assert code == 0
    assert sum(_ints(json.loads(out))) == 0
    with pytest.raises(SystemExit):
        _parser().parse_args(["taste", "forget", "--all", "--source", "user", "--yes"])
    capsys.readouterr()


def test_import_rejects_unexpected_field_without_writes(tmp_path, monkeypatch, capsys):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    monkeypatch.setenv("AGENT_BRIDGE_DATA_DIR", str(source_dir))
    _run(["taste", "remember", "Prefer ghost notes", "--domain", "drums"], capsys)
    exported = tmp_path / "memory.json"
    _run(["taste", "export", str(exported)], capsys)
    data = json.loads(exported.read_text(encoding="utf-8"))
    data["unexpected_field"] = "x"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(data), encoding="ascii")
    monkeypatch.setenv("AGENT_BRIDGE_DATA_DIR", str(target_dir))
    _, before = _run(["taste", "show"], capsys)
    with pytest.raises(ValueError):
        _run(["taste", "import", str(bad)], capsys)
    _, after = _run(["taste", "show"], capsys)
    assert json.loads(after) == json.loads(before)
    assert "Prefer ghost notes" not in after

