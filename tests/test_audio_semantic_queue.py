"""Semantic batch queueing must skip files that already have a result and an embedding.

Regression: start(semantic=True) without a path used to queue every indexed sample in path
order, so a follow-up batch for the files beyond a 10000-file limit re-queued the first batch.
"""

import json

from test_audio_taste import tone

from live_agent_bridge.audio.library import AudioLibrary
from live_agent_bridge.audio.semantic import weights_fingerprint
from live_agent_bridge.samples import Root, SampleIndex


def _library(tmp_path, monkeypatch):
    root = tmp_path / "samples"
    root.mkdir()
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}", encoding="utf-8")
    (model / "model.safetensors").write_bytes(b"fake weights")
    monkeypatch.setenv("AGENT_BRIDGE_CLAP_MODEL", str(model))
    monkeypatch.setenv("AGENT_BRIDGE_SAMPLE_DIRS", str(root))
    monkeypatch.setenv("APPDATA", str(tmp_path / "noappdata"))
    ix = SampleIndex(str(tmp_path / "samples.db"))
    ix.roots = lambda: [Root(str(root), "user_folders", "test", "env")]
    # AudioLibrary.validate_path builds its own SampleIndex; give it the same roots.
    monkeypatch.setattr(SampleIndex, "roots", lambda self: [Root(str(root), "user_folders", "test", "env")])
    return ix, root, model


def _queued(audio, job):
    with audio.connection() as db:
        return sorted(r[0] for r in db.execute("SELECT path FROM audio_queue WHERE job=?", (job,)))


def test_semantic_start_skips_files_with_result_and_embedding(tmp_path, monkeypatch):
    ix, root, model = _library(tmp_path, monkeypatch)
    for name in ("a.wav", "b.wav", "c.wav"):
        tone(root / name, hz=220 + len(name) * 10 + ord(name[0]))
    ix.scan()
    audio = AudioLibrary(ix.db_path)
    monkeypatch.setattr(audio, "_spawn", lambda job, semantic=False: {"id": job})
    fingerprint = weights_fingerprint(model)
    done = audio.analyze(str(root / "a.wav"))
    with audio.connection() as db:
        db.execute("INSERT INTO audio_embeddings VALUES (?,?,?)", (done["sha256"], fingerprint, json.dumps([1.0])))
    # b has a signal result but no embedding yet; c has nothing.
    audio.analyze(str(root / "b.wav"))

    job = audio.start(limit=10, semantic=True)["id"]
    queued = _queued(audio, job)
    assert [p.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for p in queued] == ["b.wav", "c.wav"]


def test_semantic_start_requeues_when_embedding_is_from_another_model(tmp_path, monkeypatch):
    ix, root, model = _library(tmp_path, monkeypatch)
    tone(root / "a.wav")
    ix.scan()
    audio = AudioLibrary(ix.db_path)
    monkeypatch.setattr(audio, "_spawn", lambda job, semantic=False: {"id": job})
    done = audio.analyze(str(root / "a.wav"))
    with audio.connection() as db:
        db.execute("INSERT INTO audio_embeddings VALUES (?,?,?)", (done["sha256"], "clap:other", json.dumps([1.0])))
    job = audio.start(limit=10, semantic=True)["id"]
    assert len(_queued(audio, job)) == 1


def test_semantic_start_reports_complete_when_nothing_is_left(tmp_path, monkeypatch):
    ix, root, model = _library(tmp_path, monkeypatch)
    tone(root / "a.wav")
    ix.scan()
    audio = AudioLibrary(ix.db_path)
    monkeypatch.setattr(audio, "_spawn", lambda job, semantic=False: {"id": job})
    done = audio.analyze(str(root / "a.wav"))
    with audio.connection() as db:
        db.execute("INSERT INTO audio_embeddings VALUES (?,?,?)", (done["sha256"], weights_fingerprint(model), "[1.0]"))
    assert audio.start(limit=10, semantic=True) == {"state": "complete", "queued": 0}


def test_weights_fingerprint_matches_clap_model_id_scheme(tmp_path):
    import hashlib

    (tmp_path / "pytorch_model.bin").write_bytes(b"x" * 10)
    (tmp_path / "model.safetensors").write_bytes(b"y" * 10)
    digest = hashlib.sha256()
    for name in sorted(["model.safetensors", "pytorch_model.bin"], key=lambda n: str(tmp_path / n)):
        digest.update((tmp_path / name).read_bytes())
    assert weights_fingerprint(tmp_path) == "clap:" + digest.hexdigest()
