"""Property observation. Listener callbacks only mark a subscription dirty;
the scheduler reads the value once per tick and pushes one coalesced event,
so a fast-changing property (e.g. playing_position) cannot flood the socket."""

import time

from .lom import LomError, check_name

MAX_SUBS_PER_CLIENT = 256


class Subscription(object):
    __slots__ = ("sid", "cid", "obj", "prop", "path", "callback", "dirty")

    def __init__(self, sid, cid, obj, prop, path):
        self.sid, self.cid, self.obj, self.prop, self.path = sid, cid, obj, prop, path
        self.dirty = False
        self.callback = None


class Events(object):
    def __init__(self, lom, send):
        self.lom = lom
        self.send = send  # send(cid, message)
        self.subs = {}
        self._next = 0

    def subscribe(self, cid, path, prop):
        check_name(prop)
        if sum(1 for s in self.subs.values() if s.cid == cid) >= MAX_SUBS_PER_CLIENT:
            raise LomError("too many subscriptions on this connection")
        obj = self.lom.resolve(path)
        add = getattr(obj, "add_%s_listener" % prop, None)
        if add is None:
            raise LomError("%s.%s is not observable" % (type(obj).__name__, prop))
        self._next += 1
        sub = Subscription(self._next, cid, obj, prop, self.lom.path_of(obj) or str(path))

        def cb(*_):
            sub.dirty = True
        sub.callback = cb
        add(cb)
        self.subs[sub.sid] = sub
        return {"sub": sub.sid, "path": sub.path, "prop": prop,
                "value": self.lom.ser(getattr(obj, prop))}

    def _remove(self, sub):
        self.subs.pop(sub.sid, None)
        try:
            rem = getattr(sub.obj, "remove_%s_listener" % sub.prop)
            has = getattr(sub.obj, "%s_has_listener" % sub.prop, None)
            if has is None or has(sub.callback):
                rem(sub.callback)
        except Exception:
            pass  # object already deleted in Live

    def unsubscribe(self, cid, sid=None):
        victims = [s for s in list(self.subs.values()) if s.cid == cid and (sid is None or s.sid == sid)]
        for s in victims:
            self._remove(s)
        return {"removed": len(victims)}

    def drop_client(self, cid):
        for s in [s for s in list(self.subs.values()) if s.cid == cid]:
            self._remove(s)

    def flush(self):
        now = time.time()
        for sub in list(self.subs.values()):
            if not sub.dirty:
                continue
            sub.dirty = False
            try:
                val = self.lom.ser(getattr(sub.obj, sub.prop))
            except Exception as e:
                self.send(sub.cid, {"event": "gone", "sub": sub.sid, "path": sub.path, "prop": sub.prop,
                                    "error": str(e)})
                self._remove(sub)
                continue
            self.send(sub.cid, {"event": "change", "sub": sub.sid, "path": sub.path, "prop": sub.prop,
                                "value": val, "t": now})

    def close(self):
        for s in list(self.subs.values()):
            self._remove(s)
