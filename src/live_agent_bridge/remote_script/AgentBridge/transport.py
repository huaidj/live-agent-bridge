"""Socket transport for the AgentBridge Remote Script.

Single-threaded and polled from Live's main-thread tick. Live's embedded
Python only hands the GIL to other threads while the main thread runs Python,
so socket threads add a tick of latency on receive *and* on send. Polling
non-blocking sockets inside the tick means a request is read, executed and
answered within the same tick.

Framing is newline-delimited JSON (one object per line); clients may pipeline
any number of requests, matched by ``id``.
"""

import errno
import json
import socket

MAX_LINE = 16 * 1024 * 1024
MAX_READ_PER_TICK = 4 * 1024 * 1024
_WOULD_BLOCK = (errno.EAGAIN, errno.EWOULDBLOCK, getattr(errno, "WSAEWOULDBLOCK", 10035))


class Client(object):
    _next_id = 0

    def __init__(self, sock, addr, server):
        Client._next_id += 1
        self.cid = Client._next_id
        self.sock = sock
        self.addr = addr
        self.server = server
        self.alive = True
        self._in = b""
        self._out = bytearray()

    def send(self, obj):
        """Queue a message; written by ``Server.flush`` at the end of the tick."""
        if not self.alive:
            return
        try:
            data = json.dumps(obj, separators=(",", ":"), default=_json_default)
        except Exception as e:  # serialization bug must not kill the client
            data = json.dumps({"id": obj.get("id") if isinstance(obj, dict) else None,
                               "ok": False,
                               "error": {"type": "SerializationError", "message": str(e)}})
        self._out += data.encode("utf-8") + b"\n"

    def close(self):
        if not self.alive:
            return
        self.alive = False
        try:
            self.sock.close()
        except Exception:
            pass
        self.server._client_closed(self)

    def _read(self):
        got = 0
        while got < MAX_READ_PER_TICK:
            try:
                chunk = self.sock.recv(262144)
            except socket.error as e:
                if getattr(e, "errno", None) in _WOULD_BLOCK or isinstance(e, BlockingIOError):
                    break
                self.close()
                return
            if not chunk:
                self.close()
                return
            got += len(chunk)
            self._in += chunk
        if len(self._in) > MAX_LINE and b"\n" not in self._in:
            self.send({"id": None, "ok": False, "error": {"type": "ProtocolError", "message": "frame too large"}})
            self.close()
            return
        while True:
            nl = self._in.find(b"\n")
            if nl < 0:
                break
            line, self._in = self._in[:nl], self._in[nl + 1:]
            if not line.strip():
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
            except Exception as e:
                self.send({"id": None, "ok": False, "error": {"type": "ProtocolError", "message": "bad json: %s" % e}})
                continue
            if not isinstance(msg, dict):
                self.send({"id": None, "ok": False,
                           "error": {"type": "ProtocolError", "message": "request must be an object"}})
                continue
            self.server.on_request(self, msg)
            if not self.alive:
                return

    def _flush(self):
        while self._out:
            try:
                n = self.sock.send(self._out)
            except socket.error as e:
                if getattr(e, "errno", None) in _WOULD_BLOCK or isinstance(e, BlockingIOError):
                    return  # socket buffer full; rest goes out next tick
                self.close()
                return
            del self._out[:n]


class Server(object):
    """Non-blocking TCP server. Call ``poll()`` at the start and ``flush()`` at the end of each tick."""

    def __init__(self, host, port, on_request, on_connect=None, on_disconnect=None, log=None):
        self.host = host
        self.port = port
        self.on_request = on_request
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.log = log or (lambda *_: None)
        self.clients = {}
        self._sock = None
        self.running = False

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.listen(8)
        s.setblocking(False)
        self._sock = s
        self.port = s.getsockname()[1]
        self.running = True

    def poll(self):
        if not self.running:
            return
        while True:
            try:
                sock, addr = self._sock.accept()
            except socket.error:
                break
            try:
                sock.setblocking(False)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception:
                pass
            client = Client(sock, addr, self)
            self.clients[client.cid] = client
            if self.on_connect:
                try:
                    self.on_connect(client)
                except Exception as e:
                    self.log("on_connect failed: %s" % e)
        for client in list(self.clients.values()):
            client._read()

    def flush(self):
        for client in list(self.clients.values()):
            if client._out:
                client._flush()

    def _client_closed(self, client):
        self.clients.pop(client.cid, None)
        if self.on_disconnect:
            try:
                self.on_disconnect(client)
            except Exception:
                pass

    def get_client(self, cid):
        return self.clients.get(cid)

    def stop(self):
        self.running = False
        self.flush()
        for c in list(self.clients.values()):
            c.close()
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass


def _json_default(o):
    try:
        return list(o)
    except Exception:
        return str(o)
