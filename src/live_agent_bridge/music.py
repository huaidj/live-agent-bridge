"""Compact musical notations -> MIDI notes, so an LLM can express intent in a
few tokens ("Am F C G", "x...x...x...x...", "C4:1 E4:.5 G4:.5") instead of
computing dozens of note dicts. Pure functions, no Live access.

Pitch names follow Ableton: C3 = 60.
"""

from __future__ import annotations

import re

PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}

DRUMS = {
    "kick": 36, "bd": 36, "rim": 37, "sidestick": 37, "snare": 38, "sd": 38, "clap": 39, "cp": 39,
    "snare2": 40, "lowtom": 41, "lt": 41, "hat": 42, "hh": 42, "chh": 42, "closedhat": 42,
    "tom": 43, "pedalhat": 44, "midtom": 45, "mt": 45, "openhat": 46, "oh": 46, "ohh": 46,
    "hitom": 48, "ht": 48, "crash": 49, "cy": 49, "ride": 51, "rd": 51, "china": 52, "ridebell": 53,
    "tamb": 54, "tambourine": 54, "splash": 55, "cowbell": 56, "cb": 56, "shaker": 70, "perc": 47,
}

SCALES = {
    "major": [0, 2, 4, 5, 7, 9, 11], "minor": [0, 2, 3, 5, 7, 8, 10], "dorian": [0, 2, 3, 5, 7, 9, 10],
    "phrygian": [0, 1, 3, 5, 7, 8, 10], "lydian": [0, 2, 4, 6, 7, 9, 11], "mixolydian": [0, 2, 4, 5, 7, 9, 10],
    "locrian": [0, 1, 3, 5, 6, 8, 10], "harmonic_minor": [0, 2, 3, 5, 7, 8, 11],
    "melodic_minor": [0, 2, 3, 5, 7, 9, 11], "pentatonic": [0, 2, 4, 7, 9], "minor_pentatonic": [0, 3, 5, 7, 10],
    "blues": [0, 3, 5, 6, 7, 10],
}

QUALITIES = {
    "": [0, 4, 7], "maj": [0, 4, 7], "M": [0, 4, 7], "m": [0, 3, 7], "min": [0, 3, 7], "-": [0, 3, 7],
    "dim": [0, 3, 6], "o": [0, 3, 6], "°": [0, 3, 6], "aug": [0, 4, 8], "+": [0, 4, 8],
    "sus2": [0, 2, 7], "sus4": [0, 5, 7], "sus": [0, 5, 7], "5": [0, 7],
    "6": [0, 4, 7, 9], "m6": [0, 3, 7, 9], "7": [0, 4, 7, 10], "maj7": [0, 4, 7, 11], "M7": [0, 4, 7, 11],
    "m7": [0, 3, 7, 10], "min7": [0, 3, 7, 10], "-7": [0, 3, 7, 10], "mmaj7": [0, 3, 7, 11],
    "dim7": [0, 3, 6, 9], "o7": [0, 3, 6, 9], "m7b5": [0, 3, 6, 10], "ø": [0, 3, 6, 10], "ø7": [0, 3, 6, 10],
    "add9": [0, 4, 7, 14], "madd9": [0, 3, 7, 14], "9": [0, 4, 7, 10, 14], "maj9": [0, 4, 7, 11, 14],
    "m9": [0, 3, 7, 10, 14], "11": [0, 4, 7, 10, 14, 17], "m11": [0, 3, 7, 10, 14, 17],
    "13": [0, 4, 7, 10, 14, 21], "7sus4": [0, 5, 7, 10],
}

_NOTE_RE = re.compile(r"^([A-Ga-g])([#b]*)(-?\d+)?$")
_CHORD_RE = re.compile(r"^([A-G])([#b]?)(.*?)(?:/([A-G][#b]?))?$")
_ROMAN_RE = re.compile(r"^([b#]?)(VII|VI|V|IV|III|II|I|vii|vi|v|iv|iii|ii|i)(.*?)$")
_ROMAN = {"i": 0, "ii": 1, "iii": 2, "iv": 3, "v": 4, "vi": 5, "vii": 6}


class MusicError(ValueError):
    pass


def pitch(name: str | int, default_octave: int = 3) -> int:
    """'C3' -> 60, 'F#1' -> 42, 'kick' -> 36, 64 -> 64."""
    if isinstance(name, int):
        return name
    s = str(name).strip()
    if s.lstrip("-").isdigit():
        return int(s)
    d = DRUMS.get(s.lower().replace(" ", "").replace("_", "").replace("-", ""))
    if d is not None:
        return d
    m = _NOTE_RE.match(s)
    if not m:
        raise MusicError(f"bad note name {name!r}")
    pc = PC[m.group(1).upper()] + m.group(2).count("#") - m.group(2).count("b")
    octave = int(m.group(3)) if m.group(3) is not None else default_octave
    p = pc + (octave + 2) * 12
    if not 0 <= p <= 127:
        raise MusicError(f"{name!r} is outside MIDI range")
    return p


def _root_pc(letter: str, acc: str) -> int:
    return (PC[letter.upper()] + acc.count("#") - acc.count("b")) % 12


def parse_key(key: str) -> tuple[int, str]:
    """'A minor' / 'Am' / 'F# dorian' / 'Eb' -> (root pc, scale name)."""
    k = key.strip()
    m = re.match(r"^([A-Ga-g])([#b]?)\s*(.*)$", k)
    if not m:
        raise MusicError(f"bad key {key!r}")
    rest = m.group(3).strip().lower().replace(" ", "_")
    if rest in ("", "maj", "major", "ionian"):
        mode = "major"
    elif rest in ("m", "min", "minor", "aeolian"):
        mode = "minor"
    elif rest in SCALES:
        mode = rest
    else:
        raise MusicError(f"unknown scale {m.group(3)!r}; one of {sorted(SCALES)}")
    return _root_pc(m.group(1), m.group(2)), mode


def chord_tones(symbol: str, key: str | None = None) -> tuple[int, list[int], int | None]:
    """Returns (root pitch class, intervals, bass pc or None). Accepts 'Am7', 'F/A', 'C#dim',
    or roman numerals with a key: 'ii7', 'V', 'bVII', 'iv'."""
    s = symbol.strip()
    m = _CHORD_RE.match(s)
    if m and (m.group(3) in QUALITIES):
        root = _root_pc(m.group(1), m.group(2))
        bass = _root_pc(m.group(4)[0], m.group(4)[1:]) if m.group(4) else None
        return root, QUALITIES[m.group(3)], bass
    r = _ROMAN_RE.match(s)
    if r:
        if not key:
            raise MusicError(f"roman numeral {symbol!r} needs a key")
        tonic, mode = parse_key(key)
        scale = SCALES[mode] if len(SCALES[mode]) == 7 else SCALES["major"]
        deg = _ROMAN[r.group(2).lower()]
        root = (tonic + scale[deg] + (1 if r.group(1) == "#" else -1 if r.group(1) == "b" else 0)) % 12
        upper = r.group(2).isupper()
        q = r.group(3)
        if q in ("°", "o", "dim"):
            ivs = QUALITIES["dim"]
        elif q in ("ø", "ø7"):
            ivs = QUALITIES["m7b5"]
        elif q in ("7",):
            ivs = QUALITIES["7"] if upper else QUALITIES["m7"]
        elif q in ("maj7", "M7"):
            ivs = QUALITIES["maj7"]
        elif q in ("9",):
            ivs = QUALITIES["9"] if upper else QUALITIES["m9"]
        elif q == "":
            ivs = QUALITIES[""] if upper else QUALITIES["m"]
        elif q in QUALITIES:
            ivs = QUALITIES[q]
        else:
            raise MusicError(f"unknown chord quality in {symbol!r}")
        return root, ivs, None
    raise MusicError(f"cannot parse chord {symbol!r}")


def _voice(root_pc: int, ivs: list[int], octave: int, prev: list[int] | None) -> list[int]:
    base = root_pc + (octave + 2) * 12
    close = [base + i for i in ivs]
    if prev is None:
        return close
    # choose the inversion/octave shift closest to the previous chord (smooth voice leading)
    best, best_cost = close, None
    center = sum(prev) / len(prev)
    for inv in range(len(ivs)):
        v = sorted(close[inv:] + [p + 12 for p in close[:inv]])
        for shift in (-12, 0, 12):
            cand = [p + shift for p in v]
            if cand[0] < 24 or cand[-1] > 108:
                continue
            cost = abs(sum(cand) / len(cand) - center) + abs(cand[0] - prev[0]) * 0.3
            if best_cost is None or cost < best_cost:
                best, best_cost = cand, cost
    return best


def chords_to_notes(progression: str, key: str | None = None, beats_per_chord: float = 4.0,
                    octave: int = 3, style: str = "block", voicing: str = "smooth", bass: bool = False,
                    velocity: int = 90, start: float = 0.0) -> tuple[list[dict], float]:
    """'Am F C G' or 'i VI III VII' (with key) or 'Am:2 F:2 C:4' (per-chord beats).
    style: block | stab | pulse8 | arp_up | arp_down | arp_updown (16ths)."""
    notes: list[dict] = []
    t = start
    prev = None
    for tok in progression.replace("|", " ").replace(",", " ").split():
        sym, _, dur = tok.partition(":")
        beats = float(dur) if dur else beats_per_chord
        root, ivs, bass_pc = chord_tones(sym, key)
        voiced = _voice(root, ivs, octave, prev if voicing == "smooth" else None)
        prev = voiced
        if style == "block":
            notes += [_n(p, t, beats, velocity) for p in voiced]
        elif style in ("stab", "pulse8"):
            step = 1.0 if style == "stab" else 0.5
            length = 0.25 if style == "stab" else 0.4
            k = 0.0
            while k < beats - 1e-9:
                notes += [_n(p, t + k, min(length, beats - k), velocity) for p in voiced]
                k += step
        elif style.startswith("arp"):
            seq = voiced + [voiced[0] + 12]
            if style == "arp_down":
                seq = seq[::-1]
            elif style == "arp_updown":
                seq = seq + seq[-2:0:-1]
            k, i = 0.0, 0
            while k < beats - 1e-9:
                notes.append(_n(seq[i % len(seq)], t + k, 0.25, velocity))
                k += 0.25
                i += 1
        else:
            raise MusicError("style must be block, stab, pulse8, arp_up, arp_down or arp_updown")
        if bass:
            bp = (bass_pc if bass_pc is not None else root) + (octave + 1) * 12
            while bp >= voiced[0]:
                bp -= 12
            notes.append(_n(bp, t, beats, velocity))
        t += beats
    return notes, t - start


def _n(p: int, t: float, d: float, v: float) -> dict:
    return {"pitch": int(p), "start_time": round(t, 6), "duration": round(d, 6), "velocity": float(v)}


def pattern_to_notes(rows: dict[str, str], step: float = 0.25, velocity: int = 100,
                     swing: float = 0.0, gate: float = 0.9) -> tuple[list[dict], float]:
    """Step-sequencer strings. Per char: x=velocity, X=accent(127), o/g=ghost(60),
    1-9 = velocity*digit/9, '.' '-' '_' = rest. Spaces and '|' are ignored.
    Row keys: drum names (kick, snare, hat, openhat, clap, ...), note names (C1) or numbers."""
    notes: list[dict] = []
    length = 0.0
    for key, steps in rows.items():
        p = pitch(key, default_octave=1)
        cells = [c for c in steps if c not in " |\t"]
        length = max(length, len(cells) * step)
        for i, c in enumerate(cells):
            if c in ".-_":
                continue
            if c == "X":
                v = 127
            elif c in "xX*":
                v = velocity
            elif c in "og":
                v = 60
            elif c.isdigit() and c != "0":
                v = max(1, round(127 * int(c) / 9))
            else:
                raise MusicError(f"bad step char {c!r} in row {key!r}")
            t = i * step + (step * swing * 0.5 if i % 2 == 1 else 0.0)
            notes.append(_n(p, t, step * gate, v))
    return notes, length


def melody_to_notes(text: str, default_duration: float = 0.5, velocity: int = 100,
                    octave: int = 3) -> tuple[list[dict], float]:
    """'C4:1 D4:.5 r:.5 [C3 E3 G3]:2 E4' - note[:beats], r/rest for silence,
    [..] for a chord; optional @vel suffix e.g. 'C4:1@80'."""
    notes: list[dict] = []
    t = 0.0
    for tok in re.findall(r"\[[^\]]*\][^\s]*|\S+", text):
        vel = velocity
        if "@" in tok:
            tok, v = tok.rsplit("@", 1)
            vel = int(v)
        body, _, dur = tok.rpartition(":") if ":" in tok else (tok, "", "")
        beats = float(dur) if dur else default_duration
        if body.lower() in ("r", "rest", "-"):
            t += beats
            continue
        names = body.strip("[]").split() if body.startswith("[") else [body]
        for nm in names:
            notes.append(_n(pitch(nm, octave), t, beats, vel))
        t += beats
    return notes, t


def scale_notes(key: str, low: str = "C2", high: str = "C5") -> list[int]:
    tonic, mode = parse_key(key)
    pcs = {(tonic + i) % 12 for i in SCALES[mode]}
    return [p for p in range(pitch(low), pitch(high) + 1) if p % 12 in pcs]
