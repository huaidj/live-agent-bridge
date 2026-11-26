"""AgentBridge - Ableton Live Remote Script exposing the Live API to AI agents.

Install into  <User Library>/Remote Scripts/AgentBridge  and pick "AgentBridge"
as a Control Surface in Settings > Link, Tempo & MIDI (Input/Output: None).
"""

import importlib
import json
import os
import sys

from _Framework.ControlSurface import ControlSurface

from . import core as _core

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONFIG = os.path.join(_HERE, "config.json")
# reload order: dependencies first
_MODULES = ("lom", "transport", "events", "ops", "browser", "ops_ext", "plugins", "core")
DEFAULT_TIMER_MS = 1


def create_instance(c_instance):
    return AgentBridge(c_instance)


def _load_config():
    try:
        with open(_CONFIG, "r") as f:
            return json.load(f)
    except Exception:
        return {}


class AgentBridge(ControlSurface):
    def __init__(self, c_instance):
        ControlSurface.__init__(self, c_instance)
        self._timer = None
        self._timer_ms = int(_load_config().get("timer_interval_ms", DEFAULT_TIMER_MS))
        app = self.application()
        try:
            self._live_version = "%d.%d.%d" % (app.get_major_version(), app.get_minor_version(),
                                               app.get_bugfix_version())
        except Exception:
            self._live_version = None
        self._core = None
        self._start_core()
        self._start_fast_timer()

    # ------------------------------------------------------------ core lifecycle
    def _start_core(self):
        port = int(os.environ.get("AGENT_BRIDGE_PORT", _core.DEFAULT_PORT))
        c = _core.Core(self.song, self.application, log=self.log_message, port=port,
                       live_version=self._live_version)
        c.hooks = {"reload": self._request_reload, "timer": self._set_timer_interval,
                   "message": self.show_message}
        c.tick_mode = "timer" if self._timer is not None else "update_display"
        try:
            c.start()
            self.show_message("AgentBridge %s ready on port %d" % (_core.VERSION, c.server.port))
        except Exception as e:
            self.log_message("AgentBridge failed to start: %s" % e)
            self.show_message("AgentBridge: port %d busy (%s)" % (port, e))
        self._core = c

    def _request_reload(self):
        # Runs at the start of the next tick, after the reply has been flushed.
        self._core.control.append(self._reload)

    def _reload(self):
        old = self._core
        try:
            old.stop()
        except Exception as e:
            self.log_message("AgentBridge stop before reload failed: %s" % e)
        global _core
        pkg = __name__
        try:
            for name in _MODULES:
                mod = sys.modules.get(pkg + "." + name)
                if mod is not None:
                    importlib.reload(mod)
            _core = sys.modules[pkg + ".core"]
            self.log_message("AgentBridge reloaded code, version %s" % _core.VERSION)
        except Exception as e:
            self.log_message("AgentBridge reload failed, restarting previous code: %s" % e)
        self._start_core()

    # ------------------------------------------------------------ timer
    def _start_fast_timer(self):
        """Use Live.Base.Timer for fast ticks when this Live build has it;
        otherwise fall back to update_display (~10 Hz)."""
        try:
            import Live
            timer_cls = getattr(Live.Base, "Timer", None)
        except Exception:
            timer_cls = None
        if timer_cls is None:
            self.log_message("AgentBridge: Live.Base.Timer unavailable; ticking on update_display")
            return
        for kwargs in ({"callback": self._on_timer, "interval": self._timer_ms, "repeat": True},
                       {"callback": self._on_timer, "interval": self._timer_ms}):
            try:
                t = timer_cls(**kwargs)
                t.start()
                self._timer = t
                if self._core is not None:
                    self._core.tick_mode = "timer"
                self.log_message("AgentBridge: fast timer active, interval=%d" % self._timer_ms)
                return
            except Exception as e:
                self.log_message("AgentBridge: Timer%r failed: %s" % (sorted(kwargs), e))

    def _stop_timer(self):
        if self._timer is not None:
            try:
                self._timer.stop()
            except Exception:
                pass
            self._timer = None

    def _set_timer_interval(self, ms):
        self._timer_ms = max(1, int(ms))
        try:
            cfg = _load_config()
            cfg["timer_interval_ms"] = self._timer_ms
            with open(_CONFIG, "w") as f:
                json.dump(cfg, f)
        except Exception as e:
            self.log_message("AgentBridge: could not save config: %s" % e)
        self._stop_timer()
        self._start_fast_timer()
        return self._timer is not None

    def _on_timer(self):
        try:
            self._core.tick()
        except Exception as e:
            self.log_message("AgentBridge tick error: %s" % e)

    def update_display(self):
        ControlSurface.update_display(self)
        try:
            self._core.tick()
        except Exception as e:
            self.log_message("AgentBridge tick error: %s" % e)

    def disconnect(self):
        self._stop_timer()
        try:
            self._core.stop()
        except Exception as e:
            self.log_message("AgentBridge stop error: %s" % e)
        ControlSurface.disconnect(self)
