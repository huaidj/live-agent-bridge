import sys
import threading
import time
import types
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src" / "live_agent_bridge" / "remote_script"))

# Stub Live's framework so the Remote Script package imports outside Live.
fw = types.ModuleType("_Framework")
cs = types.ModuleType("_Framework.ControlSurface")
cs.ControlSurface = object
sys.modules.setdefault("_Framework", fw)
sys.modules.setdefault("_Framework.ControlSurface", cs)

import fake_live  # noqa: E402


class LiveHarness:
    """Runs a Core with a thread standing in for Live's main-thread timer."""

    def __init__(self, tick_interval=0.1):
        self.song, self.app = fake_live.install()
        from AgentBridge.core import Core
        self.core = Core(lambda: self.song, lambda: self.app, port=0, live_version="12.4.16")
        self.core.start()
        self.port = self.core.server.port
        self.tick_interval = tick_interval
        self.paused = False
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        while not self._stop.is_set():
            if not self.paused:
                self.core.tick()
            time.sleep(self.tick_interval)

    def close(self):
        self._stop.set()
        self._t.join(1)
        self.core.stop()


@pytest.fixture
def live():
    h = LiveHarness()
    yield h
    h.close()


@pytest.fixture
async def client(live):
    from live_agent_bridge.client import LiveClient
    c = LiveClient("127.0.0.1", live.port)
    await c.connect()
    yield c
    await c.close()
