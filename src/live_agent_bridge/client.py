"""Asyncio client for the AgentBridge Remote Script.

One persistent TCP connection, newline-delimited JSON, any number of requests
in flight (matched by id). Connection loss fails pending requests at once;
a request is never re-sent after it may have reached Live, so writes are
never duplicated.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import socket
import time
from collections import deque
from typing import Any

DEFAULT_HOST = os.environ.get("AGENT_BRIDGE_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("AGENT_BRIDGE_PORT", "9890"))
# A request must *start* executing within this many seconds or Live drops it
# unexecuted; the client reports LiveBusy once that deadline has passed
# without hearing anything from Live (heartbeats arrive every 0.5 s).
START_DEADLINE = float(os.environ.get("AGENT_BRIDGE_START_DEADLINE", "3.0"))
BUSY_MESSAGE = ("Live's main thread is not responding (a modal dialog such as save/export/preferences is "
                "probably open). Close it in Live and retry. The request was NOT executed.")

NOT_CONNECTED_HELP = (
    "Cannot reach Ableton Live on {host}:{port}. Make sure Live is running and that "
    "'AgentBridge' is selected as a Control Surface (Settings > Link, Tempo & MIDI; "
    "Input/Output: None). Install the script with `live-agent-bridge install`."
)


class LiveError(Exception):
    """Live executed (or refused) the request and reported an error."""

    def __init__(self, etype: str, message: str):
        super().__init__(f"{etype}: {message}")
        self.etype = etype
        self.message = message


class LiveNotConnected(LiveError):
    pass


class LiveTimeout(LiveError):
    pass


class LiveClient:
    def __init__(self, host: str | None = None, port: int | None = None, connect_timeout: float = 2.0):
        self.host = host or os.environ.get("AGENT_BRIDGE_HOST", DEFAULT_HOST)
        self.port = port or int(os.environ.get("AGENT_BRIDGE_PORT", DEFAULT_PORT))
        self.connect_timeout = connect_timeout
        self.hello: dict[str, Any] | None = None
        self.events: deque[dict[str, Any]] = deque(maxlen=2000)
        self.last_rtt_ms: float | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._connect_lock = asyncio.Lock()
        self._read_task: asyncio.Task | None = None
        self._event_waiters: list[asyncio.Future] = []
        self._deadlines: dict[int, float] = {}
        self.last_rx = 0.0
        self._watchdog: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        async with self._connect_lock:
            if self.connected:
                return
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port, limit=64 * 1024 * 1024),
                    self.connect_timeout)
            except (OSError, asyncio.TimeoutError) as e:
                raise LiveNotConnected("NotConnected",
                                       NOT_CONNECTED_HELP.format(host=self.host, port=self.port)) from e
            sock = writer.get_extra_info("socket")
            if sock is not None:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                line = await asyncio.wait_for(reader.readline(), self.connect_timeout)
                hello = json.loads(line)
            except Exception as e:
                writer.close()
                raise LiveNotConnected("NotConnected", f"bridge handshake failed: {e}") from e
            self.hello = hello
            self._reader, self._writer = reader, writer
            self.last_rx = time.time()
            self._read_task = asyncio.create_task(self._read_loop(reader))
            if self._watchdog is None or self._watchdog.done():
                self._watchdog = asyncio.create_task(self._watch())

    async def close(self) -> None:
        if self._watchdog:
            self._watchdog.cancel()
        if self._read_task:
            self._read_task.cancel()
        if self._writer:
            self._writer.close()
        self._drop("ConnectionClosed", "client closed")

    def _drop(self, etype: str, message: str) -> None:
        self._writer = None
        self._reader = None
        pending, self._pending = self._pending, {}
        self._deadlines.clear()
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(LiveError(etype, message))

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                self.last_rx = time.time()
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("event") == "hb":
                    continue
                if "ack" in msg:  # a write started executing: never report it as not executed
                    self._deadlines.pop(msg["ack"], None)
                    continue
                rid = msg.get("id")
                if rid is not None and "event" not in msg:
                    self._deadlines.pop(rid, None)
                    fut = self._pending.pop(rid, None)
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
                    continue
                if "event" in msg:
                    self.events.append(msg)
                    waiters, self._event_waiters = self._event_waiters, []
                    for w in waiters:
                        if not w.done():
                            w.set_result(None)
        except (asyncio.CancelledError, ConnectionError, OSError):
            pass
        finally:
            if self._reader is reader:
                self._drop("ConnectionLost",
                           "connection to Live dropped (Live closed, script reloaded or crashed); "
                           "the request may or may not have been applied - check state before retrying")

    async def request(self, op: str, args: dict[str, Any] | None = None, timeout: float = 20.0) -> Any:
        if not self.connected:
            await self.connect()
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        expires_at = time.time() + START_DEADLINE
        self._deadlines[rid] = expires_at
        frame = json.dumps({"id": rid, "op": op, "args": args or {}, "timeout": timeout,
                            "expires_at": expires_at}, separators=(",", ":")).encode() + b"\n"
        t0 = time.perf_counter()
        try:
            self._writer.write(frame)
        except Exception as e:
            self._pending.pop(rid, None)
            self._drop("ConnectionLost", str(e))
            raise LiveNotConnected("NotConnected", NOT_CONNECTED_HELP.format(host=self.host, port=self.port)) from e
        try:
            msg = await asyncio.wait_for(fut, timeout + 1.0)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            self._deadlines.pop(rid, None)
            raise LiveTimeout("Timeout", f"Live did not answer {op!r} within {timeout:.0f}s") from None
        self.last_rtt_ms = (time.perf_counter() - t0) * 1000
        if not msg.get("ok"):
            err = msg.get("error") or {}
            raise LiveError(err.get("type", "Error"), err.get("message", "unknown error"))
        return msg.get("result")

    async def _watch(self) -> None:
        """Fail requests whose start deadline passed while Live stayed silent: Live will
        drop them unexecuted when it resumes, so reporting LiveBusy now is safe."""
        try:
            while True:
                await asyncio.sleep(0.25)
                now = time.time()
                for rid, deadline in list(self._deadlines.items()):
                    if now > deadline + 0.25 and self.last_rx < deadline:
                        self._deadlines.pop(rid, None)
                        fut = self._pending.pop(rid, None)
                        if fut is not None and not fut.done():
                            fut.set_exception(LiveError("LiveBusy", BUSY_MESSAGE))
        except asyncio.CancelledError:
            pass

    def take_events(self, limit: int = 200, sub: int | None = None) -> list[dict[str, Any]]:
        out, keep = [], deque(maxlen=self.events.maxlen)
        while self.events:
            e = self.events.popleft()
            if len(out) < limit and (sub is None or e.get("sub") == sub):
                out.append(e)
            else:
                keep.append(e)
        self.events.extend(keep)
        return out

    async def wait_events(self, timeout: float, sub: int | None = None) -> None:
        """Wait until an event (of subscription ``sub`` if given) is queued, or timeout.
        Events of other subscriptions do not end the wait early."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not any(sub is None or e.get("sub") == sub for e in self.events):
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            fut = loop.create_future()
            self._event_waiters.append(fut)
            try:
                await asyncio.wait_for(fut, remaining)
            except asyncio.TimeoutError:
                return
