"""Offline edge cases for the local rekordbox importer (tiny inline ASCII fixtures only)."""

import hashlib

import pytest

from live_agent_bridge.taste import rekordbox
from live_agent_bridge.taste.rekordbox import import_rekordbox
from live_agent_bridge.taste.store import TasteStore, json_text


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(rekordbox, 'data_dir', lambda *a, **k: tmp_path / 'data')
    return TasteStore(tmp_path / 'memory.db')


def counts(store):
    with store.connection() as db:
        return tuple(db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ('events', 'facts'))


def test_doctype_and_entity_xml_rejected_without_imports(tmp_path, store):
    source = tmp_path / 'lib.xml'
    source.write_text('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><DJ_PLAYLISTS><COLLECTION>'
                      '<TRACK TrackID="1" Name="&a;"/></COLLECTION></DJ_PLAYLISTS>', encoding='ascii')
    with pytest.raises(ValueError):
        import_rekordbox(str(source), store)
    assert counts(store) == (0, 0)


def test_unsupported_suffix_rejected(tmp_path, store):
    source = tmp_path / 'lib.txt'
    source.write_text('<DJ_PLAYLISTS/>', encoding='ascii')
    with pytest.raises(ValueError):
        import_rekordbox(str(source), store)
    assert counts(store) == (0, 0)


def test_xml_skips_nameless_hashes_missing_id_and_handles_numbers(tmp_path):
    source = tmp_path / 'lib.xml'
    source.write_text('<?xml version="1.0" encoding="UTF-8"?><DJ_PLAYLISTS><COLLECTION>'
                      '<TRACK TrackID="9" Artist="Nobody"/>'
                      '<TRACK Name="No ID" AverageBpm="fast" Rating="x" PlayCount="7"/>'
                      '</COLLECTION></DJ_PLAYLISTS>', encoding='ascii')
    tracks = list(rekordbox._xml_tracks(source))
    assert len(tracks) == 1
    tid, title, data = tracks[0]
    attrib = {'Name': 'No ID', 'AverageBpm': 'fast', 'Rating': 'x', 'PlayCount': '7'}
    assert tid == hashlib.sha256(json_text(attrib).encode()).hexdigest()
    assert tid == list(rekordbox._xml_tracks(source))[0][0]
    assert title == 'No ID'
    assert data['bpm'] is None and data['rating'] is None
    assert data['play_count'] == 7
    assert rekordbox._number('12800', 100) == 128
    assert rekordbox._number('abc') is None
    assert rekordbox._number(None) is None


def test_non_sqlite_snapshot_with_journal_refused(tmp_path, store):
    source = tmp_path / 'master.db'
    source.write_bytes(b'not a sqlite database, encrypted placeholder')
    (tmp_path / 'master.db-wal').write_bytes(b'pending')
    with pytest.raises(ValueError, match='journal'):
        import_rekordbox(str(source), store)
    assert counts(store) == (0, 0)
    assert source.read_bytes() == b'not a sqlite database, encrypted placeholder'
