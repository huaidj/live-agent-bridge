"""The Max device answers `request_limit` beyond 64 requests in flight (node_bridge.js). M4LClient must cap its own
concurrency so that gathering 200 requests yields 200 real answers."""

import asyncio
import json

from live_agent_bridge import m4l


class SlowDevice:
    """Replies 20 ms after each frame; refuses (request_limit) like the real device when more than 64 are pending."""

    def __init__(self):
        self.pending = 0
        self.max_pending = 0
        self.refused = 0
        self.server = None
        self.port = None

    async def start(self):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def _client(self, reader, writer):
        async def answer(frame):
            if self.pending >= 64:
                self.refused += 1
                result = {"status": "error", "code": "request_limit"}
            else:
                self.pending += 1
                self.max_pending = max(self.max_pending, self.pending)
                await asyncio.sleep(0.02)
                self.pending -= 1
                result = {"status": "ok", "value": 111.0}
            writer.write((json.dumps({"id": frame["id"], "result": result}) + "\n").encode())
            await writer.drain()

        while line := await reader.readline():
            asyncio.create_task(answer(json.loads(line)))
        writer.close()


async def test_two_hundred_concurrent_gets_all_answered():
    d = SlowDevice()
    await d.start()
    c = m4l.M4LClient(port_=d.port)
    try:
        results = await asyncio.gather(*[c.get("live_set", "Song", "tempo") for _ in range(200)])
    finally:
        await c.close()
        await d.stop()
    assert all(r["status"] == "ok" for r in results)
    assert d.refused == 0 and d.max_pending <= m4l.MAX_IN_FLIGHT < 64
