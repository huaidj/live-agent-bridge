"""Live names the same knob 'Dry/Wet' (Reverb), 'Dry Wet' (Echo) and 'DryWet' (Grain Delay); any spelling must match."""

import types

import pytest
from AgentBridge.ops import match_params


def P(n):
    return types.SimpleNamespace(name=n)


@pytest.mark.parametrize("device_name", ["Dry/Wet", "Dry Wet", "DryWet"])
@pytest.mark.parametrize("asked", ["Dry/Wet", "Dry Wet", "DryWet", "dry/wet", "drywet"])
def test_dry_wet_spellings(device_name, asked):
    plist = [P("Device On"), P("Feedback"), P(device_name)]
    assert [p.name for p in match_params(plist, asked)] == [device_name]


def test_exact_beats_looser_matches():
    plist = [P("Filter Freq"), P("Freq"), P("LFO Freq")]
    assert [p.name for p in match_params(plist, "Freq")] == ["Freq"]
    assert [p.name for p in match_params(plist, "lfo")] == ["LFO Freq"]
    assert match_params(plist, "Resonance") == []
