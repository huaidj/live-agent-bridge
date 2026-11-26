import hashlib
import json
import time

import pytest

np = pytest.importorskip("numpy")
sf = pytest.importorskip("soundfile")

from live_agent_bridge.audio.reference_worker import analyze_song, run
from live_agent_bridge.audio.references import ReferenceLibrary
from live_agent_bridge.taste.cli import export_memory
from live_agent_bridge.taste.store import TasteStore


class FakeAudioModel:
    model_id = "fixture-model"

    def texts(self, descriptions):
        # Distinct known vectors allow exact pipeline checks, without claiming real semantic accuracy.
        return [[1.0, 0.0] if "house" in text.lower() else [0.0, 1.0] for text in descriptions]

    def audio_array(self, samples):
        assert isinstance(samples, np.ndarray) and samples.ndim == 1
        return [1.0, 0.0]


def make_audio(path, seconds=12):
    data = 0.2 * np.sin(2 * np.pi * 440 * np.arange(seconds * 22050) / 22050)
    sf.write(path, data, 22050)


def insert_job(library, path, remember=True):
    now = time.time()
    with library.connection() as db:
        db.execute("INSERT INTO reference_jobs VALUES (?,?,?,'Only the bass',?,'queued',?,?,NULL,NULL,NULL)",
                   ("job", str(path), "Favorite reference", int(remember), now, now))


def test_segment_coverage_filename_independence_and_real_measurements(tmp_path):
    one = tmp_path / "wrong techno 99bpm.wav"
    two = tmp_path / "totally different name.wav"
    make_audio(one)
    two.write_bytes(one.read_bytes())
    model = FakeAudioModel()
    first = analyze_song(str(one), model)
    second = analyze_song(str(two), model)
    assert first == second
    assert first["observations"]["genre"][0]["label"] == "house"
    assert first["segments"][0]["start_seconds"] == 0
    assert first["segments"][-1]["end_seconds"] == 12
    assert first["measurements"]["bpm"] is None
    assert first["filename_used_by_model"] is False


def test_reference_selection_is_separate_from_model_traits_and_portable(tmp_path):
    source = tmp_path / "favorite.wav"
    make_audio(source)
    before = source.stat().st_mtime_ns, hashlib.sha256(source.read_bytes()).hexdigest()
    library = ReferenceLibrary(tmp_path / "data")
    insert_job(library, source)
    run(library, "job", FakeAudioModel)
    result = library.status("job", True)
    assert result["state"] == "complete", result
    assert result["analysis"]["memory"]["confirmed_feature_preferences"] == 0
    store = TasteStore(library.directory / "taste.db")
    profile = store.profile()
    assert profile["preferences"] == []
    assert profile["references"][0]["observations"]["genre"][0]["label"] == "house"
    assert profile["references"][0]["user_note"] == "Only the bass"
    assert store.recall("house")["results"][0]["kind"] == "fact"
    assert str(source) not in json.dumps(profile)
    assert before == (source.stat().st_mtime_ns, hashlib.sha256(source.read_bytes()).hexdigest())
    restored = TasteStore(tmp_path / "restored.db")
    restored.import_data(store.export())
    assert restored.profile() == profile
    export_memory(store, str(tmp_path / "export.json"))
    assert "Favorite reference" in (tmp_path / "export.md").read_text(encoding="utf-8")
    assert "house" in (tmp_path / "export.md").read_text(encoding="utf-8")


def test_test_only_and_cancellation_do_not_write_user_memory(tmp_path):
    source = tmp_path / "fixture.wav"
    make_audio(source)
    library = ReferenceLibrary(tmp_path / "data")
    insert_job(library, source, False)
    run(library, "job", FakeAudioModel)
    assert library.status("job")["state"] == "complete"
    assert not (library.directory / "taste.db").exists()
    with library.connection() as db:
        db.execute("UPDATE reference_jobs SET state='queued' WHERE id='job'")
    library.cancel("job")
    run(library, "job", lambda: pytest.fail("Cancelled jobs must not load the model"))
    assert library.status("job")["state"] == "cancelled"


def test_unsupported_and_relative_inputs_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        ReferenceLibrary.validate_path("song.wav")
    text = tmp_path / "private.txt"
    text.write_text("private")
    with pytest.raises(ValueError, match="audio"):
        ReferenceLibrary.validate_path(str(text))
