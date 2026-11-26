import hashlib
import sqlite3

from live_agent_bridge.taste.rekordbox import import_rekordbox
from live_agent_bridge.taste.store import TasteStore


def test_xml_import_idempotency_original_unchanged_and_update(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_DATA_DIR", str(tmp_path / "state"))
    source = tmp_path / "rekordbox.xml"
    xml = '<DJ_PLAYLISTS><COLLECTION><TRACK TrackID="42" Name="Local Music" Artist="Artist" Genre="House" AverageBpm="126" Tonality="Am" /></COLLECTION></DJ_PLAYLISTS>'
    source.write_text(xml, encoding="utf-8")
    before = source.stat().st_mtime_ns, hashlib.sha256(source.read_bytes()).hexdigest()
    store = TasteStore(tmp_path / "taste.db")
    result = import_rekordbox(str(source), store)
    assert result["tracks"] == 1 and result["original_unchanged"]
    digest = store.digest()
    import_rekordbox(str(source), store)
    assert store.digest() == digest
    assert (source.stat().st_mtime_ns, hashlib.sha256(source.read_bytes()).hexdigest()) == before
    assert store.profile()["library_distribution"]["bpm"][0]["value"] == "120–129"
    source.write_text(xml.replace('AverageBpm="126"', 'AverageBpm="134"'), encoding="utf-8")
    import_rekordbox(str(source), store)
    assert store.profile()["library_distribution"]["bpm"][0]["value"] == "130–139"


def test_sqlite_copy_reads_local_data_and_does_not_change_original(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_DATA_DIR", str(tmp_path / "state"))
    source = tmp_path / "library.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE djmdContent(ID TEXT,Title TEXT,BPM INTEGER,Rating INTEGER,DJPlayCount INTEGER)")
        db.execute("INSERT INTO djmdContent VALUES ('1','Track',12800,4,7)")
    before = hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_mtime_ns
    store = TasteStore(tmp_path / "taste.db")
    import_rekordbox(str(source), store)
    assert store.profile()["library_distribution"]["bpm"][0]["value"] == "120–129"
    assert (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_mtime_ns) == before
