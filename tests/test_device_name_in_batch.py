"""Raw ops (used by `batch`) accept a device *name* in `device` when a track is given.

The MCP tool's `device` is a name/index while the raw op's `device` was a LOM path only, so
batch ops like device.set_params{track, device: "Reverb"} failed with "unknown path root".
"""

import pytest

from live_agent_bridge.client import LiveError


async def test_device_name_with_track_resolves(client, live):
    await client.request("device.insert", {"name": "Reverb", "track": 0})
    r = await client.request("device.set_params", {"track": 0, "device": "Reverb",
                                                   "params": [{"index": 0, "value": 0}]})
    assert r[0]["value"] == 0


async def test_paths_still_resolve_and_bad_names_still_fail(client, live):
    await client.request("device.insert", {"name": "Reverb", "track": 0})
    r = await client.request("device.set_params", {"device": "track 0 devices 0",
                                                   "params": [{"index": 0, "value": 1}]})
    assert r[0]["value"] == 1
    with pytest.raises(LiveError, match="no device named"):
        await client.request("device.set_params", {"track": 0, "device": "Nope", "params": [{"index": 0, "value": 1}]})
