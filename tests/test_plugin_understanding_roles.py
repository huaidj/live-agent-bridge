"""Focused offline tests for plugins.understand(): role mapping, module counts and learned overrides."""


def _inspect(exposed, unexposed=()):
    return {
        "plugin_name": "Synth",
        "exposed_parameters": [{"name": n} for n in exposed],
        "unexposed_parameter_names": list(unexposed),
    }


def test_exposed_parameters_map_through_role_of(tmp_path, monkeypatch):
    from live_agent_bridge import plugins as P

    monkeypatch.setenv("AGENT_BRIDGE_DATA_DIR", str(tmp_path))
    names = ["Filter 1 Cutoff", "Filter 1 Resonance", "Master Volume", "Mystery Knob"]
    understood = P.understand(_inspect(names))
    u = understood["understanding"]
    expected: dict[str, list[str]] = {}
    for n in names:
        r = P.role_of(n)
        if r:
            expected.setdefault(r, []).append(n)
    assert u["controllable_roles"] == expected
    assert u["controllable_roles"] == {
        "cutoff": ["Filter 1 Cutoff"],
        "resonance": ["Filter 1 Resonance"],
        "volume": ["Master Volume"],
    }
    by_name = {p["name"]: p for p in understood["exposed_parameters"]}
    assert by_name["Mystery Knob"]["role"] is None
    assert by_name["Filter 1 Cutoff"]["role"] == "cutoff"
    assert all("learned" not in p for p in understood["exposed_parameters"])
    assert u["learned_parameters"] == 0
    assert u["roles_needing_configure"] == {}


def test_unexposed_names_counted_in_modules_and_roles_needing_configure(tmp_path, monkeypatch):
    from live_agent_bridge import plugins as P

    monkeypatch.setenv("AGENT_BRIDGE_DATA_DIR", str(tmp_path))
    unexposed = ["Filter 1 Resonance", "Filter 1 Drive", "Env 2 Attack", "Env 2 Release"]
    u = P.understand(_inspect(["Filter 1 Cutoff"], unexposed))["understanding"]
    assert u["modules"]["Filter 1"] == {
        "exposed": 1,
        "unexposed": 2,
        "examples": ["Filter 1 Cutoff", "Filter 1 Resonance", "Filter 1 Drive"],
    }
    assert u["modules"]["Env 2"] == {
        "exposed": 0,
        "unexposed": 2,
        "examples": ["Env 2 Attack", "Env 2 Release"],
    }
    assert u["roles_needing_configure"] == {
        "resonance": ["Filter 1 Resonance"],
        "drive": ["Filter 1 Drive"],
        "attack": ["Env 2 Attack"],
        "release": ["Env 2 Release"],
    }
    assert u["controllable_roles"] == {"cutoff": ["Filter 1 Cutoff"]}


def test_confirmed_user_override_and_pending_inference_stays_out(tmp_path, monkeypatch):
    from live_agent_bridge import plugins as P

    monkeypatch.setenv("AGENT_BRIDGE_DATA_DIR", str(tmp_path))
    assert P.learn("Synth", "Filter 1 Cutoff", "drive", "user says drive", "user")["status"] == "confirmed"
    assert P.learn("Synth", "Filter 1 Resonance", "mix", "agent guess", "inference")["status"] == "pending"
    kept = P.learn("Synth", "Filter 1 Cutoff", "pitch", "agent guess", "inference")
    assert kept["kept"]["role"] == "drive"
    understood = P.understand(_inspect(["Filter 1 Cutoff", "Filter 1 Resonance"]))
    u = understood["understanding"]
    assert u["controllable_roles"] == {
        "drive": ["Filter 1 Cutoff"],
        "resonance": ["Filter 1 Resonance"],
    }
    assert "mix" not in u["controllable_roles"]
    assert "pitch" not in u["controllable_roles"]
    assert "cutoff" not in u["controllable_roles"]
    by_name = {p["name"]: p for p in understood["exposed_parameters"]}
    assert by_name["Filter 1 Cutoff"]["learned"]["status"] == "confirmed"
    assert by_name["Filter 1 Resonance"]["learned"]["status"] == "pending"
    assert by_name["Filter 1 Resonance"]["learned"]["role"] == "mix"
    assert by_name["Filter 1 Resonance"]["role"] == "resonance"
    assert u["learned_parameters"] == 2
