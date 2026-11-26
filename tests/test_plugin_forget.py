"""plugin_learn action='forget': agents can undo their own pending guesses, never a user's meaning."""

import json


def test_forget_pending_inference_leaves_store_clean(tmp_path, monkeypatch):
    from live_agent_bridge import plugins as P

    monkeypatch.setenv("AGENT_BRIDGE_DATA_DIR", str(tmp_path))
    P.learn("Synth", "Filter 1 Cutoff", "drive", "agent guess", "inference")
    P.learn("Synth", "Osc 1 Level", "volume", "keep me", "inference")
    res = P.forget("Synth", "Filter 1 Cutoff", "inference")
    assert res["deleted"] is True and res["removed"]["status"] == "pending"
    assert P.load_knowledge() == {"Synth": {"Osc 1 Level": P.load_knowledge()["Synth"]["Osc 1 Level"]}}
    P.forget("Synth", "Osc 1 Level", "inference")
    # empty plug-in entries are dropped, so a learn/forget round trip leaves no residue
    assert P.load_knowledge() == {}


def test_inference_cannot_forget_user_confirmed(tmp_path, monkeypatch):
    from live_agent_bridge import plugins as P

    monkeypatch.setenv("AGENT_BRIDGE_DATA_DIR", str(tmp_path))
    P.learn("Synth", "Filter 1 Cutoff", "drive", "user says drive", "user")
    res = P.forget("Synth", "Filter 1 Cutoff", "inference")
    assert res["deleted"] is False and res["kept"]["status"] == "confirmed"
    assert P.load_knowledge()["Synth"]["Filter 1 Cutoff"]["role"] == "drive"
    assert P.forget("Synth", "Filter 1 Cutoff", "user")["deleted"] is True
    assert P.load_knowledge() == {}


def test_forget_missing_is_a_noop(tmp_path, monkeypatch):
    from live_agent_bridge import plugins as P

    monkeypatch.setenv("AGENT_BRIDGE_DATA_DIR", str(tmp_path))
    assert P.forget("Nope", "X", "inference")["deleted"] is False
    P.learn("Synth", "A", None, "", "inference")
    before = (tmp_path / "plugin_knowledge.json").read_text(encoding="utf-8")
    assert P.forget("Synth", "B", "inference")["deleted"] is False
    assert json.loads((tmp_path / "plugin_knowledge.json").read_text(encoding="utf-8")) == json.loads(before)
