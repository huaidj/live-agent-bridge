"""status reports per-op Live-side timing so an agent can find the slow ops in the current set."""

import pytest

from live_agent_bridge.client import LiveError


async def test_status_lists_slowest_ops(client):
    for _ in range(3):
        await client.request("lom.get", {"path": "live_set", "props": ["tempo"]})
    await client.request("song.set", {"tempo": 101})
    with pytest.raises(LiveError):
        await client.request("lom.get", {"path": "live_set", "props": ["__class__"]})
    st = await client.request("status")
    rows = {r["op"]: r for r in st["slowest_ops"]}
    assert rows["lom.get"]["n"] == 4  # failures are timed too
    assert rows["song.set"]["n"] == 1
    for r in rows.values():
        assert r["mean_ms"] >= 0 and r["max_ms"] >= r["mean_ms"]
    means = [r["mean_ms"] for r in st["slowest_ops"]]
    assert means == sorted(means, reverse=True)


def test_top_ops_ranks_and_limits():
    from live_agent_bridge.remote_script.AgentBridge.core import _top_ops

    rows = _top_ops({"a": [2, 10.0, 7.0], "b": [1, 400.0, 400.0], "c": [0, 0.0, 0.0]}, n=1)
    assert rows == [{"op": "b", "n": 1, "mean_ms": 400.0, "max_ms": 400.0}]
