"""device_control store_bank crashed Live (access violation) on a native device on 2026-11-17: the op must refuse
anything that is not a Max device with a valid bank index BEFORE touching Device.store_chosen_bank."""

import pytest
from fake_live import Obj

from live_agent_bridge.client import LiveError


class Native(Obj):
    class_name = "Reverb"

    def __init__(self, parent):
        Obj.__init__(self, parent)
        self.name = "Reverb"
        self.parameters = []
        self.calls = []

    def store_chosen_bank(self, script, bank):  # a call here is the crash
        self.calls.append((script, bank))


class MaxDev(Native):
    class_name = "MxDeviceAudioEffect"

    def get_bank_count(self):
        return 2


async def test_native_device_is_refused_without_calling_live(client, live):
    t = live.song.tracks[0]
    d = Native(t)
    t.devices.append(d)
    with pytest.raises(LiveError, match="only works on Max for Live"):
        await client.request("device.control", {"action": "store_bank", "track": 0, "index": 0, "bank": 0})
    assert d.calls == []


async def test_max_device_validates_bank_then_stores(client, live):
    t = live.song.tracks[0]
    d = MaxDev(t)
    t.devices.append(d)
    with pytest.raises(LiveError, match="bank must be between 0 and 1"):
        await client.request("device.control", {"action": "store_bank", "track": 0, "index": 0, "bank": 5})
    with pytest.raises(LiveError, match="bank must be between"):
        await client.request("device.control", {"action": "store_bank", "track": 0, "index": 0})
    assert d.calls == []
    await client.request("device.control", {"action": "store_bank", "track": 0, "index": 0, "bank": 1})
    assert d.calls == [(0, 1)]
