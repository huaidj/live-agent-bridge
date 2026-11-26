"""Focused MCP tests for the independent taste server tools."""

import json
from contextlib import asynccontextmanager

from mcp.shared.memory import create_connected_server_and_client_session


@asynccontextmanager
async def taste_session(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_DATA_DIR", str(tmp_path))
    from live_agent_bridge.taste import tools as taste_tools
    server = taste_tools.standalone()._mcp_server
    async with create_connected_server_and_client_session(server) as session:
        yield session


async def tool(session, name, **args):
    r = await session.call_tool(name, args)
    text = r.content[0].text if r.content else ""
    assert not r.isError, text
    return json.loads(text) if text[:1] in "[{" else text


async def tool_error(session, name, **args):
    r = await session.call_tool(name, args)
    text = r.content[0].text if r.content else ""
    assert r.isError, text
    return text


def pref_id(res):
    assert isinstance(res, dict), res
    pid = res.get("preference_id") or res.get("id")
    assert pid, res
    return pid


async def test_remember_recall_and_profile(tmp_path, monkeypatch):
    async with taste_session(tmp_path, monkeypatch) as session:
        res = await tool(session, "taste_remember", statement="short kick", origin="user",
                         domain="drums", writer="test")
        assert "confirmed" in json.dumps(res), res
        r = await tool(session, "taste_recall", query="kick")
        assert r["total"] >= 1, r
        assert "short kick" in json.dumps(r), r
        profile = await tool(session, "taste_profile", domain="drums")
        assert "short kick" in json.dumps(profile), profile


async def test_pending_confirm_and_id_errors(tmp_path, monkeypatch):
    async with taste_session(tmp_path, monkeypatch) as session:
        res = await tool(session, "taste_remember", statement="warm pads", origin="inference",
                         domain="synth", writer="test")
        assert "pending" in json.dumps(res), res
        pid = pref_id(res)
        done = await tool(session, "taste_remember", action="confirm", statement="yes, I like warm pads",
                          origin="user", preference_id=pid, writer="test")
        assert "confirmed" in json.dumps(done), done
        text = await tool_error(session, "taste_remember", action="confirm", statement="yes", origin="user")
        assert "preference ID" in text, text
        text = await tool_error(session, "taste_remember", action="confirm", statement="yes",
                                origin="inference", preference_id=pid)
        assert "preference ID" in text, text
        text = await tool_error(session, "taste_remember", action="retire", statement="drop it",
                                origin="user", preference_id="missing-pref-0000")
        assert "Preference ID" in text, text


async def test_validation_errors_and_reference_list(tmp_path, monkeypatch):
    async with taste_session(tmp_path, monkeypatch) as session:
        await tool_error(session, "taste_recall", query="")
        text = await tool_error(session, "taste_remember", statement="   ", origin="user")
        assert "statement" in text, text
        text = await tool_error(session, "music_reference", action="status")
        assert "job_id" in text, text
        r = await tool(session, "music_reference", action="list")
        assert isinstance(r, dict), r
        assert isinstance(r["results"], list), r
