"""Semantic search returns one hit per audio content, listing identical copies separately.

Real case: the same kick existed in three sample folders and filled 3 of 8 result slots.
"""

import json
import shutil

from test_audio_semantic_queue import _library
from test_audio_taste import tone

from live_agent_bridge.audio.library import AudioLibrary


class FakeModel:
    model_id = "clap:fake"

    def text(self, description):
        return [1.0, 0.0]


def test_identical_files_collapse_into_one_hit(tmp_path, monkeypatch):
    ix, root, _ = _library(tmp_path, monkeypatch)
    tone(root / "kick.wav", hz=60)
    (root / "copy").mkdir()
    shutil.copy(root / "kick.wav", root / "copy" / "kick.wav")
    tone(root / "hat.wav", hz=8000)
    ix.scan()
    audio = AudioLibrary(ix.db_path)
    vectors = {"kick.wav": [1.0, 0.0], "hat.wav": [0.0, 1.0]}
    for path in (root / "kick.wav", root / "copy" / "kick.wav", root / "hat.wav"):
        r = audio.analyze(str(path))
        with audio.connection() as db:
            db.execute("INSERT OR REPLACE INTO audio_embeddings VALUES (?,?,?)",
                       (r["sha256"], FakeModel.model_id, json.dumps(vectors[path.name])))
    out = audio._semantic_search_local(description="kick", limit=5, model=FakeModel())
    names = [h["path"] for h in out["results"]]
    assert len(names) == 2                                  # kick once, hat once
    top = out["results"][0]
    assert top["path"].endswith("kick.wav")
    assert len(top["same_content_paths"]) == 1
    assert out["results"][1]["path"].endswith("hat.wav")
    assert "same_content_paths" not in out["results"][1]
