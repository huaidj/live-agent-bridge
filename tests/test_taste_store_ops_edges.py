import pytest

from live_agent_bridge.taste.store import TasteStore


def _store(tmp_path, name="taste.sqlite3"):
    return TasteStore(tmp_path / name)


def _confirm(store, statement, domain):
    eid = store.event("preference", statement)
    return store.remember(statement, [eid], status="confirmed", domain=domain)


def test_recall_empty_match_and_forget_validation(tmp_path):
    store = _store(tmp_path)
    _confirm(store, "Prefer sparse bass", "bass")
    assert store.recall("zzzunmatchedqqq")["total"] == 0
    first = store.recall("sparse")["total"]
    assert first >= 1
    assert store.recall("sparse")["total"] == first
    with pytest.raises(ValueError):
        store.forget()
    with pytest.raises(ValueError):
        store.forget(source="user", all_data=True)


def test_profile_domain_filtering(tmp_path):
    store = _store(tmp_path)
    _confirm(store, "Prefer short kicks", "drums")
    _confirm(store, "Prefer sparse bass", "bass")
    unfiltered = {p["statement"] for p in store.profile()["preferences"]}
    assert unfiltered == {"Prefer short kicks", "Prefer sparse bass"}
    drums = [p["statement"] for p in store.profile("drums")["preferences"]]
    assert drums == ["Prefer short kicks"]
    bass = [p["statement"] for p in store.profile("bass")["preferences"]]
    assert bass == ["Prefer sparse bass"]
    assert store.profile("unknown-domain")["preferences"] == []


def test_backup_and_digest(tmp_path):
    store = _store(tmp_path)
    _confirm(store, "Prefer sparse bass", "bass")
    digest = store.digest()
    assert store.digest() == digest
    target = tmp_path / "backups" / "copy.sqlite3"
    store.backup(target)
    assert target.exists()
    assert TasteStore(target).digest() == digest
    with pytest.raises(ValueError, match="new file"):
        store.backup(target)
    with pytest.raises(ValueError, match="new file"):
        store.backup(tmp_path / "taste.sqlite3")
    assert store.digest() == digest
    _confirm(store, "Prefer short kicks", "drums")
    assert store.digest() != digest


def test_clear_all_data_preserves_usability(tmp_path):
    store = _store(tmp_path)
    _confirm(store, "Prefer sparse bass", "bass")
    event = store.event("library_import", "test", source="rekordbox", event_id="rb")
    store.fact(source="rekordbox", source_id="1", name="Test", data={"bpm": 126, "genre": "House"}, event_id=event)
    result = store.forget(all_data=True)
    assert result["scope"] == "all"
    assert result["deleted"] >= 2
    profile = store.profile()
    assert profile["preferences"] == []
    assert profile["library_distribution"] == {}
    assert store.recall("sparse")["total"] == 0
    assert store.forget(all_data=True)["deleted"] == 0
    _confirm(store, "Prefer warm pads", "synth")
    assert store.recall("warm")["total"] >= 1
    assert [p["statement"] for p in store.profile()["preferences"]] == ["Prefer warm pads"]
