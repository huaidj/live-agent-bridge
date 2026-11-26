"""A manual write to an automated parameter overrides its automation (automation_state 2). The response must say so."""

import types

from AgentBridge.ops import param_info


def P(state):
    return types.SimpleNamespace(name="Track Volume", value=0.5, min=0.0, max=1.0, is_quantized=False,
                                 automation_state=state, str_for_value=lambda v: "%.1f" % v)


def test_overridden_parameter_carries_a_warning():
    info = param_info(P(2))
    assert info["automation_state"] == 2 and "re_enable" in info["warning"]


def test_automated_or_plain_parameters_have_no_warning():
    assert "warning" not in param_info(P(1)) and param_info(P(1))["automation_state"] == 1
    assert "warning" not in param_info(P(0)) and "automation_state" not in param_info(P(0))
