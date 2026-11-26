import contextlib
import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from live_agent_bridge import server
from live_agent_bridge.samples import SampleIndex, parse_meta


@pytest.fixture
def sample_dir(tmp_path, monkeypatch):
    root = tmp_path / "samplepack"
    (root / "Drums").mkdir(parents=True)
    (root / "Loops").mkdir()
    for n in ("Kick 01.wav", "Snare Tight.wav", "Closed Hat 3.wav", "Clap Big.wav"):
        (root / "Drums" / n).write_bytes(b"RIFF")
    (root / "Loops" / "Funk Break 98 bpm Am.wav").write_bytes(b"RIFF")
    (root / "Loops" / "Pad Cmaj loop 120bpm.wav").write_bytes(b"RIFF")
    monkeypatch.setenv("AGENT_BRIDGE_SAMPLE_DIRS", str(root))
    monkeypatch.setenv("APPDATA", str(tmp_path / "noappdata"))
    ix = SampleIndex(str(tmp_path / "s.db"))
    # only the temp folder: drop auto-discovered User/Core Library roots for determinism
    roots = [r for r in ix.roots() if r.path.startswith(str(tmp_path))]
    ix.roots = lambda: roots
    ix.scan()
    monkeypatch.setattr(server, "_samples", ix)
    return root


@contextlib.asynccontextmanager
async def mcp_session(live, monkeypatch):
    from live_agent_bridge.client import LiveClient
    monkeypatch.setattr(server, "_client", LiveClient("127.0.0.1", live.port))
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as s:
        yield s
    await server._client.close()


async def tool(s, tool_name, **args):
    r = await s.call_tool(tool_name, args)
    text = r.content[0].text if r.content else ""
    assert not r.isError, text
    return json.loads(text) if text[:1] in "[{" else text


def test_parse_meta():
    m = parse_meta("x/Funk Break 98 bpm Am.wav")
    assert m["bpm"] == 98 and m["key"] == "Am" and "break" in m["tags"] and m["is_loop"]


async def test_tools_listed(live, monkeypatch):
    async with mcp_session(live, monkeypatch) as session:
        names = {t.name for t in (await session.list_tools()).tools}
        assert {"get_session", "batch", "lom_call", "sample_search", "write_chords", "load_drum_kit"} <= names


async def test_session_and_chords(live, monkeypatch):
    async with mcp_session(live, monkeypatch) as session:
        snap = await tool(session, "get_session")
        assert snap["tempo"] == 120.0
        res = await tool(session, "write_chords", progression="Am F C G", track="Bass", slot=1, name="Prog")
        assert res["created"] and res["notes"] == 12
        clip = live.song.tracks[1].clip_slots[1].clip
        assert clip.name == "Prog" and clip.length == 16
        res = await tool(session, "write_drum_pattern", track=0, slot=0,
                         rows={"kick": "x...x...x...x...", "hat": "..x...x...x...x."}, bars_repeat=2)
        assert res["notes"] == 16 and live.song.tracks[0].clip_slots[0].clip.length == 8


async def test_sample_search_and_drum_kit(live, sample_dir, monkeypatch):
    from fake_live import BrowserItem
    drums = live.app.browser.user_folders[0].children[0]
    drums.children += [BrowserItem(n, loadable=True) for n in ("Snare Tight.wav", "Closed Hat 3.wav")]
    async with mcp_session(live, monkeypatch) as session:
        r = await tool(session, "sample_search", query="funk", bpm=98)
        assert r["results"][0]["name"].startswith("Funk Break")
        r = await tool(session, "sample_search", key="C", loop=True)  # relative minor matches too
        assert {x["name"] for x in r["results"]} == {"Pad Cmaj loop 120bpm.wav", "Funk Break 98 bpm Am.wav"}
        kit = await tool(session, "load_drum_kit", track=0, pads={"kick": "kick", "snare": "snare tight",
                                                                   "hat": "closed hat"})
        assert kit["completed"] == 4, kit
        rack = live.song.tracks[0].devices[0]
        assert rack.drum_pads[36].chains == ["Kick 01.wav"]
        assert rack.drum_pads[38].chains == ["Snare Tight.wav"]
        assert rack.drum_pads[42].chains == ["Closed Hat 3.wav"]


async def test_load_sample_to_arrangement(live, sample_dir, monkeypatch):
    async with mcp_session(live, monkeypatch) as session:
        p = str(sample_dir / "Loops" / "Funk Break 98 bpm Am.wav")
        r = await tool(session, "load_sample", path=p, target="arrangement", track=1, start_time=8)
        assert live.song.tracks[1].arrangement_clips[0].file_path == p and r["type"] == "Clip"


async def test_tool_error_is_reported(live, monkeypatch):
    async with mcp_session(live, monkeypatch) as session:
        r = await session.call_tool("get_track", {"track": 42})
        assert r.isError and "out of range" in r.content[0].text


def test_sample_index_survives_corruption_and_concurrency(tmp_path):
    import threading
    db = tmp_path / "s.db"
    db.write_bytes(b"this is not a sqlite database" * 100)
    ix = SampleIndex(str(db))
    assert ix.recovered_from and ix.count == 0  # damaged cache moved aside and rebuilt
    root = tmp_path / "lib"
    root.mkdir()
    for i in range(30):
        (root / f"Kick {i}.wav").write_bytes(b"RIFF")
    ix.roots = lambda: [__import__("live_agent_bridge.samples", fromlist=["Root"]).Root(str(root), "user_folders",
                                                                                         "lib", "test")]
    errors = []

    def run(f):
        try:
            f()
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))
    threads = [threading.Thread(target=run, args=(ix.scan,)) for _ in range(3)]
    threads += [threading.Thread(target=run, args=(lambda: ix.search("kick"),)) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and ix.count == 30
    assert ix._last_scan_time() > 0  # shared with other processes, so they don't rescan
