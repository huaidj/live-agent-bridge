"""Local CLAP interpretation over spaced audio segments. No filename is sent to the model."""

from __future__ import annotations

import argparse
import json
import math
import time

from ..taste.locking import file_lock
from ..taste.store import TasteStore, utc_now
from .features import analyze_file, check_decodable
from .library import content_hash
from .references import VERSION, ReferenceLibrary

PROMPTS = {
    "genre": {"house": "This is house music with a steady dance groove.",
              "techno": "This is techno music with repetitive electronic rhythm.",
              "trance": "This is melodic trance music.", "drum_and_bass": "This is fast drum and bass music.",
              "garage": "This is UK garage music with a shuffled dance beat.",
              "dubstep": "This is dubstep music with heavy bass.", "hip_hop": "This is hip hop music.",
              "disco_funk": "This is disco and funk music.", "pop": "This is pop music.",
              "rock": "This is rock music.", "jazz": "This is jazz music.",
              "classical": "This is classical orchestral music.", "ambient": "This is ambient music."},
    "instruments": {"electronic_drums": "Electronic kick drums, snare drums and hi hats.",
                    "acoustic_drums": "An acoustic drum kit is playing.", "synth_bass": "A deep synthesizer bass line.",
                    "electric_bass": "An electric bass guitar is playing.",
                    "synth_lead": "A melodic synthesizer lead is playing.", "pad": "Sustained synthesizer pads.",
                    "piano": "A piano is playing.", "guitar": "A guitar is playing.",
                    "strings": "An ensemble of string instruments.", "brass": "Brass instruments are playing.",
                    "vocals": "A person is singing vocals in the music.",
                    "percussion": "Hand percussion and shakers are playing."},
    "texture": {"dark": "The music sounds dark, deep and brooding.", "bright": "The music sounds bright and sparkling.",
                "warm": "The music has a warm and soft sound.", "distorted": "The music is gritty and distorted.",
                "clean": "The music sounds clean and clear.", "spacious": "The music sounds spacious and reverberant.",
                "dry": "The music sounds dry and close.", "minimal": "The music has a sparse minimal arrangement.",
                "dense": "The music has a dense layered arrangement.", "energetic": "The music sounds energetic and intense.",
                "relaxed": "The music sounds relaxed and gentle."},
}


def cosine(left, right):
    denominator = math.sqrt(sum(v * v for v in left) * sum(v * v for v in right))
    return sum(a * b for a, b in zip(left, right, strict=True)) / denominator if denominator else 0.0


def analyze_song(path: str, model, cancelled=lambda: False, progress=lambda: None, segments: int = 6) -> dict:
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly

    check_decodable(path)
    info = sf.info(path)
    if info.duration <= 0 or info.duration > 3600 or info.channels > 8 or info.samplerate > 384000:
        raise ValueError("Reference must be decodable audio up to 60 minutes and 8 channels")
    groups = {group: list(labels) for group, labels in PROMPTS.items()}
    flat = [(group, label, prompt) for group, labels in PROMPTS.items() for label, prompt in labels.items()]
    text_vectors = model.texts([prompt for _, _, prompt in flat])
    count = min(segments, max(1, math.ceil(info.duration / 10)))
    starts = np.linspace(0, max(0, info.duration - 10), count).tolist()
    parts, averages = [], {group: {label: [] for label in labels} for group, labels in groups.items()}
    with sf.SoundFile(path) as stream:
        for start in starts:
            if cancelled():
                raise InterruptedError("Reference analysis cancelled")
            progress()
            stream.seek(min(int(start * info.samplerate), info.frames - 1))
            data = stream.read(min(int(info.samplerate * 10), info.frames - stream.tell()),
                               dtype="float32", always_2d=True).mean(axis=1)
            if not np.all(np.isfinite(data)):
                raise ValueError("Audio contains non-finite samples")
            divisor = math.gcd(info.samplerate, 48000)
            audio = resample_poly(data, 48000 // divisor, info.samplerate // divisor)
            if float(np.sqrt(np.mean(audio ** 2))) < 1e-6:
                parts.append({"start_seconds": round(start, 3), "end_seconds": round(start + len(data) / info.samplerate, 3),
                              "silent": True, "candidates": {}})
                continue
            vector = model.audio_array(audio)
            scores = {group: [] for group in groups}
            for (group, label, _), candidate in zip(flat, text_vectors, strict=True):
                similarity = cosine(vector, candidate)
                scores[group].append({"label": label, "cosine_similarity": round(similarity, 5)})
                averages[group][label].append(similarity)
            top = {group: sorted(values, key=lambda row: (-row["cosine_similarity"], row["label"]))[:3]
                   for group, values in scores.items()}
            parts.append({"start_seconds": round(start, 3), "end_seconds": round(start + len(data) / info.samplerate, 3),
                          "candidates": top})
    observations = {}
    for group, values in averages.items():
        ranking = [{"label": label, "cosine_similarity": round(sum(scores) / len(scores), 5), "segments": len(scores)}
                   for label, scores in values.items() if scores]
        observations[group] = sorted(ranking, key=lambda row: (-row["cosine_similarity"], row["label"]))[:3]
    return {"version": VERSION, "model": model.model_id, "duration_seconds": round(info.duration, 3),
            "observations": observations, "segments": parts,
            # measure tempo/key in the body of the song: intros are often beatless or harmonically thin
            "measurements": analyze_file(path, max_seconds=90,
                                         offset_seconds=max(0.0, min(info.duration * 0.3, info.duration - 90))),
            "coverage": {"segments": count, "segment_seconds": 10,
                         "note": "Spaced excerpts across the timeline; not a full arrangement transcription."},
            "limitations": ["Scores compare a fixed candidate vocabulary; they are not calibrated probabilities.",
                            "Candidate genres/instruments/textures require user review.",
                            "This model does not explain which feature caused the user to like the song.",
                            "Lyrics, artist identity, precise chords and stems are not inferred."],
            "audio_uploaded": False, "filename_used_by_model": False}


def run(library: ReferenceLibrary, job: str, model_factory=None) -> None:
    try:
        with file_lock(library.directory / "audio-model-worker.lock", timeout=0.1):
            with library.connection() as db:
                row = db.execute("SELECT * FROM reference_jobs WHERE id=?", (job,)).fetchone()
                if row is None:
                    return
                if row["state"] == "cancelling":
                    db.execute("UPDATE reference_jobs SET state='cancelled' WHERE id=?", (job,))
                    return
                db.execute("UPDATE reference_jobs SET state='running',updated=? WHERE id=?", (time.time(), job))
            path = library.validate_path(row["path"])
            before = path.stat()
            checksum = content_hash(path)
            if model_factory is None:
                from .semantic import LocalCLAP
                model_factory = LocalCLAP
            model = model_factory()

            def cancelled():
                with library.connection() as db:
                    return db.execute("SELECT state FROM reference_jobs WHERE id=?", (job,)).fetchone()[0] == "cancelling"

            def progress():
                with library.connection() as db:
                    db.execute("UPDATE reference_jobs SET updated=? WHERE id=?", (time.time(), job))

            result = analyze_song(str(path), model, cancelled, progress)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns, checksum) != (after.st_size, after.st_mtime_ns, content_hash(path)):
                raise ValueError("Reference changed during analysis; no memory was written")
            if cancelled():
                raise InterruptedError("Reference analysis cancelled")
            result.update(sha256=checksum, original_unchanged=True)
            if row["remember"]:
                store = TasteStore(library.directory / "taste.db")
                label = row["label"] or "Reference " + checksum[:8]
                selection = store.event("choice", "Selected favorite reference: " + label, source="user",
                                        data={"sha256": checksum, "user_note": row["user_note"]}, writer="reference-import/user")
                evidence_id = "reference:" + checksum + ":" + model.model_id + ":" + job
                analysis_evidence = store.event("audio_analysis", label, source="reference", event_id=evidence_id,
                                               data={"sha256": checksum, "model": model.model_id, "job": job},
                                               writer="local-audio-model/" + VERSION, occurred_at=utc_now())
                memory = {"observations": result["observations"], "user_note": row["user_note"],
                          "selection_evidence": selection, "sha256": checksum, "model": model.model_id,
                          "time_spans": [{"start": part["start_seconds"], "end": part["end_seconds"]}
                                         for part in result["segments"]], "model_inference": True}
                store.fact(source="reference", source_id=checksum, name=label, data=memory, event_id=analysis_evidence)
                result["memory"] = {"selection_evidence": selection, "analysis_evidence": analysis_evidence,
                                    "confirmed_feature_preferences": 0}
            with library.connection() as db:
                db.execute("UPDATE reference_jobs SET state='complete',result=?,updated=? WHERE id=?",
                           (json.dumps(result), time.time(), job))
    except InterruptedError:
        with library.connection() as db:
            db.execute("UPDATE reference_jobs SET state='cancelled',updated=? WHERE id=?", (time.time(), job))
    except Exception as error:  # noqa: BLE001 -- persist the worker outcome for status/result tools
        with library.connection() as db:
            db.execute("UPDATE reference_jobs SET state='error',error=?,updated=? WHERE id=?",
                       (f"{type(error).__name__}: {str(error)[:500]}", time.time(), job))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    run(ReferenceLibrary(args.directory), args.job)


if __name__ == "__main__":
    main()
