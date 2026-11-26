"""Create or resolve a synthetic 124 BPM WAV for Live acceptance."""

from __future__ import annotations

import math
import sys
import wave
from array import array
from pathlib import Path


def acceptance_audio(sample: str | Path | None, directory: Path) -> str:
    if sample:
        path = Path(sample).expanduser().resolve(strict=True)
        if not path.is_file() or path.suffix.lower() != ".wav":
            raise ValueError("Provide an existing WAV fixture")
        return str(path)

    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "synthetic-acceptance-124bpm.wav"
    rate = 48_000
    duration = 64 * 60 / 124
    frames = round(rate * duration)
    if target.exists():
        with wave.open(str(target), "rb") as existing:
            if (existing.getnchannels(), existing.getsampwidth(), existing.getframerate(), existing.getnframes()) != (
                1, 2, rate, frames,
            ):
                raise ValueError(f"Existing fixture has unexpected format: {target}")
        return str(target.resolve())

    samples = array("h")
    for frame in range(frames):
        time = frame / rate
        beat = time * 124 / 60
        beat_number = int(beat)
        position = beat - beat_number
        fundamental = (55.0, 65.406, 82.407, 55.0)[beat_number // 4 % 4]
        envelope = min(1.0, position * 64) * math.exp(-4 * position)
        harmonic = math.sin(math.tau * fundamental * time) + 0.2 * math.sin(math.tau * 2 * fundamental * time)
        signal = 0.34 * envelope * harmonic
        samples.append(round(32767 * signal))
    if sys.byteorder != "little":
        samples.byteswap()
    with target.open("xb") as raw, wave.open(raw, "wb") as output:
        output.setparams((1, 2, rate, frames, "NONE", "not compressed"))
        output.writeframes(samples.tobytes())
    return str(target.resolve())
