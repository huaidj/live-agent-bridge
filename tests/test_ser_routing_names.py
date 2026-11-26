"""lom_get on routing properties must show display names, not '<Track.RoutingType object at 0x...>' (seen 2026-11-16)."""

import types

from AgentBridge.lom import Lom


def test_routing_objects_serialize_to_display_names():
    lom = Lom.__new__(Lom)   # ser() needs no Live set for plain values
    rt = types.SimpleNamespace(display_name="TRIBE · Drums")
    assert lom.ser(rt) == "TRIBE · Drums"
    assert lom.ser([rt, types.SimpleNamespace(display_name="Post FX")]) == ["TRIBE · Drums", "Post FX"]
    assert lom.ser({"x": rt}) == {"x": "TRIBE · Drums"}
