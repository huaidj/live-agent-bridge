"""Global undo is refused before invoking Live's native history methods."""

import pytest

from live_agent_bridge.client import LiveError
from live_agent_bridge.m4l import M4LClient, M4LError


@pytest.mark.parametrize("method", ["undo", "redo"])
async def test_remote_script_refuses_global_history(client, live, monkeypatch, method):
    def called():
        pytest.fail("The native global history method must not run")

    monkeypatch.setattr(live.song, method, called, raising=False)
    for operation, arguments in (
        ("transport", {"action": method}),
        ("lom.call", {"path": "live_set", "method": method}),
    ):
        with pytest.raises(LiveError, match="unavailable through AgentBridge"):
            await client.request(operation, arguments)


@pytest.mark.parametrize("method", ["undo", "redo"])
async def test_m4l_refuses_global_history_before_connecting(method):
    with pytest.raises(M4LError, match="unavailable through AgentBridge"):
        await M4LClient(port_=1).call("live_set", "Song", method, write=True)
