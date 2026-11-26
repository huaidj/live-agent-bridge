import shutil
import time

import pytest

np = pytest.importorskip("numpy")
sf = pytest.importorskip("soundfile")

from live_agent_bridge.audio.features import analyze_file
from live_agent_bridge.audio.library import AudioLibrary
from live_agent_bridge.audio.worker import run
from live_agent_bridge.samples import Root, SampleIndex


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / "samples"
    root.mkdir()
    monkeypatch.setenv("AGENT_BRIDGE_SAMPLE_DIRS", str(root))
    monkeypatch.setenv("APPDATA", str(tmp_path / "noappdata"))
    ix = SampleIndex(str(tmp_path / "samples.db"))
    ix.roots = lambda: [Root(str(root), "user_folders", "test", "env")]
    return ix, root


def tone(path, seconds=2, hz=440):
    sr = 22050
    sf.write(path, 0.5 * np.sin(2 * np.pi * hz * np.arange(int(sr * seconds)) / sr), sr, subtype="PCM_16")


def click_loop(path, seconds=8, bpm=120):
    sr = 22050
    signal = np.zeros(sr * seconds, dtype=np.float32)
    for onset in np.arange(0.1, seconds, 60 / bpm):
        at = int(onset * sr)
        n = min(256, len(signal) - at)
        signal[at:at + n] = np.hanning(n) * 0.8
    sf.write(path, signal, sr)


def test_audio_measures_signal_without_believing_filename(library):
    ix, root = library
    click_loop(root / "Totally Wrong 140bpm.wav")
    ix.scan()
    audio = AudioLibrary(ix.db_path)
    result = audio.analyze(str(root / "Totally Wrong 140bpm.wav"))
    assert abs(result["bpm"] - 120) < 2
    assert result["bpm_alternatives"]
    found = ix.search(bpm=120, bpm_tolerance=2)
    assert found["total"] == 1
    hit = found["results"][0]
    assert hit["metadata_sources"]["bpm"]["source"] == "audio_estimate"
    assert hit["conflicts"][0]["filename"] == 140
    assert hit["audio"]["instrument"] is None and hit["audio"]["key"] is None


def test_pitch_silence_and_direct_measurements(tmp_path):
    path = tmp_path / "Snare Fminor 99bpm.wav"
    tone(path)
    result = analyze_file(str(path))
    assert result["pitch_hz"] == pytest.approx(440, abs=4)
    assert result["bpm"] is None and result["key"] is None and result["instrument"] is None
    assert result["rms_dbfs"] == pytest.approx(-9.03, abs=0.1)
    silent = tmp_path / "silence.wav"
    sf.write(silent, np.zeros(22050), 22050)
    result = analyze_file(str(silent))
    assert result["silent"] and result["bpm"] is None
    assert result["pitch_hz"] is None


def test_content_cache_rename_invalidation_and_path_boundary(library, tmp_path):
    ix, root = library
    original = root / "a.wav"
    tone(original)
    ix.scan()
    audio = AudioLibrary(ix.db_path)
    first = audio.analyze(str(original))
    renamed = root / "b.wav"
    shutil.copyfile(original, renamed)
    assert audio.analyze(str(renamed))["sha256"] == first["sha256"]
    with audio.connection() as db:
        assert db.execute("SELECT count(*) FROM audio_content").fetchone()[0] == 1
    time.sleep(0.01)
    tone(original, hz=660)
    assert audio.result(str(original)) is None
    outside = tmp_path / "private.wav"
    tone(outside)
    with pytest.raises(ValueError, match="indexed"):
        audio.analyze(str(outside))


def test_worker_process_job_completes_and_can_resume_errors(library, monkeypatch):
    ix, root = library
    tone(root / "valid.wav")
    (root / "broken.wav").write_bytes(b"bad file")
    ix.scan()
    audio = AudioLibrary(ix.db_path)
    # Keep job execution synchronous here; a separate test exercises a real child process.
    monkeypatch.setattr(audio, "_spawn", lambda job, semantic=False: {"id": job})
    job = audio.start(limit=2)["id"]
    run(audio, job)
    assert audio.status(job)["state"] == "partial"
    assert audio.status(job)["counts"] == {"done": 1, "error": 1}
    tone(root / "broken.wav")
    audio.resume(job)
    run(audio, job)
    assert audio.status(job)["state"] == "complete"
    assert audio.status(job)["counts"] == {"done": 2}


def test_real_analysis_subprocess(library):
    ix, root = library
    tone(root / "worker.wav")
    ix.scan()
    audio = AudioLibrary(ix.db_path)
    job = audio.start(str(root / "worker.wav"))["id"]
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        status = audio.status(job)
        if status["state"] in ("complete", "error", "partial"):
            break
        time.sleep(0.1)
    assert status["state"] == "complete", status
    assert audio.result(str(root / "worker.wav"))["pitch_hz"] == pytest.approx(440, abs=4)


def test_semantic_search_fails_explicitly_without_model(library, monkeypatch):
    monkeypatch.delenv("AGENT_BRIDGE_CLAP_MODEL", raising=False)
    ix, root = library
    monkeypatch.setenv("AGENT_BRIDGE_DATA_DIR", str(root.parent / "empty-model-config"))
    with pytest.raises(ValueError, match="local.*CLAP"):
        AudioLibrary(ix.db_path).semantic_search(description="dark bass")
