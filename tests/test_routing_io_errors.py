"""routing io= on a device without that port used to raise a bare 'IndexError: Index out of range' (seen against the
M4L LOM API Bridge, 2026-11-17). It must say what the device exposes."""

import pytest
from fake_live import Obj

from live_agent_bridge.client import LiveError


class Port:
    routing_type = "None"
    routing_channel = "None"
    available_routing_types = []
    available_routing_channels = []


class IoDevice(Obj):
    class_name = "MxDeviceMidiEffect"

    def __init__(self, parent):
        Obj.__init__(self, parent)
        self.name = "LOM API Bridge"
        self.parameters = []
        self.audio_inputs = []
        self.midi_inputs = [Port()]


async def _device(live):
    t = live.song.tracks[0]
    t.devices.append(IoDevice(t))
    return "live_set tracks 0 devices 0"


async def test_missing_port_lists_what_the_device_exposes(client, live):
    path = await _device(live)
    with pytest.raises(LiveError, match="has no audio_inputs; it exposes: midi_inputs"):
        await client.request("routing.set_io", {"device": path, "io": "audio_inputs"})


async def test_index_out_of_range_and_bad_io_are_explicit(client, live):
    path = await _device(live)
    with pytest.raises(LiveError, match="index must be 0..0"):
        await client.request("routing.set_io", {"device": path, "io": "midi_inputs", "index": 3})
    with pytest.raises(LiveError, match="io must be"):
        await client.request("routing.set_io", {"device": path, "io": "nonsense"})


async def test_valid_port_still_works(client, live):
    path = await _device(live)
    r = await client.request("routing.set_io", {"device": path, "io": "midi_inputs", "index": 0})
    assert set(r) == {"type", "channel"}
