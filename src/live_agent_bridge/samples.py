"""Local sample library index (SQLite FTS5).

Runs in the MCP server process, never on Live's main thread. Roots are
discovered from Live's own Library.cfg (Places / user folders, installed
packs, User Library, Core Library) plus AGENT_BRIDGE_SAMPLE_DIRS and folders
added at runtime. Scans are incremental (path + mtime + size), and each hit
carries a browser hint so Live can load it into Simpler / Drum Rack without
walking its whole browser.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import threading
import time
import wave
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audio.features import VERSION as _SIGNAL_VERSION  # numpy is only imported lazily there

AUDIO_EXT = {".wav", ".aif", ".aiff", ".flac", ".mp3", ".ogg", ".m4a"}

TAGS = {
    "kick": ["kick", "kik", "bd", "bassdrum"], "snare": ["snare", "snr", "sd"], "clap": ["clap", "clp"],
    "rim": ["rim", "rimshot", "sidestick"], "hihat": ["hihat", "hi-hat", "hat", "hh", "chh", "ohh"],
    "open_hat": ["openhat", "open hat", "ohh", "oh"], "cymbal": ["crash", "ride", "cymbal", "china", "splash"],
    "tom": ["tom", "toms"], "perc": ["perc", "percussion", "conga", "bongo", "shaker", "tamb", "cowbell", "clave"],
    "808": ["808"], "bass": ["bass", "sub", "reese"], "lead": ["lead", "synth lead"], "pad": ["pad", "pads"],
    "chord": ["chord", "chords", "stab"], "keys": ["piano", "keys", "rhodes", "organ", "epiano"],
    "guitar": ["guitar", "gtr"], "strings": ["string", "strings", "violin", "cello"], "brass": ["brass", "horn"],
    "vocal": ["vocal", "vox", "voice", "acapella", "chant"], "fx": ["fx", "sfx", "effect"],
    "riser": ["riser", "uplifter", "sweep", "build"], "impact": ["impact", "hit", "boom", "downlifter"],
    "texture": ["texture", "atmo", "ambience", "ambient", "drone", "foley"], "fill": ["fill", "roll"],
    "break": ["break", "breakbeat"], "top": ["top", "tops"], "loop": ["loop", "loops"],
    "one_shot": ["one shot", "oneshot", "one-shot", "shot", "hit"],
}
_TAG_RES = {t: re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(w) for w in ws) + r")s?(?![a-z])")
            for t, ws in TAGS.items()}
_BPM_RE = re.compile(r"(?<!\d)(\d{2,3}(?:\.\d+)?)\s*-?\s*bpm", re.I)
_BARE_NUM_RE = re.compile(r"(?:^|[ _\-\[\(])(\d{2,3})(?=$|[ _\-\]\)])")
_KEY_RE = re.compile(r"(?:^|[ _\-\[\(])([A-G])([#b♯♭]|sharp|flat)?\s?((?i:minor|min|major|maj)|m|M)?(?=$|[ _\-\]\)])")
_NOTE_ORDER = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_FLATS = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#", "Cb": "B", "Fb": "E"}


def parse_meta(path: str) -> dict[str, Any]:
    p = Path(path)
    stem = p.stem
    hay = (p.parent.name + " " + stem).lower().replace("_", " ")
    tags = sorted(t for t, rx in _TAG_RES.items() if rx.search(hay))
    bpm = None
    m = _BPM_RE.search(stem) or _BPM_RE.search(p.parent.name)
    if m:
        bpm = float(m.group(1))
    elif "loop" in tags:
        for m in _BARE_NUM_RE.finditer(stem):
            v = float(m.group(1))
            if 60 <= v <= 200:
                bpm = v
                break
    if bpm is not None and not 40 <= bpm <= 300:
        bpm = None
    key = None
    for m in _KEY_RE.finditer(stem.replace("_", " ")):
        root, acc, qual = m.group(1).upper(), m.group(2), m.group(3)
        if not acc and not qual and bpm is None:
            continue  # lone letter: too ambiguous ("Kick A.wav")
        if acc in ("#", "♯", "sharp"):
            root += "#"
        elif acc in ("b", "♭", "flat"):
            root = _FLATS.get(root + "b", root)
        minor = qual is not None and (qual == "m" or qual.lower().startswith("min"))
        key = root + ("m" if minor else "")
        break
    is_loop = 1 if ("loop" in tags or bpm is not None) and "one_shot" not in tags else 0
    return {"bpm": bpm, "key": key, "tags": tags, "is_loop": is_loop}


def _duration(path: str) -> float | None:
    if not path.lower().endswith(".wav"):
        return None
    try:
        with wave.open(path, "rb") as w:
            return round(w.getnframes() / float(w.getframerate()), 3)
    except Exception:
        return None


@dataclass
class Root:
    path: str
    category: str          # Live browser category for load hints
    browser_root: str | None
    source: str


def _latest_live_prefs() -> Path | None:
    base = Path(os.environ.get("APPDATA", "")) / "Ableton"
    cands = [p for p in base.glob("Live */Preferences/Library.cfg")]
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime)


def discover_roots() -> list[Root]:
    roots: list[Root] = []
    seen: set[str] = set()

    def add(path: str, cat: str, broot: str | None, source: str):
        if not path:
            return
        norm = os.path.normcase(os.path.abspath(path))
        if norm in seen or not os.path.isdir(path):
            return
        seen.add(norm)
        roots.append(Root(os.path.abspath(path), cat, broot, source))

    for extra in filter(None, os.environ.get("AGENT_BRIDGE_SAMPLE_DIRS", "").split(os.pathsep)):
        add(extra, "user_folders", Path(extra).name, "env")
    cfg = _latest_live_prefs()
    user_lib = None
    if cfg:
        try:
            tree = ET.parse(cfg)
            for el in tree.iter("UserFolderInfo"):
                add(el.get("Path", ""), "user_folders", el.get("DisplayName"), "Live Places")
            for el in tree.iter("LibrarySliceInfo"):
                add(el.get("Path", ""), "packs", el.get("DisplayName"), "Live pack")
            for el in tree.iter():
                if el.tag in ("UserLibrary", "UserLibraryPath") and el.get("Value"):
                    user_lib = el.get("Value")
        except ET.ParseError:
            pass
    from .splice import sample_roots as _splice_roots
    for path, label in _splice_roots():
        add(path, "user_folders", None, label)
    user_lib = user_lib or str(Path.home() / "Documents" / "Ableton" / "User Library")
    add(user_lib, "user_library", None, "User Library")
    for core in sorted(Path("C:/ProgramData/Ableton").glob("Live */Resources/Core Library")) if os.name == "nt" \
            else sorted(Path("/Applications").glob("Ableton Live */Contents/App-Resources/Core Library")):
        add(str(core), "packs", "Core Library", "Core Library")
    return roots


class SampleIndex:
    def __init__(self, db_path: str | None = None):
        base = Path(os.environ["AGENT_BRIDGE_DATA_DIR"]) if os.environ.get("AGENT_BRIDGE_DATA_DIR") else (
            Path(os.environ.get("LOCALAPPDATA", Path.home() / ".cache")) / "live-agent-bridge")
        base.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path or str(base / "samples.db")
        self._lock = threading.Lock()
        self._scan_thread: threading.Thread | None = None
        self.last_scan: dict[str, Any] = {}
        self.recovered_from: str | None = None
        try:
            self._init_schema()
        except sqlite3.DatabaseError:
            # A damaged index is only a cache: keep the bad file for inspection and rebuild.
            bad = f"{self.db_path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
            moved: list[tuple[str, str]] = []
            try:
                # all-or-nothing: the journal files are the ones another process may hold
                for suffix in ("-wal", "-shm", ""):
                    if os.path.exists(self.db_path + suffix):
                        os.replace(self.db_path + suffix, bad + suffix)
                        moved.append((self.db_path + suffix, bad + suffix))
                self.recovered_from = bad
            except OSError:
                for orig, dest in reversed(moved):
                    try:
                        os.replace(dest, orig)
                    except OSError:
                        pass
                # Still held by another process: use a separate fresh file for this process.
                self.recovered_from = self.db_path
                self.db_path = f"{self.db_path}.{os.getpid()}-{int(time.time())}.db"
            self._init_schema()

    @contextlib.contextmanager
    def _conn(self):
        """One short-lived connection per operation: safe across threads and processes."""
        db = sqlite3.connect(self.db_path, timeout=15.0, isolation_level=None)
        try:
            db.execute("PRAGMA busy_timeout=15000")
            yield db
        finally:
            db.close()

    def _init_schema(self) -> None:
        with self._conn() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS samples(
                  id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, folder TEXT, root TEXT,
                  bpm REAL, key TEXT, tags TEXT, is_loop INTEGER, duration REAL, size INTEGER,
                  mtime REAL, hint TEXT);
                CREATE VIRTUAL TABLE IF NOT EXISTS samples_fts USING fts5(name, folder, tags, tokenize='unicode61');
                CREATE TABLE IF NOT EXISTS extra_roots(path TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
            """)
            db.execute("SELECT count(*) FROM samples").fetchone()
            db.execute("SELECT count(*) FROM samples_fts").fetchone()
        from .audio.library import AudioLibrary
        AudioLibrary(self.db_path)

    # ------------------------------------------------------------ roots
    def roots(self) -> list[Root]:
        roots = discover_roots()
        have = {os.path.normcase(r.path) for r in roots}
        with self._conn() as db:
            extra = [r[0] for r in db.execute("SELECT path FROM extra_roots")]
        for p in extra:
            if os.path.isdir(p) and os.path.normcase(p) not in have:
                roots.append(Root(p, "user_folders", Path(p).name, "added"))
        return roots

    def add_root(self, path: str) -> None:
        if not os.path.isdir(path):
            raise ValueError(f"not a folder: {path}")
        with self._conn() as db:
            db.execute("INSERT OR IGNORE INTO extra_roots VALUES (?)", (os.path.abspath(path),))

    def remove_root(self, path: str) -> None:
        with self._conn() as db:
            db.execute("DELETE FROM extra_roots WHERE path = ?", (os.path.abspath(path),))

    # ------------------------------------------------------------ scanning
    @property
    def count(self) -> int:
        with self._conn() as db:
            return db.execute("SELECT count(*) FROM samples").fetchone()[0]

    @property
    def scanning(self) -> bool:
        return self._scan_thread is not None and self._scan_thread.is_alive()

    def _last_scan_time(self) -> float:
        with self._conn() as db:
            row = db.execute("SELECT value FROM meta WHERE key = 'last_scan'").fetchone()
        if not row:
            return 0.0
        try:
            return float(json.loads(row[0]).get("finished", 0))
        except (ValueError, AttributeError):
            return 0.0

    def ensure_scanned(self) -> None:
        """Start a background scan if the index is empty or stale (> 1 day, shared across processes)."""
        if self.scanning:
            return
        if self.count == 0 or time.time() - self._last_scan_time() > 86400:
            self.start_scan()

    def start_scan(self) -> None:
        with self._lock:
            if self.scanning:
                return
            self._scan_thread = threading.Thread(target=self._scan_safely, daemon=True, name="sample-scan")
            self._scan_thread.start()

    def _scan_safely(self) -> None:
        try:
            self.scan()
        except Exception as e:  # a background scan must never take the server down
            self.last_scan = {"finished": time.time(), "error": f"{type(e).__name__}: {e}"}

    def wait(self, timeout: float) -> bool:
        t = self._scan_thread
        if t is not None:
            t.join(timeout)
        return not self.scanning

    def scan(self) -> dict[str, Any]:
        lock = self.db_path + ".scan.lock"
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.time() - os.path.getmtime(lock) < 600:
                return {"skipped": "another process is scanning"}
            os.remove(lock)  # stale lock from a crashed scanner
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return self._scan()
        finally:
            try:
                os.remove(lock)
            except OSError:
                pass

    def _scan(self) -> dict[str, Any]:
        t0 = time.time()
        roots = self.roots()
        with self._conn() as db:
            known = {p: (m, s) for p, m, s in db.execute("SELECT path, mtime, size FROM samples")}
        seen: set[str] = set()
        rows: list[tuple] = []
        for root in roots:
            for path, st in _walk(root.path):
                seen.add(path)
                prev = known.get(path)
                if prev and prev[0] == st.st_mtime and prev[1] == st.st_size:
                    continue
                rel = os.path.relpath(path, root.path).replace("\\", "/").split("/")
                meta = parse_meta(path)
                if root.source.startswith("Splice"):
                    meta["tags"] = sorted(set(meta["tags"]) | {"splice"})
                hint = {"category": root.category, "root": root.browser_root, "rel": rel}
                rows.append((path, Path(path).name, os.path.dirname(path), root.path, meta["bpm"], meta["key"],
                             " ".join(meta["tags"]), meta["is_loop"], None, st.st_size, st.st_mtime,
                             json.dumps(hint)))
        # Also drop entries whose root was removed or is no longer available: they cannot be loaded.
        gone = [p for p in known if p not in seen]
        with self._conn() as db:
            db.execute("BEGIN IMMEDIATE")  # serialize writers across processes
            cur = db.cursor()
            cur.executemany("""INSERT INTO samples(path,name,folder,root,bpm,key,tags,is_loop,duration,size,mtime,hint)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                               ON CONFLICT(path) DO UPDATE SET name=excluded.name, folder=excluded.folder,
                               root=excluded.root, bpm=excluded.bpm, key=excluded.key, tags=excluded.tags,
                               is_loop=excluded.is_loop, duration=excluded.duration, size=excluded.size,
                               mtime=excluded.mtime, hint=excluded.hint""", rows)
            cur.executemany("DELETE FROM samples WHERE path = ?", [(p,) for p in gone])
            cur.execute("DELETE FROM samples_fts")
            cur.execute("""INSERT INTO samples_fts(rowid, name, folder, tags)
                           SELECT id, replace(replace(name,'_',' '),'-',' '),
                                  replace(replace(replace(folder,'\\',' '),'_',' '),'-',' '), tags FROM samples""")
            db.execute("COMMIT")
        self.last_scan = {"finished": time.time(), "seconds": round(time.time() - t0, 2), "updated": len(rows),
                          "removed": len(gone), "total": self.count, "roots": len(roots)}
        with self._conn() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('last_scan', ?)", (json.dumps(self.last_scan),))
        return self.last_scan

    # ------------------------------------------------------------ search
    def search(self, query: str = "", bpm: float | None = None, bpm_tolerance: float = 3.0,
               key: str | None = None, loop: bool | None = None, tags: list[str] | None = None,
               folder: str | None = None, max_duration: float | None = None, limit: int = 20,
               offset: int = 0, detail: bool = False) -> dict[str, Any]:
        if not 1 <= limit <= 200 or offset < 0 or bpm_tolerance < 0:
            raise ValueError("Search needs a limit from 1 to 200, nonnegative offset and BPM tolerance")
        where, args = [], []
        terms = [t for t in re.split(r"[^\w#]+", query.lower()) if t]
        if terms:
            fts = " AND ".join('"%s"*' % t.replace('"', "") for t in terms)
            where.append("s.id IN (SELECT rowid FROM samples_fts WHERE samples_fts MATCH ?)")
            args.append(fts)
        if bpm is not None:
            # half/double-time loops are usable too
            where.append("(s.bpm BETWEEN ? AND ? OR s.bpm BETWEEN ? AND ? OR s.bpm BETWEEN ? AND ?)")
            for f in (1.0, 0.5, 2.0):
                args += [bpm * f - bpm_tolerance, bpm * f + bpm_tolerance]
        if key:
            where.append("(s.key = ? OR s.key = ?)")
            args += [normalize_key(key), relative_key(normalize_key(key))]
        if loop is not None:
            where.append("s.is_loop = ?")
            args.append(1 if loop else 0)
        for t in tags or []:
            where.append("(' ' || s.tags || ' ') LIKE ?")
            args.append(f"% {t} %")
        if folder:
            where.append("s.folder LIKE ?")
            args.append(f"%{folder}%")
        if max_duration is not None:
            where.append("(s.duration IS NULL OR s.duration <= ?)")
            args.append(max_duration)
        inner = """SELECT s.*, ROW_NUMBER() OVER (PARTITION BY COALESCE(s.hash, s.path)
                                              ORDER BY length(s.path), s.path) AS copy_rank,
                        COUNT(*) OVER (PARTITION BY COALESCE(s.hash, s.path)) AS copies FROM (
            SELECT src.id,src.path,src.name,src.folder,src.tags,p.hash AS hash,
                   COALESCE(json_extract(c.result,'$.bpm'),src.bpm) AS bpm,
                   COALESCE(json_extract(c.result,'$.key'),src.key) AS key,
                   COALESCE(json_extract(c.result,'$.loop'),src.is_loop) AS is_loop,
                   COALESCE(json_extract(c.result,'$.duration'),src.duration) AS duration,
                   c.result AS analysis,src.bpm AS filename_bpm,src.key AS filename_key,src.is_loop AS filename_loop
            FROM samples src LEFT JOIN audio_paths p ON p.path=src.path AND p.size=src.size
                AND abs(p.mtime_ns/1000000000.0-src.mtime)<0.00001 AND p.version='__SIGNAL_VERSION__'
            LEFT JOIN audio_content c ON c.hash=p.hash AND c.version=p.version) s""".replace("__SIGNAL_VERSION__", _SIGNAL_VERSION)
        if where:
            inner += " WHERE " + " AND ".join(where)
        # Identical audio (same content hash) in several folders is listed once, after filtering.
        base = "FROM (" + inner + ") s WHERE s.copy_rank = 1"
        sql = ("SELECT s.path,s.name,s.folder,s.bpm,s.key,s.tags,s.is_loop,s.duration,"
               "s.analysis,s.filename_bpm,s.filename_key,s.filename_loop,s.copies " + base)
        order = " ORDER BY length(s.folder), s.name"
        if bpm is not None:
            order = " ORDER BY abs(s.bpm - %f), s.name" % float(bpm)
        with self._conn() as db:
            total = db.execute("SELECT count(*) " + base, args).fetchone()[0]
            rows = db.execute(sql + order + " LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
        results = [{"path": r[0], "name": r[1], "folder": r[2], "bpm": r[3], "key": r[4],
                    "tags": r[5].split() if r[5] else [], "loop": bool(r[6]),
                    "duration": r[7] if r[7] is not None else _duration(r[0])} for r in rows]
        limitations = None
        for result, row in zip(results, rows, strict=True):
            analysis = json.loads(row[8]) if row[8] else None
            if row[12] and row[12] > 1:
                result["copies"] = row[12]
            result["metadata_sources"] = {}
            result["conflicts"] = []
            for field, hint in (("bpm", row[9]), ("key", row[10]), ("loop", bool(row[11]))):
                measured = analysis.get(field) if analysis else None
                confidence = analysis.get(field + "_confidence", 1) if measured is not None else 0.2
                source = {"source": "audio_estimate" if measured is not None else "filename_hint",
                          "confidence": confidence}
                if detail:
                    source["filename_value"] = hint
                result["metadata_sources"][field] = source
                if measured is not None and hint is not None and (
                        abs(measured - hint) > 3 if field == "bpm" else measured != hint):
                    result["conflicts"].append({"field": field, "audio": measured, "filename": hint})
            result["metadata_sources"]["tags"] = {"source": "filename_hint", "confidence": 0.2}
            if analysis:
                limitations = limitations or analysis.get("limitations")
                result["audio"] = analysis if detail else {k: analysis[k] for k in _AUDIO_SUMMARY if k in analysis}
        out = {"total": total, "results": results, "indexed": self.count, "scanning": self.scanning}
        if limitations and not detail:
            out["limitations"] = limitations
        return out

    def hint_for(self, path: str) -> dict | None:
        with self._conn() as db:
            row = db.execute("SELECT hint FROM samples WHERE path = ?", (path,)).fetchone()
        if row:
            return json.loads(row[0])
        for root in self.roots():
            if _under(path, root.path):
                rel = os.path.relpath(path, root.path).replace("\\", "/").split("/")
                return {"category": root.category, "root": root.browser_root, "rel": rel}
        return None

    def status(self) -> dict[str, Any]:
        return {"indexed": self.count, "scanning": self.scanning, "last_scan": self.last_scan,
                "recovered_from_corrupt_db": self.recovered_from,
                "db": self.db_path,
                "roots": [{"path": r.path, "source": r.source, "browser": r.category} for r in self.roots()]}


# Compact per-hit measurement summary; the full analysis is returned only with detail=True.
_AUDIO_SUMMARY = ("analyzer", "duration", "rms_dbfs", "peak_dbfs", "bpm", "bpm_confidence", "bpm_alternatives",
                  "pitch_hz", "key", "key_confidence", "loop", "loop_confidence", "instrument", "silent",
                  "stereo_width", "low_band_ratio", "spectral_centroid_hz", "onset_count")


def _under(path: str, root: str) -> bool:
    a, b = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root))
    return a == b or a.startswith(b.rstrip("\\/") + os.sep)


def _walk(root: str):
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if not e.name.startswith(".") and e.name != "__MACOSX":
                                stack.append(e.path)
                        elif e.name.startswith("._"):
                            continue  # AppleDouble resource fork, not audio
                        elif os.path.splitext(e.name)[1].lower() in AUDIO_EXT:
                            yield e.path, e.stat()
                    except OSError:
                        continue
        except OSError:
            continue


def normalize_key(key: str) -> str:
    k = key.strip().replace(" ", "")
    low = k.lower()
    minor = low.endswith(("minor", "min")) or (low.endswith("m") and not low.endswith("maj"))
    for suffix in ("minor", "major", "min", "maj", "m", "M"):
        if k.endswith(suffix) and len(k) > len(suffix):
            k = k[: -len(suffix)]
            break
    root = k[0].upper() + k[1:]
    root = _FLATS.get(root, root)
    return root + ("m" if minor else "")


def relative_key(key: str) -> str:
    minor = key.endswith("m")
    root = key[:-1] if minor else key
    if root not in _NOTE_ORDER:
        return key
    i = _NOTE_ORDER.index(root)
    return _NOTE_ORDER[(i + 3) % 12] if minor else _NOTE_ORDER[(i + 9) % 12] + "m"
