"""Display-text parameter writes must land on the requested value for small-unit displays.

Real case (Grain Delay 'Spray'): asking for "45.0 ms" produced "0.05 ms". The text is parsed to
seconds (0.045), written to display_value (which Live reads in display units, i.e. 0.045 ms), and
an absolute 0.051 tolerance accepted the wrong result instead of falling back to the search.
"""

import math

import pytest

from live_agent_bridge.remote_script.AgentBridge import ops


class MsParam:
    """0..1 parameter shown as milliseconds on a square curve, like Grain Delay Spray."""

    name, min, max, is_quantized, is_enabled, value_items = "Spray", 0.0, 1.0, False, True, []

    def __init__(self):
        self.value = 0.0

    def str_for_value(self, v):
        return "%.2f ms" % (500.0 * v * v)

    def __str__(self):
        return self.str_for_value(self.value)

    @property
    def display_value(self):
        return 500.0 * self.value * self.value

    @display_value.setter
    def display_value(self, ms):            # Live interprets display_value in display units
        self.value = math.sqrt(max(0.0, ms) / 500.0)


@pytest.mark.parametrize("text, want_ms", [("45.0 ms", 45.0), ("2 ms", 2.0), ("300 ms", 300.0)])
def test_ms_display_lands_on_requested_value(text, want_ms):
    p = MsParam()
    ops.set_param(p, display=text)
    got = 500.0 * p.value * p.value
    assert got == pytest.approx(want_ms, rel=0.03, abs=0.01)


def test_db_display_still_uses_fast_path():
    class DbParam(MsParam):
        def str_for_value(self, v):
            return "%.1f dB" % (v * 24 - 12)

        @property
        def display_value(self):
            return self.value * 24 - 12

        @display_value.setter
        def display_value(self, db):
            self.value = (db + 12) / 24

    p = DbParam()
    ops.set_param(p, display="-6 dB")
    assert str(p) == "-6.0 dB"
