"""Local rekordbox import. Encrypted reads use pyrekordbox on a verified copy only."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import tempfile
import xml.etree.ElementTree as ET
from contextlib import closing
from pathlib import Path

from .paths import data_dir
from .store import TasteStore, json_text, utc_now


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _number(value, scale=1):
    try:
        return float(value) / scale
    except (ValueError, TypeError):
        return None


def _xml_tracks(source: Path):
    # XML exports contain no audio. iterparse keeps memory bounded for large libraries.
    with source.open("rb") as handle:
        head = handle.read(4096).upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
        raise ValueError("DTD/entity XML is unsupported")
    for _, element in ET.iterparse(source, events=("end",)):
        if element.tag == "TRACK" and "Name" in element.attrib:
            a = element.attrib
            yield str(a.get("TrackID") or hashlib.sha256(json_text(a).encode()).hexdigest()), a["Name"], {
                "artist": a.get("Artist", ""), "album": a.get("Album", ""), "genre": a.get("Genre", ""),
                "bpm": _number(a.get("AverageBpm")), "key": a.get("Tonality"),
                "rating": _number(a.get("Rating")), "play_count": _number(a.get("PlayCount")),
                "isrc": a.get("ISRC")}
        element.clear()


def _master_tracks(copy: Path, encrypted: bool):
    if not encrypted:
        # Standard SQLite exports/test databases need no decryption package.
        with closing(sqlite3.connect(copy.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM djmdContent")
            for row in rows:
                data = dict(row)
                links = {}
                for label, table, foreign, column in (("artist", "djmdArtist", "ArtistID", "Name"),
                        ("genre", "djmdGenre", "GenreID", "Name"), ("key", "djmdKey", "KeyID", "ScaleName")):
                    if data.get(foreign):
                        linked = db.execute(f"SELECT {column} FROM {table} WHERE ID=?", (data[foreign],)).fetchone()
                        links[label] = linked[0] if linked else None
                yield str(data["ID"]), str(data.get("Title", "")), dict(links, bpm=_number(data.get("BPM"), 100),
                        rating=_number(data.get("Rating")), play_count=_number(data.get("DJPlayCount")), isrc=data.get("ISRC"))
        return
    try:
        from pyrekordbox import MasterDatabase
    except ImportError:
        raise ValueError('Encrypted import needs pyrekordbox and SQLCipher; use rekordbox XML or install ".[rekordbox]"') from None
    # The upstream reader can update metadata; its only writable target is this temporary copy.
    # Never invoke MasterDatabase() without explicit paths or autocommit disabled.
    db = MasterDatabase(path=copy, db_dir=copy.parent, unlock=True, autocommit=False)
    try:
        for track in db.get_content():
            def name(attribute, field="Name", track=track):
                linked = getattr(track, attribute, None)
                return getattr(linked, field, None) if linked is not None else None

            yield str(track.ID), str(track.Title or ""), {
                "artist": name("Artist"), "genre": name("Genre"), "key": name("Key", "ScaleName"),
                "bpm": _number(getattr(track, "BPM", None), 100), "rating": getattr(track, "Rating", None),
                "play_count": getattr(track, "DJPlayCount", None), "isrc": getattr(track, "ISRC", None)}
    finally:
        db.close()
        db.engine.dispose()


def import_rekordbox(source: str, store: TasteStore) -> dict:
    path = Path(source).resolve(strict=True)
    if path.suffix.lower() not in (".xml", ".db") or path.stat().st_size > 2_000_000_000:
        raise ValueError("Choose a rekordbox XML export or database snapshot under 2 GB")
    source_hash, before = _hash(path), path.stat()
    staging = data_dir() / "import-staging"
    staging.mkdir(parents=True, exist_ok=True)
    # Preserve only normalized metadata in memory. Do not copy songs, artwork or analysis files.
    with tempfile.TemporaryDirectory(prefix="rekordbox-", dir=staging) as temporary:
        copy = Path(temporary) / path.name
        if path.suffix.lower() == ".xml":
            shutil.copyfile(path, copy)
            tracks = list(_xml_tracks(copy))
        else:
            with path.open("rb") as stream:
                encrypted = stream.read(16) != b"SQLite format 3\x00"
            if encrypted:
                if any(Path(str(path) + suffix).exists() and Path(str(path) + suffix).stat().st_size
                       for suffix in ("-wal", "-journal")):
                    raise ValueError("Database has an active journal; supply a clean backup or XML export")
                shutil.copyfile(path, copy)
                if _hash(copy) != source_hash:
                    raise ValueError("Snapshot copy failed verification")
            else:
                origin = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
                try:
                    with closing(sqlite3.connect(copy)) as target:
                        origin.backup(target)
                finally:
                    origin.close()
            tracks = list(_master_tracks(copy, encrypted))
        snapshot_hash = _hash(copy)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, source_hash) != (after.st_size, after.st_mtime_ns, _hash(path)):
            raise ValueError("Source library changed during import; no memory was imported")
    # Assemble into a separate memory first; malformed tracks cannot partially modify the live memory.
    with tempfile.TemporaryDirectory(prefix="rekordbox-memory-", dir=staging) as temporary:
        prepared = TasteStore(Path(temporary) / "taste.db")
        event_id = "rekordbox:" + snapshot_hash
        with store.connection() as db:
            previous = db.execute("SELECT occurred_at FROM events WHERE id=?", (event_id,)).fetchone()
        prepared.event("library_import", "rekordbox library snapshot", source="rekordbox", event_id=event_id,
                       data={"sha256": snapshot_hash, "tracks": len(tracks)}, occurred_at=previous[0] if previous else utc_now(),
                       writer="rekordbox-import")
        for tid, title, data in tracks:
            prepared.fact(source="rekordbox", source_id=tid, name=title, data=data, event_id=event_id)
        imported = store.import_data(prepared.export(), replace_source="rekordbox")
    return {**imported, "tracks": len(tracks), "source_sha256": source_hash, "original_unchanged": True,
            "note": "Library snapshot only; event time is import time, not a listening timestamp."}
