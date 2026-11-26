"""Offline regression tests for numeric PluginChange input validation.

Non-finite numbers (NaN, +/-inf) must be rejected before they reach Live,
while ordinary finite values, including out-of-range absolute values that the
bridge documents as clamped, must still be accepted unchanged.
"""

import math

import pytest
from pydantic import ValidationError

from live_agent_bridge.server_plugins import PluginChange

NON_FINITE = [math.nan, math.inf, -math.inf, float("nan"), float("inf"), float("-inf")]


@pytest.mark.parametrize("field", ["value", "normalized", "delta_normalized"])
@pytest.mark.parametrize("bad", NON_FINITE)
def test_non_finite_numbers_are_rejected(field, bad):
    with pytest.raises(ValidationError):
        PluginChange(name="Cutoff", **{field: bad})


@pytest.mark.parametrize("field", ["value", "normalized", "delta_normalized"])
@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "Infinity"])
def test_non_finite_strings_are_rejected(field, bad):
    with pytest.raises(ValidationError):
        PluginChange.model_validate({"name": "Cutoff", field: bad})


@pytest.mark.parametrize("good", [0.0, 0.25, 0.5, 1.0])
def test_finite_normalized_values_are_accepted(good):
    change = PluginChange(name="Cutoff", normalized=good)
    assert change.normalized == good


@pytest.mark.parametrize("good", [-1.0, -0.1, 0.0, 0.1, 1.0])
def test_finite_delta_values_are_accepted(good):
    change = PluginChange(role="cutoff", delta_normalized=good)
    assert change.delta_normalized == good


@pytest.mark.parametrize("good", [-1e6, -6.0, 0.0, 2500.0, 1e6])
def test_finite_absolute_values_pass_through_for_bridge_clamping(good):
    # Absolute values are not range-limited here; the bridge clamps finite values.
    change = PluginChange(index=3, value=good)
    assert change.value == good
    assert change.model_dump(exclude_none=True) == {"index": 3, "value": good}


@pytest.mark.parametrize("field,bad", [("normalized", 1.5), ("normalized", -0.1),
                                       ("delta_normalized", 1.5), ("delta_normalized", -1.5)])
def test_finite_out_of_range_normalized_inputs_still_rejected(field, bad):
    with pytest.raises(ValidationError):
        PluginChange(name="Cutoff", **{field: bad})
