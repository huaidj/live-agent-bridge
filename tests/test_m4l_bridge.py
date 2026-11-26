"""Max for Live LiveAPI transport: client framing, preview-by-default writes, identity binding,
observe/poll, unreachable device and the MCP tool. A fake device speaks the real device's protocol."""

import asyncio
import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from live_agent_bridge import m4l, server


class FakeMaxDevice:
    """Loopback NDJSON server mimicking node_bridge.js + max_lom_runtime.js semantics."""

    def __init__(self):
        self.values = {("live_set", "tempo"): 111.0, ("live_set tracks 0", "name"): "Drums"}
        self.types = {"live_set": "Song", "live_set tracks 0": "Track", "this_device": "MaxDevice"}
        self.ids = {"live_set": 1, "live_set tracks 0": 7, "this_device": 42}
        self.subs, self.writes, self.server, self.port, self.silent = {}, [], None, None, False

    async def start(self):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    def _identity(self, path):
        return "max:test:%d" % self.ids[path]

    def handle(self, r):
        op = r.get("op")
        if op == "readiness":
            return {"status": "ok", "ready": True, "backend": "max"}
        if op in ("poll", "unobserve"):
            sub = self.subs.get(r.get("subscription_id"))
            if not sub or sub["owner"] != r.get("owner"):
                return {"status": "error", "code": "subscription_unavailable"}
            if op == "unobserve":
                del self.subs[r["subscription_id"]]
                return {"status": "ok", "closed": True}
            events, sub["events"] = sub["events"], []
            return {"status": "ok", "events": events, "remaining": 0}
        path, typ = r.get("path"), r.get("object_type")
        if path not in self.types:
            return {"status": "error", "code": "invalid_request", "detail": "no such path"}
        if self.types[path] != typ and not (typ == "Device" and self.types[path] == "MaxDevice"):
            return {"status": "error", "code": "object_type_mismatch"}
        target = {"path": path, "object_type": self.types[path], "identity": self._identity(path), "backend": "max"}
        if op == "resolve":
            return {"status": "ok", "target": target}
        key = (path, r.get("member"))
        if op == "get":
            return {"status": "ok", "target": target, "value": self.values.get(key)}
        if op in ("set", "call", "observe") and r.get("expected_identity") != target["identity"]:
            return {"status": "error", "code": "stale_identity"}
        if op == "observe":
            sid = "sub%d" % (len(self.subs) + 1)
            self.subs[sid] = {"owner": r["owner"], "events": [{"value": self.values.get(key)}]}
            return {"status": "ok", "subscription_id": sid}
        if r.get("dry_run") is not False:
            return {"status": "preview", "target": target, "before": self.values.get(key),
                    "proposed": r.get("value"), "write_may_have_occurred": False}
        if op == "set":
            before = self.values.get(key)
            self.values[key] = r["value"]
            self.writes.append(key)
            return {"status": "ok", "target": target, "before": before, "value": r["value"], "verified": True,
                    "write_may_have_occurred": True}
        self.writes.append((path, r["member"], tuple(r.get("arguments") or [])))
        return {"status": "ok", "target": target, "result": None, "write_may_have_occurred": True}

    async def _client(self, reader, writer):
        while line := await reader.readline():
            frame = json.loads(line)
            if self.silent:
                continue
            writer.write((json.dumps({"id": frame["id"], "result": self.handle(frame["request"])}) + "\n").encode())
            await writer.drain()
        writer.close()


@pytest.fixture
async def device():
    d = FakeMaxDevice()
    await d.start()
    yield d
    await d.stop()


async def test_get_and_preview_by_default(device):
    c = m4l.M4LClient(port_=device.port)
    assert (await c.readiness())["ready"] is True
    assert (await c.get("live_set", "Song", "tempo"))["value"] == 111.0
    preview = await c.set("live_set", "Song", "tempo", 120.0)
    assert preview["status"] == "preview" and device.values[("live_set", "tempo")] == 111.0
    assert device.writes == []
    done = await c.set("live_set", "Song", "tempo", 120.0, write=True)
    assert done["status"] == "ok" and device.values[("live_set", "tempo")] == 120.0
    await c.close()


async def test_get_normalizes_empty_arrangement_clip_atom_only(monkeypatch):
    empty = {"status": "ok", "value": [""], "total": 1, "next_offset": None}

    async def request(op, **kwargs):
        assert op == "get"
        return empty

    client = m4l.M4LClient()
    monkeypatch.setattr(client, "request", request)
    clips = await client.get("live_set tracks 0", "Track", "arrangement_clips")
    assert clips == {**empty, "value": [], "total": 0}
    assert (await client.get("live_set tracks 0", "Track", "another_member")) == empty


@pytest.mark.parametrize("readback_identity", ["max:test:1", "max:test:2"])
async def test_partial_set_only_upgrades_after_readback_of_same_identity(monkeypatch, readback_identity):
    calls = []
    original_target = {"identity": "max:test:1"}
    partial = {"status": "partial", "target": original_target, "before": False, "value": False,
               "actual": False, "verified": False, "verification_kind": "no_observable_change"}

    async def request(op, **kwargs):
        calls.append(op)
        if op == "resolve":
            return {"status": "ok", "target": original_target}
        if op == "set":
            assert kwargs["expected_identity"] == original_target["identity"]
            assert kwargs["expected_value"] is False
            return partial
        assert op == "get"
        return {"status": "ok", "target": {"identity": readback_identity}, "value": True}

    client = m4l.M4LClient()
    monkeypatch.setattr(client, "request", request)
    result = await client.set("live_set", "Song", "loop", True, write=True, expected_value=False)
    assert calls == ["resolve", "set", "get"]
    if readback_identity == original_target["identity"]:
        assert result["status"] == "ok" and result["value"] is True
        assert result["verification_kind"] == "settled_readback"
        assert result["initial_verification_kind"] == "no_observable_change"
    else:
        assert result == partial


@pytest.mark.parametrize("actual, verified", [(0.4200000464916229, True), (0.419, False)])
async def test_set_distinguishes_float32_roundoff_from_a_real_parameter_mismatch(monkeypatch, actual, verified):
    target = {"identity": "max:test:4"}
    partial = {"status": "partial", "target": target, "verification_kind": "mismatch", "actual": actual}
    calls = []

    async def request(op, **kwargs):
        calls.append(op)
        if op == "resolve":
            return {"status": "ok", "target": target}
        if op == "set":
            return partial
        assert op == "get"
        return {"status": "ok", "target": target, "value": actual}

    client = m4l.M4LClient()
    monkeypatch.setattr(client, "request", request)
    result = await client.set("this_device parameters 0", "DeviceParameter", "value", 0.42, write=True)
    assert calls == ["resolve", "set", "get"]
    if verified:
        assert result["verification_kind"] == "float32_readback"
        assert result["status"] == "ok" and result["exact_match"] is False
    else:
        assert result == partial


async def test_pipelined_requests_match_by_id(device):
    c = m4l.M4LClient(port_=device.port)
    results = await asyncio.gather(*[c.get("live_set tracks 0", "Track", "name") for _ in range(20)],
                                   c.get("live_set", "Song", "tempo"))
    assert [r["value"] for r in results[:-1]] == ["Drums"] * 20 and results[-1]["value"] == 111.0
    await c.close()


async def test_call_observe_poll_and_type_check(device):
    c = m4l.M4LClient(port_=device.port)
    assert (await c.call("live_set", "Song", "stop_playing"))["status"] == "preview"
    assert (await c.call("live_set", "Song", "stop_playing", write=True))["status"] == "ok"
    assert device.writes == [("live_set", "stop_playing", ())]
    sub = await c.observe("live_set", "Song", "tempo")
    events = await c.poll(sub["subscription_id"])
    assert events["events"] == [{"value": 111.0}]
    assert (await c.unobserve(sub["subscription_id"]))["closed"] is True
    with pytest.raises(m4l.M4LError, match="Cannot resolve"):
        await c.set("live_set", "Track", "name", "x", write=True)     # wrong object type never writes
    assert len(device.writes) == 1
    await c.close()


async def test_unreachable_and_timeout_are_explicit(device):
    with pytest.raises(m4l.M4LError, match="Load the 'LOM API Bridge'"):
        await m4l.M4LClient(port_=1).readiness()
    device.silent = True
    c = m4l.M4LClient(port_=device.port, timeout=0.3)
    with pytest.raises(m4l.M4LError, match="a write may have happened"):
        await c.get("live_set", "Song", "tempo")
    await c.close()
    with pytest.raises(ValueError, match="loopback"):
        m4l.M4LClient(host="192.168.1.5")


async def test_mcp_tool_status_get_and_write_flag(device, monkeypatch, tmp_path):
    monkeypatch.setenv("ABLETON_MAX_PORT", str(device.port))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from live_agent_bridge import server_m4l
    monkeypatch.setattr(server_m4l, "_client", None)
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as s:
        async def tool(**args):
            r = await s.call_tool("m4l_bridge", args)
            return r.isError, json.loads(r.content[0].text) if not r.isError else r.content[0].text

        err, st = await tool(action="status")
        assert not err and st["connected"] is True and st["install"]["installed"] is False
        err, got = await tool(action="get", path="live_set", object_type="Song", member="tempo")
        assert got["value"] == 111.0
        err, pv = await tool(action="set", path="live_set", object_type="Song", member="tempo", value=99.0)
        assert pv["status"] == "preview" and device.values[("live_set", "tempo")] == 111.0
        err, ok = await tool(action="set", path="live_set", object_type="Song", member="tempo", value=99.0, write=True)
        assert ok["status"] == "ok" and device.values[("live_set", "tempo")] == 99.0
        err, msg = await tool(action="get", path="live_set", object_type="Song")
        assert err and "member is required" in msg
        # a device error frame is a tool error (seen in Live: object_type_mismatch came back as a plain dict)
        err, msg = await tool(action="get", path="live_set", object_type="Track", member="name")
        assert err and "object_type_mismatch" in msg
    await server_m4l._client.close()
