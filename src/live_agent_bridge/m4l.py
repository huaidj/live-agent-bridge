"""Client for the Max for Live LiveAPI companion device ("LOM API Bridge").

The device runs inside Live as a MIDI effect: Node for Max (`node.script`) listens on a loopback TCP
port and forwards each request to a Max `js` object that executes it with Max's documented `LiveAPI`
(https://docs.cycling74.com/apiref/js/liveapi/). This gives the agent a second, official transport that
works from inside a Max device, next to the Python Remote Script used by the rest of the bridge.

Protocol (owned by the device; this module only speaks it): newline-delimited UTF-8 JSON frames
``{"id": str, "request": {...}}`` -> ``{"id": str, "result": {...}}``. Writes are previews unless
``dry_run`` is false, and every write must carry the identity returned by ``resolve``.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import math
import os
from pathlib import Path
from typing import Any

DEFAULT_PORT = 9878
MAX_FRAME = 1024 * 1024
MAX_IN_FLIGHT = 32
DEVICE_NAME = "LOM API Bridge"


class M4LError(Exception):
    """The Max device is unreachable or answered with an error frame."""


def port() -> int:
    value = os.environ.get("ABLETON_MAX_PORT")
    return int(value) if value else DEFAULT_PORT


def device_folder() -> Path:
    user = Path(os.environ.get("USERPROFILE", Path.home())) / "Documents" / "Ableton" / "User Library"
    return user / "Presets" / "MIDI Effects" / DEVICE_NAME


def install_status() -> dict:
    folder = device_folder()
    files = {name: (folder / name).is_file() for name in
             ("LOM_API_Bridge.amxd", "max_lom_runtime.js", "node_bridge.js", "lom_schema.json")}
    return {"folder": str(folder), "files": files, "installed": all(files.values())}


class M4LClient:
    """One persistent loopback connection; requests are pipelined and matched by id."""

    _ids = itertools.count(1)

    def __init__(self, host: str = "127.0.0.1", port_: int | None = None, timeout: float = 10.0):
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("The Max bridge only listens on loopback")
        self.host, self.port, self.timeout = host, port_ or port(), timeout
        self.owner = "agent-bridge-%d" % os.getpid()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._pump: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        # The device answers `request_limit` beyond 64 requests in flight (node_bridge.js); stay well below it so a
        # caller that gathers 200 requests gets 200 answers, not 136 errors.
        self._inflight = asyncio.Semaphore(MAX_IN_FLIGHT)

    async def connect(self) -> None:
        async with self._lock:
            if self._writer is not None and not self._writer.is_closing():
                return
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port, limit=4 * MAX_FRAME), 2.0)
            except (OSError, asyncio.TimeoutError):
                raise M4LError("Max for Live bridge not reachable on %s:%d. Load the '%s' MIDI effect "
                               "(User Library > Presets > MIDI Effects) on one track of the open set."
                               % (self.host, self.port, DEVICE_NAME)) from None
            self._pump = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                try:
                    frame = json.loads(line)
                except ValueError:
                    continue
                fut = self._pending.pop(str(frame.get("id")), None)
                if fut is not None and not fut.done():
                    fut.set_result(frame.get("result"))
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(M4LError("Max for Live bridge closed the connection"))
            self._pending.clear()
            self._writer = None

    async def request(self, op: str, timeout: float | None = None, **fields: Any) -> dict:
        async with self._inflight:
            return await self._request(op, timeout, **fields)

    async def _request(self, op: str, timeout: float | None = None, **fields: Any) -> dict:
        await self.connect()
        rid = "r%d" % next(self._ids)
        body = {"op": op, **{k: v for k, v in fields.items() if v is not None}}
        data = (json.dumps({"id": rid, "request": body}, ensure_ascii=False) + "\n").encode("utf-8")
        if len(data) > MAX_FRAME:
            raise M4LError("Request exceeds the 1 MiB frame limit")
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        assert self._writer is not None
        self._writer.write(data)
        await self._writer.drain()
        try:
            result = await asyncio.wait_for(fut, timeout or self.timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            # Never retried: the device may already have executed a write.
            raise M4LError("Max for Live bridge did not answer in time; a write may have happened") from None
        if not isinstance(result, dict):
            raise M4LError("Invalid reply from the Max for Live bridge")
        return result

    # -------------------------------------------------------------- convenience
    async def readiness(self) -> dict:
        return await self.request("readiness")

    async def resolve(self, path: str, object_type: str) -> dict:
        return await self.request("resolve", path=path, object_type=object_type)

    async def get(self, path: str, object_type: str, member: str, offset: int | None = None,
                  limit: int | None = None) -> dict:
        reply = await self.request("get", path=path, object_type=object_type, member=member,
                                   offset=offset, limit=limit)
        # Max LiveAPI can return one empty atom for an empty arrangement-clip
        # collection. It is never an actual clip, and it must not count as one.
        if (object_type == "Track" and member == "arrangement_clips" and reply.get("status") == "ok"
                and reply.get("value") == [""] and reply.get("total") == 1):
            return {**reply, "value": [], "total": 0, "next_offset": None}
        return reply

    async def _identity(self, path: str, object_type: str) -> str:
        resolved = await self.resolve(path, object_type)
        target = resolved.get("target") or {}
        if resolved.get("status") != "ok" or not target.get("identity"):
            raise M4LError("Cannot resolve %s as %s: %s" % (path, object_type, resolved.get("detail") or resolved))
        return target["identity"]

    async def set(self, path: str, object_type: str, member: str, value: Any, write: bool = False,
                  expected_value: Any = None) -> dict:
        identity = await self._identity(path, object_type)
        fields = {"path": path, "object_type": object_type, "member": member, "value": value,
                  "expected_identity": identity, "dry_run": not write}
        if expected_value is not None:
            fields["expected_value"] = expected_value
        reply = await self.request("set", **fields)
        kind = reply.get("verification_kind")
        float_parameter = object_type == "DeviceParameter" and member == "value" and isinstance(value, float)
        if (not write or reply.get("status") != "partial"
                or not (kind == "no_observable_change" or (kind == "mismatch" and float_parameter))
                or reply.get("target", {}).get("identity") != identity
                or not isinstance(value, (bool, int, float, str))):
            return reply

        # LiveAPI can apply a change on a later Live tick (for example Song.loop).
        # Never send another set. Only upgrade a partial result after reading the
        # requested value back from the exact same object.
        for delay in (0.025, 0.075, 0.15):
            await asyncio.sleep(delay)
            try:
                observed = await self.request("get", timeout=min(self.timeout, 0.25),
                                              path=path, object_type=object_type, member=member)
            except M4LError:
                break
            if observed.get("status") != "ok" or observed.get("target", {}).get("identity") != identity:
                break
            actual = observed.get("value")
            exact = actual == value
            roundoff = float_parameter and isinstance(actual, float) and math.isclose(
                actual, value, rel_tol=2e-7, abs_tol=0.0)
            if exact or roundoff:
                return {**reply, "status": "ok", "value": actual, "actual": actual, "verified": True,
                        "exact_match": exact, "verification_kind": "settled_readback" if exact else "float32_readback",
                        "initial_verification_kind": kind}
            if kind == "mismatch":
                break
        return reply

    async def call(self, path: str, object_type: str, member: str, arguments: list | None = None,
                   write: bool = False) -> dict:
        if write and object_type == "Song" and member in ("undo", "redo"):
            raise M4LError("Global Song undo/redo is unavailable through AgentBridge")
        identity = await self._identity(path, object_type)
        return await self.request("call", path=path, object_type=object_type, member=member,
                                  arguments=arguments or [], expected_identity=identity, dry_run=not write)

    async def observe(self, path: str, object_type: str, member: str, ttl_seconds: int = 60) -> dict:
        identity = await self._identity(path, object_type)
        return await self.request("observe", path=path, object_type=object_type, member=member,
                                  expected_identity=identity, owner=self.owner, ttl_seconds=ttl_seconds)

    async def poll(self, subscription_id: str, limit: int = 100) -> dict:
        return await self.request("poll", subscription_id=subscription_id, owner=self.owner, limit=limit)

    async def unobserve(self, subscription_id: str) -> dict:
        return await self.request("unobserve", subscription_id=subscription_id, owner=self.owner)

    async def capabilities(self, path: str, object_type: str, member: str | None = None,
                           offset: int = 0, limit: int = 100) -> dict:
        return await self.request("capabilities", path=path, object_type=object_type, member=member,
                                  offset=offset, limit=limit)

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
        if self._pump is not None:
            self._pump.cancel()
        self._writer = None
