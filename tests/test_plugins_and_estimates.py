"""Plug-in understanding/operation, tempo/key estimation and the warm CLAP worker."""

import json
import sys
import textwrap

import pytest

from live_agent_bridge.client import LiveError

np = pytest.importorskip("numpy")
sf = pytest.importorskip("soundfile")

SR = 22050


# ------------------------------------------------------------------ tempo / key estimation

def _env(n, k):
    return np.exp(-np.arange(n) / (k * SR))


def _uk_bass(path, bpm=140, with_tones=True, seconds=12):
    x = np.zeros(SR * seconds)
    beat = 60 / bpm
    for b in np.arange(0, seconds - 1, beat / 4):
        step = int(round(b / (beat / 4))) % 16
        a = int(b * SR)
        n = min(int(0.15 * SR), len(x) - a)
        if step in (0, 7, 10):  # syncopated kick: defeats the old "regular intervals" rule
            x[a:a + n] += np.sin(2 * np.pi * 55 * np.arange(n) / SR) * _env(n, .05)
        if step % 2 == 1:
            x[a:a + n] += np.random.default_rng(step).normal(0, .15, n) * _env(n, .01)
    if with_tones:
        notes = [57, 60, 64, 69, 64, 60, 57, 62, 65, 69]  # A minor
        for i, b in enumerate(np.arange(0, seconds - 0.5, beat)):
            f = 440 * 2 ** ((notes[i % len(notes)] - 69) / 12)
            a = int(b * SR)
            n = min(int(beat * SR), len(x) - a)
            t = np.arange(n) / SR
            x[a:a + n] += 0.2 * (np.sin(2 * np.pi * f * t) + .5 * np.sin(4 * np.pi * f * t)) * _env(n, .3)
    sf.write(path, x.astype(np.float32), SR)


def test_tempo_and_key_on_syncopated_music(tmp_path):
    from live_agent_bridge.audio.features import analyze_file

    _uk_bass(tmp_path / "a.wav")
    r = analyze_file(str(tmp_path / "a.wav"))
    assert r["bpm"] == pytest.approx(140, abs=1.5) and 0 < r["bpm_confidence"] < 1
    assert r["key"] == "A minor" and r["key_confidence"] > 0.1
    assert r["key_candidates"][0]["key"] == "A minor"


def test_drums_have_tempo_but_no_key_and_chords_no_tempo(tmp_path):
    from live_agent_bridge.audio.features import analyze_file

    _uk_bass(tmp_path / "d.wav", with_tones=False)
    drums = analyze_file(str(tmp_path / "d.wav"))
    assert drums["bpm"] == pytest.approx(140, abs=1.5) and drums["key"] is None
    t = np.arange(SR * 12) / SR
    chord = sum(np.sin(2 * np.pi * 440 * 2 ** ((m - 69) / 12) * t) for m in (60, 64, 67)) * .2
    sf.write(tmp_path / "c.wav", chord.astype(np.float32), SR)
    held = analyze_file(str(tmp_path / "c.wav"))
    assert held["bpm"] is None  # a sustained chord has no pulse
    assert held["key_candidates"][0]["key"] == "C major"


def test_half_time_pattern_reports_felt_tempo(tmp_path):
    from live_agent_bridge.audio.features import analyze_file

    x = np.zeros(SR * 12)
    beat = 60 / 124
    for i, b in enumerate(np.arange(0, 11, beat)):
        a = int(b * SR)
        n = min(int(0.12 * SR), len(x) - a)
        amp = 1.0 if i % 2 == 0 else 0.35  # strong every two beats, weak in between
        x[a:a + n] += amp * np.sin(2 * np.pi * 60 * np.arange(n) / SR) * _env(n, .04)
    sf.write(tmp_path / "h.wav", x.astype(np.float32), SR)
    r = analyze_file(str(tmp_path / "h.wav"))
    assert r["bpm"] == pytest.approx(124, abs=1.5)
    assert any(abs(a - 62) < 1.5 for a in r["bpm_alternatives"])


def test_ambiguous_key_is_not_asserted(monkeypatch, tmp_path):
    import live_agent_bridge.audio.features as F

    monkeypatch.setattr(F, "estimate_key", lambda mono, sr: {
        "key": "A# major", "confidence": 0.02, "candidates": [{"key": "A# major", "correlation": .41}],
        "profile": {}})
    _uk_bass(tmp_path / "a.wav")
    r = F.analyze_file(str(tmp_path / "a.wav"))
    assert r["key"] is None and r["key_note"].startswith("ambiguous") and r["key_candidates"]


def test_analysis_window_offset(tmp_path):
    from live_agent_bridge.audio.features import analyze_file

    _uk_bass(tmp_path / "a.wav", seconds=20)
    r = analyze_file(str(tmp_path / "a.wav"), max_seconds=8, offset_seconds=6)
    assert r["analyzed_from_seconds"] == pytest.approx(6, abs=0.01)
    assert r["analyzed_seconds"] == pytest.approx(8, abs=0.01)


# ------------------------------------------------------------------ plug-in understanding (MCP side)

def test_roles_groups_and_learned_knowledge(tmp_path, monkeypatch):
    from live_agent_bridge import plugins as P

    monkeypatch.setenv("AGENT_BRIDGE_DATA_DIR", str(tmp_path))
    assert P.role_of("LFO 1 Frequency") == "lfo_rate" and P.role_of("Filter 1 Cutoff") == "cutoff"
    assert P.role_of("Lo Cut") == "low_cut" and P.group_of("Hi Cut") == "filter"
    assert P.group_of("Oscillator 3 Level") == "Oscillator 3"
    inspect = {"plugin_name": "Synth", "exposed_parameters": [
        {"name": "Device On"}, {"name": "Cut A"}, {"name": "Filter 1 Cutoff"}, {"name": "Filter 2 Cutoff"}],
        "unexposed_parameter_names": ["Delay Feedback", "Env 1 Attack"]}
    u = P.understand(json.loads(json.dumps(inspect)))["understanding"]
    assert u["controllable_roles"]["cutoff"] == ["Filter 1 Cutoff", "Filter 2 Cutoff"]
    assert u["roles_needing_configure"]["attack"] == ["Env 1 Attack"]
    with pytest.raises(ValueError, match="Several"):
        P.resolve_role(json.loads(json.dumps(inspect)), "cutoff")
    with pytest.raises(ValueError, match="configure"):
        P.resolve_role(json.loads(json.dumps(inspect)), "feedback")
    # a user-taught meaning wins and is never overwritten by an inference
    P.learn("Synth", "Cut A", "cutoff", "main filter", "user")
    kept = P.learn("Synth", "Cut A", "drive", "", "inference")
    assert kept["kept"]["role"] == "cutoff"
    assert P.resolve_role(json.loads(json.dumps(inspect)), "cutoff") == "Cut A"


# ------------------------------------------------------------------ plug-in ops in the Remote Script

@pytest.fixture
def plugin_track(live):
    import fake_live

    class Plugin(fake_live.Device):
        def __init__(self, parent):
            fake_live.Device.__init__(self, parent, "Synth")
            self.class_name, self.class_display_name = "PluginDevice", "Synth"
            self.parameters = [fake_live.DeviceParameter(self, "Device On", 0, 1, 1, True, ["Off", "On"]),
                               fake_live.DeviceParameter(self, "Filter Cutoff", 0, 1, 0.5),
                               fake_live.DeviceParameter(self, "Mix", 0, 1, 1.0,
                                                         fmt=lambda v: "%d%%" % round(v * 100))]
            self.presets, self.selected_preset_index, self.is_editor_open = ["Init", "Warm Pad"], 0, False

        def get_parameter_names(self, begin=0, end=-1):
            names = ["Device On", "Filter Cutoff", "Mix", "Env 1 Attack", "Delay Feedback"]
            return names[begin:None if end == -1 else end]

    t = live.song.tracks[0]
    t.devices.append(Plugin(t))
    return t


async def test_plugin_inspect_set_snapshot_preset(client, live, plugin_track):
    data = await client.request("plugin.inspect", {"track": 0, "index": 0})
    assert data["kind"] == "plugin" and data["total_parameters"] == 5 and data["exposed_count"] == 3
    assert data["unexposed_parameter_names"] == ["Env 1 Attack", "Delay Feedback"] and data["how_to_expose"]
    with pytest.raises(LiveError, match="not exposed"):
        await client.request("plugin.set", {"track": 0, "index": 0, "changes": [{"name": "attack"}]})
    await client.request("plugin.snapshot", {"track": 0, "index": 0, "action": "save", "name": "A"})
    r = await client.request("plugin.set", {"track": 0, "index": 0, "changes": [
        {"name": "Filter Cutoff", "delta_normalized": -0.25}, {"name": "Mix", "delta_normalized": 0.5}]})
    assert r[0]["value"] == pytest.approx(0.25) and r[1]["note"] == "already at its maximum"
    diff = await client.request("plugin.snapshot", {"track": 0, "index": 0, "action": "diff", "name": "A"})
    assert diff["count"] == 1
    await client.request("plugin.snapshot", {"track": 0, "index": 0, "action": "morph", "name": "A", "amount": 0.5})
    assert plugin_track.devices[0].parameters[1].value == pytest.approx(0.375)
    await client.request("plugin.snapshot", {"track": 0, "index": 0, "action": "recall", "name": "A"})
    assert plugin_track.devices[0].parameters[1].value == pytest.approx(0.5)
    p = await client.request("plugin.preset", {"track": 0, "index": 0, "action": "select", "preset": "warm"})
    assert p["selected_preset_index"] == 1
    inset = await client.request("plugin.list_in_set", {})
    assert inset["devices"][0]["exposed"] == 3 and inset["devices"][0]["total"] == 5
    # deleting the last snapshot leaves no empty per-device entry in the saved set data
    await client.request("plugin.snapshot", {"track": 0, "index": 0, "action": "delete", "name": "A"})
    from live_agent_bridge.remote_script.AgentBridge.plugins import SNAPSHOT_KEY
    assert live.song.get_data(SNAPSHOT_KEY, None) in ("{}", None)


# ------------------------------------------------------------------ warm CLAP worker

def test_warm_worker_reuses_one_process_and_restarts(tmp_path):
    from live_agent_bridge.audio import clap_service

    worker = tmp_path / "fakeworker.py"
    worker.write_text(textwrap.dedent("""
        import json, os, sys
        print(json.dumps({"ready": True, "model": "fake"}), flush=True)
        for line in sys.stdin:
            req = json.loads(line)
            if req.get("description") == "die":
                sys.exit(3)
            print(json.dumps({"id": req["id"], "pid": os.getpid(), "results": [], "total": 0}), flush=True)
    """))
    svc = clap_service.ClapService(str(tmp_path / "x.db"), sys.executable, None)
    svc._start_args = None
    original = clap_service.ClapService._start

    def start(self):
        import subprocess
        self.process = subprocess.Popen([sys.executable, str(worker)], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, text=True, bufsize=1)
        assert self._read_line(10)["ready"]
    clap_service.ClapService._start = start
    try:
        a = svc.search({"description": "x", "limit": 1})
        b = svc.search({"description": "y", "limit": 1})
        assert a["pid"] == b["pid"] and a["worker"] == "warm"
        with pytest.raises(ValueError):
            svc.search({"description": "die"})  # exits mid-request: second attempt also dies
        c = svc.search({"description": "z", "limit": 1})
        assert c["pid"] != a["pid"]  # transparently restarted
    finally:
        clap_service.ClapService._start = original
        svc.stop()
