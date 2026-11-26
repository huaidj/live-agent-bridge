"""Offline tests for plug-in role resolution and the learned-knowledge lifecycle."""

from __future__ import annotations

import pytest

from live_agent_bridge.plugins import learn, load_knowledge, resolve_role, understand


def _inspect(plugin: str, exposed: list[str], unexposed: list[str] | None = None) -> dict:
    return {
        "plugin_name": plugin,
        "exposed_parameters": [{"name": name} for name in exposed],
        "unexposed_parameter_names": list(unexposed or []),
    }


def test_resolve_role_prefers_confirmed_meaning_and_hints_hidden(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_DATA_DIR", str(tmp_path))

    # Detection alone picks the keyword match.
    assert resolve_role(_inspect("Synth One", ["Filter 1 Cutoff", "Macro 1"]), "cutoff") == "Filter 1 Cutoff"

    # A user-confirmed meaning wins over detection.
    learn("Synth One", "Macro 1", "cutoff", "user says this is the cutoff", "user")
    assert resolve_role(_inspect("Synth One", ["Filter 1 Cutoff", "Macro 1"]), "cutoff") == "Macro 1"

    # A role only found on an unexposed parameter yields a configure hint.
    with pytest.raises(ValueError, match="configure one of") as excinfo:
        resolve_role(_inspect("Synth One", ["Filter 1 Cutoff"], ["Filter 1 Resonance"]), "resonance")
    assert "Filter 1 Resonance" in str(excinfo.value)


def test_learn_inference_never_overwrites_confirmed(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_DATA_DIR", str(tmp_path))

    first = learn("Synth One", "Filter 1 Cutoff", "cutoff", "confirmed by user", "user")
    assert first["status"] == "confirmed"

    result = learn("Synth One", "Filter 1 Cutoff", "drive", "agent guess", "inference")
    assert set(result) == {"kept", "ignored", "reason"}
    assert result["kept"]["role"] == "cutoff"
    assert result["kept"]["status"] == "confirmed"
    assert result["ignored"]["role"] == "drive"
    assert result["ignored"]["status"] == "pending"
    assert "never overwritten" in result["reason"]

    stored = load_knowledge()["Synth One"]["Filter 1 Cutoff"]
    assert stored["role"] == "cutoff"
    assert stored["status"] == "confirmed"
    assert stored["origin"] == "user"


def test_knowledge_scoped_per_plugin_persists_and_truncates(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_DATA_DIR", str(tmp_path))

    long_note = "x" * 600
    entry = learn("Synth One", "Filter 1 Cutoff", "cutoff", long_note, "user")
    assert len(entry["note"]) == 500
    learn("Synth Two", "Filter 1 Cutoff", "drive", "short note", "user")

    knowledge = load_knowledge()
    assert knowledge["Synth One"]["Filter 1 Cutoff"]["role"] == "cutoff"
    assert len(knowledge["Synth One"]["Filter 1 Cutoff"]["note"]) == 500
    assert knowledge["Synth Two"]["Filter 1 Cutoff"]["role"] == "drive"
    assert "Synth Three" not in knowledge

    one = understand(_inspect("Synth One", ["Filter 1 Cutoff"]))
    two = understand(_inspect("Synth Two", ["Filter 1 Cutoff"]))
    three = understand(_inspect("Synth Three", ["Filter 1 Cutoff"]))
    assert one["exposed_parameters"][0]["role"] == "cutoff"
    assert two["exposed_parameters"][0]["role"] == "drive"
    assert three["exposed_parameters"][0]["role"] == "cutoff"
    assert "learned" not in three["exposed_parameters"][0]
    assert one["understanding"]["learned_parameters"] == 1
    assert three["understanding"]["learned_parameters"] == 0
