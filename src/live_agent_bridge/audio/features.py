"""Measurements from decoded audio. Never infers instruments or keys from filenames."""

from __future__ import annotations

import math

VERSION = "signal-v2"

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
# Krumhansl-Kessler key profiles (tonic first)
MAJOR_PROFILE = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
MINOR_PROFILE = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]

# AIFC compression types that no local decoder (libsndfile, ffmpeg 9) can read. 'able' is Ableton's
# own codec used throughout Factory Packs; only Live itself decodes it.
PROPRIETARY_AIFC = {b"able": "Ableton-encoded AIFC (codec 'able'); only Live can decode it"}


class UndecodableAudio(ValueError):
    """The file is valid but uses a codec no local decoder supports; retrying will not help."""


def aifc_compression(path: str) -> bytes | None:
    """Compression type of an AIFF-C file's COMM chunk, or None for anything else."""
    import struct

    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] != b"FORM" or head[8:12] != b"AIFC":
            return None
        while True:
            chunk = f.read(8)
            if len(chunk) < 8:
                return None
            cid, size = chunk[:4], struct.unpack(">I", chunk[4:])[0]
            if cid == b"COMM":
                body = f.read(min(size, 64))
                return body[18:22] if len(body) >= 22 else None
            f.seek(size + (size & 1), 1)


def check_decodable(path: str) -> None:
    if path.lower().endswith((".aif", ".aiff", ".aifc")):
        reason = PROPRIETARY_AIFC.get(aifc_compression(path) or b"")
        if reason:
            raise UndecodableAudio(reason)


def estimate_tempo(mono, sr: int):
    """(bpm, confidence 0..1, alternatives) from the onset-strength autocorrelation, or None when
    the signal has no stable pulse. Octave errors are possible; alternatives list the half/double."""
    import numpy as np
    from scipy import signal

    n_fft, hop = 1024, 256
    if len(mono) < sr * 2:
        return None
    _, _, spectrum = signal.stft(mono, sr, nperseg=n_fft, noverlap=n_fft - hop, boundary=None)
    magnitude = np.log1p(100 * np.abs(spectrum))
    onset = np.maximum(0, np.diff(magnitude, axis=1)).sum(axis=0)
    if onset.size < 64 or float(onset.std()) < 1e-6:
        return None
    # Sustained tones (chords, pads) wobble but have no transients: their onset envelope is flat.
    median = float(np.median(onset))
    if median > 0 and float(np.percentile(onset, 90)) / median < 3.0:
        return None
    fps = sr / hop
    window = max(3, int(fps))  # remove the slow loudness trend (about one second)
    onset = np.maximum(0, onset - np.convolve(onset, np.ones(window) / window, mode="same"))
    onset = onset - onset.mean()
    ac = signal.correlate(onset, onset, mode="full", method="fft")[len(onset) - 1:]
    if ac[0] <= 1e-12:
        return None
    ac = ac / ac[0]

    def at(lag):
        if lag >= len(ac) - 1:
            return 0.0
        i = int(lag)
        return float(ac[i] + (ac[i + 1] - ac[i]) * (lag - i))

    bpms = np.arange(60.0, 200.01, 0.25)
    scores = []
    for bpm in bpms:
        lag = fps * 60 / bpm
        # a real pulse also correlates at 2 and 4 beats; a mild prior favours common dance tempi
        score = at(lag) + 0.5 * at(2 * lag) + 0.25 * at(4 * lag)
        # bar-level repetition makes half-time correlate strongly too; prefer the common dance range
        prior = math.exp(-0.5 * (math.log2(bpm / 125.0) / 0.6) ** 2)
        scores.append(score * (0.5 + 0.5 * prior))
    scores = np.array(scores)
    best = int(np.argmax(scores))
    if scores[best] <= 0.05:
        return None
    # Loops are cut to whole bars: among strong candidate peaks (incl. 2:3 / half / double relatives),
    # prefer one whose tempo makes the file an exact number of 4/4 bars.
    seconds = len(mono) / sr
    peaks, _ = signal.find_peaks(scores)
    strong = [int(i) for i in peaks if scores[i] >= 0.5 * scores[best]]
    if seconds <= 32:

        def bar_error(i):
            beats = seconds * bpms[i] / 60
            return abs(beats / 4 - round(beats / 4)) * 4  # in beats

        fitting = [i for i in strong if bar_error(i) < 0.08 and round(seconds * bpms[i] / 240) >= 1]
        if fitting and bar_error(best) >= 0.08:
            best = max(fitting, key=lambda i: scores[i])
    if 0 < best < len(scores) - 1:  # parabolic refinement
        a, b, c = scores[best - 1], scores[best], scores[best + 1]
        shift = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) else 0.0
    else:
        shift = 0.0
    bpm = float(bpms[best] + shift * 0.25)
    spread = float(np.percentile(scores, 90) - np.median(scores))
    confidence = float(np.clip((scores[best] - np.percentile(scores, 90)) / (spread + scores[best] + 1e-9) * 1.5,
                               0, 0.95))  # never certain: half/double-time remains possible
    # Patterns that repeat every two beats make half-time win; if the single beat is still a clear
    # autocorrelation peak above the baseline, report the faster (felt) tempo.
    if bpm < 90 and bpm * 2 <= 200:
        baseline = float(np.median(ac[int(fps * 60 / 200):int(fps * 60 / 60)]))
        if at(fps * 60 / (bpm * 2)) - baseline > 0.15:
            bpm, confidence = bpm * 2, confidence * 0.8
    others = sorted((i for i in strong if abs(bpms[i] - bpm) > 3), key=lambda i: -scores[i])[:2]
    alternatives = []
    for x in sorted([x for x in (bpm / 2, bpm * 2) if 45 <= x <= 240] + [float(bpms[i]) for i in others]):
        if not alternatives or x - alternatives[-1] > 1.0:  # merge near-duplicates (e.g. 62.0 vs 62.04)
            alternatives.append(round(x, 2))
    return round(bpm, 2), round(confidence, 3), alternatives


def estimate_key(mono, sr: int):
    """Key estimate from a long-term chroma profile matched to Krumhansl-Kessler profiles.
    Returns None when the audio does not contain enough distinct pitch classes to imply a key."""
    import numpy as np
    from scipy import signal

    n_fft = 8192
    if len(mono) < n_fft * 2:
        return None
    from scipy.ndimage import median_filter

    frequency, _, spectrum = signal.stft(mono, sr, nperseg=n_fft, noverlap=n_fft // 2, boundary=None)
    power = np.abs(spectrum) ** 2
    usable = (frequency >= 55) & (frequency <= 2000)
    if not usable.any():
        return None
    # Harmonic/percussive separation (median filtering): drums and clicks must not imply a key.
    band = power[usable]
    width = max(1, min(17, band.shape[1] // 2 * 2 + 1))
    harmonic = median_filter(band, size=(1, width), mode="nearest")
    percussive = median_filter(band, size=(17, 1), mode="nearest")
    mask = harmonic ** 2 / np.maximum(harmonic ** 2 + percussive ** 2, 1e-20)
    tonal = band * mask
    # Measured: real mixes keep ~15% harmonic energy here; drums/clicks alone stay below ~7%.
    if float(tonal.sum()) / max(float(band.sum()), 1e-20) < 0.10:
        return None
    midi = 69 + 12 * np.log2(frequency[usable] / 440.0)
    classes = np.mod(np.round(midi).astype(int), 12)
    frames = tonal
    # per-frame normalisation keeps loud transients from dominating the tonal profile
    frames = frames / np.maximum(frames.sum(axis=0, keepdims=True), 1e-12)
    chroma = np.zeros(12)
    for pc in range(12):
        chroma[pc] = frames[classes == pc].sum()
    total = chroma.sum()
    if total <= 0:
        return None
    chroma = chroma / total
    if int((chroma > 0.06).sum()) < 3:  # one or two pitch classes do not establish a key
        return None
    candidates = []
    for tonic in range(12):
        for mode, profile in (("major", MAJOR_PROFILE), ("minor", MINOR_PROFILE)):
            rotated = np.roll(profile, tonic)
            r = float(np.corrcoef(chroma, rotated)[0, 1])
            candidates.append((r, f"{NOTE_NAMES[tonic]} {mode}"))
    candidates.sort(reverse=True)
    (r1, best), (r2, _) = candidates[0], candidates[1]
    if r1 < 0.3:
        return None
    # separation from the runner-up (relative/parallel keys are close) times overall fit
    confidence = float(np.clip((r1 - r2) * 2.5, 0, 1) * np.clip((r1 - 0.3) / 0.5, 0, 1))
    return {"key": best, "confidence": round(confidence, 3),
            "candidates": [{"key": k, "correlation": round(r, 3)} for r, k in candidates[:3]],
            "profile": {NOTE_NAMES[i]: round(float(chroma[i]), 3) for i in range(12)}}


def analyze_file(path: str, max_seconds: float = 120, offset_seconds: float = 0.0) -> dict:
    try:
        import numpy as np
        import soundfile as sf
        from scipy import signal
    except ImportError:
        raise ValueError('Audio analysis needs the optional dependencies: uv pip install -e ".[audio]"') from None
    check_decodable(path)
    info = sf.info(path)
    if info.channels > 8 or info.samplerate < 1000 or info.samplerate > 384000:
        raise ValueError("Unsupported sample rate/channel count")
    start = max(0, min(int(offset_seconds * info.samplerate), info.frames - 1))
    frames = min(info.frames - start, int(info.samplerate * max_seconds))
    if frames <= 0:
        raise ValueError("Audio file contains no frames")
    audio, sr = sf.read(path, start=start, frames=frames, dtype="float32", always_2d=True)
    if not np.all(np.isfinite(audio)):
        raise ValueError("Audio file contains non-finite samples")
    mono = audio.mean(axis=1)
    peak, rms = float(np.max(np.abs(audio))), float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    result = {"analyzer": VERSION, "duration": round(info.frames / sr, 6),
              "analyzed_seconds": round(len(audio) / sr, 6), "analyzed_from_seconds": round(start / sr, 3),
              "sample_rate": sr, "channels": info.channels,
              "rms_dbfs": round(20 * math.log10(max(rms, 1e-9)), 3),
              "peak_dbfs": round(20 * math.log10(max(peak, 1e-9)), 3),
              "bpm": None, "bpm_confidence": 0, "bpm_alternatives": [],
              "pitch_hz": None, "pitch_confidence": 0, "key": None, "key_confidence": 0,
              "loop": None, "loop_confidence": 0, "instrument": None,
              "limitations": ["Tempo and pitch are estimates, not verified musical annotations.",
                              "A single pitch does not establish a musical key.",
                              "Instrument identity requires an optional local semantic model."],
              "source": "decoded_audio"}
    if rms < 1e-6:
        return dict(result, silent=True, low_band_ratio=0, spectral_centroid_hz=0,
                    stereo_width=0, onset_count=0)
    width = float(np.sqrt(np.mean((audio[:, 0] - audio[:, 1]) ** 2)) / max(rms * 2, 1e-9)) if info.channels == 2 else 0
    divisor = math.gcd(sr, 22050)
    mono = signal.resample_poly(mono, 22050 // divisor, sr // divisor)
    sr = 22050
    nperseg = min(2048, len(mono))
    frequency, _, spectrum = signal.stft(mono, sr, nperseg=nperseg, noverlap=nperseg // 2)
    power = np.abs(spectrum) ** 2
    averaged = power.mean(axis=1)
    total_power = max(float(averaged.sum()), 1e-20)
    result.update(silent=False, stereo_width=round(width, 4),
                  low_band_ratio=round(float(averaged[frequency < 200].sum()) / total_power, 4),
                  spectral_centroid_hz=round(float((frequency * averaged).sum()) / total_power, 3))

    # Onset timing ignores file/pack labels. Tempo comes from the periodicity of a spectral-flux onset
    # envelope (autocorrelation over 60-200 BPM), so syncopated real music is handled, not just clicks.
    hop = 220
    envelope = np.array([np.sqrt(np.mean(mono[i:i + hop] ** 2)) for i in range(0, len(mono), hop)])
    flux = np.maximum(0, np.diff(envelope, prepend=0))
    peaks, _ = signal.find_peaks(flux, distance=8, prominence=max(float(flux.max()) * 0.12, 1e-5))
    result["onset_count"] = len(peaks)
    if len(peaks) >= 5 and len(mono) / sr >= 2:
        tempo = estimate_tempo(mono, sr)
        if tempo is not None:
            bpm, confidence, alternatives = tempo
            result.update(bpm=bpm, bpm_confidence=confidence, bpm_alternatives=alternatives,
                          tempo_method="spectral_flux_autocorrelation")
            beats = result["duration"] * bpm / 60
            if abs(beats - round(beats)) < 0.15 and round(beats) % 4 == 0:
                result.update(loop=True, loop_confidence=round(0.4 + 0.4 * confidence, 3))

    key = estimate_key(mono, sr)
    if key is not None:
        result.update(key_candidates=key["candidates"], key_method="chroma_krumhansl",
                      pitch_class_profile=key["profile"], key_confidence=key["confidence"])
        if key["confidence"] >= 0.1:
            result["key"] = key["key"]
        else:  # unknown stays unknown: the candidates are reported, no single key is asserted
            result["key_note"] = "ambiguous: top candidates are nearly tied"

    # Stable periodicity is evidence of pitch, not of an instrument or major/minor tonality.
    middle = mono[:min(len(mono), sr * 2)]
    if len(middle) >= 1024:
        ac = signal.correlate(middle, middle, mode="full", method="fft")[len(middle) - 1:]
        low, high = max(1, sr // 2000), min(len(ac) - 1, sr // 45)
        candidates, _ = signal.find_peaks(ac[low:high])
        if len(candidates) and ac[0] > 1e-12:
            candidates = candidates + low
            best = int(candidates[np.argmax(ac[candidates])])
            periodicity = float(ac[best] / ac[0])
            if periodicity > 0.8:
                result.update(pitch_hz=round(sr / best, 3), pitch_confidence=round(min(1, periodicity), 3))
    return result
