"""Warm CLAP worker kept by the MCP server process.

The first semantic search starts ``search_worker --serve`` in the configured inference Python and
waits for the model to load; later searches reuse it (tens of milliseconds instead of seconds).
The worker exits after ``IDLE_SECONDS`` without use, or when the server exits, freeing its memory.
"""

from __future__ import annotations

import atexit
import itertools
import json
import os
import subprocess
import threading
import time
from pathlib import Path

IDLE_SECONDS = float(os.environ.get("AGENT_BRIDGE_CLAP_IDLE", "600"))
LOAD_TIMEOUT = 180.0
REQUEST_TIMEOUT = 120.0

_services: dict[tuple, "ClapService"] = {}
_registry_lock = threading.Lock()


class ClapService:
    def __init__(self, db_path: str, python: str, environment: dict):
        self.db_path, self.python, self.environment = db_path, python, environment
        self.process: subprocess.Popen | None = None
        self.model: str | None = None
        self.last_used = 0.0
        self.lock = threading.Lock()
        self.ids = itertools.count(1)
        self._reaper = threading.Thread(target=self._reap, daemon=True)
        self._reaper.start()

    # ------------------------------------------------------------ lifecycle
    def _start(self) -> None:
        args = [self.python, "-m", "live_agent_bridge.audio.search_worker", "--db", self.db_path, "--serve"]
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                        env=self.environment, bufsize=1, **options)
        hello = self._read_line(LOAD_TIMEOUT)
        if not hello.get("ready"):
            self.stop()
            raise ValueError(hello.get("error") or "Local CLAP worker failed to load the model")
        self.model = hello.get("model")

    def stop(self) -> None:
        proc, self.process = self.process, None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    def _reap(self) -> None:
        while True:
            time.sleep(15)
            with self.lock:
                if self.process is not None and time.time() - self.last_used > IDLE_SECONDS:
                    self.stop()

    def _read_line(self, timeout: float) -> dict:
        box: dict = {}

        def read():
            try:
                box["line"] = self.process.stdout.readline()
            except (OSError, ValueError):
                box["line"] = ""
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(timeout)
        if reader.is_alive():
            self.stop()
            raise TimeoutError("Local CLAP worker did not answer in time")
        line = box.get("line", "")
        if not line:
            self.stop()
            raise ValueError("Local CLAP worker exited")
        return json.loads(line)

    # ------------------------------------------------------------ requests
    def search(self, request: dict) -> dict:
        with self.lock:
            self.last_used = time.time()
            for attempt in (1, 2):  # one transparent restart if the worker died
                if self.process is None or self.process.poll() is not None:
                    self.process = None
                    self._start()
                rid = next(self.ids)
                try:
                    self.process.stdin.write(json.dumps(dict(request, id=rid)) + "\n")
                    self.process.stdin.flush()
                    answer = self._read_line(REQUEST_TIMEOUT)
                except (OSError, ValueError):
                    if attempt == 2:
                        raise
                    self.stop()
                    continue
                self.last_used = time.time()
                answer.pop("id", None)
                answer["worker"] = "warm"
                return answer
        raise ValueError("Local CLAP worker failed")


def service_for(db_path: str, python: str, environment: dict) -> ClapService:
    key = (str(Path(db_path).resolve()), python)
    with _registry_lock:
        svc = _services.get(key)
        if svc is None:
            svc = _services[key] = ClapService(db_path, python, environment)
        return svc


@atexit.register
def _shutdown() -> None:
    for svc in list(_services.values()):
        svc.stop()
