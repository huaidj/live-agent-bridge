"""Max for Live LiveAPI transport as an MCP tool (second official backend next to the Remote Script)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from . import m4l
from .server import RW, mcp

_client: m4l.M4LClient | None = None


def _get_client() -> m4l.M4LClient:
    global _client
    if _client is None:
        _client = m4l.M4LClient()
    return _client


@mcp.tool(annotations=RW)
async def m4l_bridge(
    action: Literal["status", "resolve", "get", "set", "call", "observe", "poll", "unobserve", "capabilities"],
    path: Annotated[str | None, Field(description="LiveAPI path, e.g. 'live_set', 'live_set tracks 0', "
                                                  "'this_device'")] = None,
    object_type: Annotated[str | None, Field(description="Expected LOM class, e.g. Song, Track, Device, "
                                                         "DeviceParameter, Clip")] = None,
    member: Annotated[str | None, Field(description="Property or function name")] = None,
    value: Any = None,
    arguments: list | None = None,
    write: Annotated[bool, Field(description="set/call: actually write. Default false = preview only")] = False,
    subscription_id: str | None = None,
    offset: int | None = None,
    limit: int | None = None,
) -> dict:
    """Talk to Live through Max for Live's official LiveAPI (Cycling '74 docs: apiref/js/liveapi, LOM).
    Needs the 'LOM API Bridge' MIDI effect loaded on one track. status = install + connection check.
    Writes are previews unless write=true, and are bound to the identity a fresh resolve returned.
    Device errors (object_type_mismatch, unknown_member, stale_identity, ...) are tool errors.
    The first poll after observe also carries the value current at subscribe time."""
    if action == "status":
        out = {"install": m4l.install_status(), "port": m4l.port()}
        try:
            out["readiness"] = await _get_client().readiness()
            out["connected"] = True
        except m4l.M4LError as error:
            out["connected"], out["detail"] = False, str(error)
        return out
    try:
        result = await _dispatch(_get_client(), action, path, object_type, member, value, arguments, write,
                                 subscription_id, offset, limit)
    except m4l.M4LError as error:
        raise ToolError(str(error)) from None
    if result.get("status") == "error":
        # The device answers errors as frames; surface them as tool errors like every other tool.
        msg = "Max for Live bridge error %s: %s" % (result.get("code", "unknown"), result.get("detail", ""))
        if "write_may_have_occurred" in result:
            msg += " (write_may_have_occurred=%s)" % result["write_may_have_occurred"]
        raise ToolError(msg)
    return result


async def _dispatch(client: m4l.M4LClient, action: str, path: str | None, object_type: str | None,
                    member: str | None, value: Any, arguments: list | None, write: bool,
                    subscription_id: str | None, offset: int | None, limit: int | None) -> dict:
    if action in ("poll", "unobserve"):
        if not subscription_id:
            raise ToolError("subscription_id is required")
        return await (client.poll(subscription_id, limit or 100) if action == "poll"
                      else client.unobserve(subscription_id))
    if not path or not object_type:
        raise ToolError("path and object_type are required")
    if action == "resolve":
        return await client.resolve(path, object_type)
    if action == "capabilities":
        return await client.capabilities(path, object_type, member, offset or 0, limit or 100)
    if not member:
        raise ToolError("member is required")
    if action == "get":
        return await client.get(path, object_type, member, offset, limit)
    if action == "set":
        return await client.set(path, object_type, member, value, write=write)
    if action == "call":
        return await client.call(path, object_type, member, arguments, write=write)
    return await client.observe(path, object_type, member)
