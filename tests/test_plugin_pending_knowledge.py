"""Regression: a pending agent inference must not override detected roles.

plugins.py documents that user-taught knowledge wins over detection while agent guesses
stay pending until the user confirms them. resolve_role already ignores pending entries on
its learned-first path, so understand() must not promote a pending inference into
controllable_roles either.
"""

import json


def _inspect():
    return json.loads(json.dumps({
        "plugin_name": "Synth",
        "exposed_parameters": [{"name": "Filter 1 Cutoff"}],
        "unexposed_parameter_names": [],
    }))


def test_pending_inference_does_not_override_detected_role(tmp_path, monkeypatch):
    from live_agent_bridge import plugins as P

    monkeypatch.setenv("AGENT_BRIDGE_DATA_DIR", str(tmp_path))
    saved = P.learn("Synth", "Filter 1 Cutoff", "drive", "agent guess", "inference")
    assert saved["status"] == "pending"
    understood = P.understand(_inspect())
    u = understood["understanding"]
    assert u["controllable_roles"].get("cutoff") == ["Filter 1 Cutoff"]
    assert "drive" not in u["controllable_roles"]
    # the pending guess is still reported so the user can confirm it
    assert understood["exposed_parameters"][0]["learned"]["status"] == "pending"
    assert P.resolve_role(_inspect(), "cutoff") == "Filter 1 Cutoff"


def test_confirmed_user_role_still_wins(tmp_path, monkeypatch):
    from live_agent_bridge import plugins as P

    monkeypatch.setenv("AGENT_BRIDGE_DATA_DIR", str(tmp_path))
    P.learn("Synth", "Filter 1 Cutoff", "drive", "user says drive", "user")
    u = P.understand(_inspect())["understanding"]
    assert u["controllable_roles"].get("drive") == ["Filter 1 Cutoff"]
    assert "cutoff" not in u["controllable_roles"]
