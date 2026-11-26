"""sample_search output must stay small enough for an agent and list identical content once.

Real case: 25 results came back as 64,000 characters (full analysis per hit, repeated
limitations text), and every SampleRadar file appeared twice because of a 'SampleRadar Copy'.
"""

import json
import shutil

from test_audio_taste import click_loop
from test_sample_index_roots import _index

from live_agent_bridge.audio.library import AudioLibrary


def _analyze_all(ix, root):
    audio = AudioLibrary(ix.db_path)
    for p in root.rglob("*.wav"):
        audio.analyze(str(p))


def test_default_results_are_compact_and_detail_is_opt_in(tmp_path, monkeypatch):
    ix = _index(tmp_path, monkeypatch)
    root = tmp_path / "pack"
    root.mkdir()
    click_loop(root / "loop_120_a.wav", bpm=120)
    ix.add_root(str(root))
    ix.scan()
    _analyze_all(ix, root)
    compact = ix.search("loop")
    hit = compact["results"][0]
    assert "limitations" not in hit["audio"] and "pitch_class_profile" not in hit["audio"]
    assert "sha256" not in hit["audio"] and "key_candidates" not in hit["audio"]
    assert {"bpm", "key", "instrument", "loop"} <= set(hit["audio"])
    assert compact["limitations"]                      # stated once for the whole response
    full = ix.search("loop", detail=True)["results"][0]
    assert "limitations" in full["audio"] and "sha256" in full["audio"]
    assert len(json.dumps(hit)) < len(json.dumps(full))


def test_identical_content_is_listed_once_with_copies(tmp_path, monkeypatch):
    ix = _index(tmp_path, monkeypatch)
    root = tmp_path / "pack"
    (root / "Copy").mkdir(parents=True)
    click_loop(root / "loop_120_a.wav", bpm=120)
    shutil.copy(root / "loop_120_a.wav", root / "Copy" / "loop_120_a.wav")
    click_loop(root / "loop_121_b.wav", bpm=121)
    ix.add_root(str(root))
    ix.scan()
    _analyze_all(ix, root)
    r = ix.search("loop")
    names = sorted(h["name"] for h in r["results"])
    assert names == ["loop_120_a.wav", "loop_121_b.wav"]
    assert r["total"] == 2
    a = next(h for h in r["results"] if h["name"] == "loop_120_a.wav")
    assert a["copies"] == 2
    # the copy is still reachable when a filter selects only its folder
    only = ix.search("loop", folder="Copy")
    assert [h["path"] for h in only["results"]] == [str(root / "Copy" / "loop_120_a.wav")]
