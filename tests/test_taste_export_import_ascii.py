import copy
import json

import pytest

from live_agent_bridge.taste.cli import export_memory
from live_agent_bridge.taste.store import TasteStore


def _seed(store):
    kick = store.event("preference", "short kick", event_id="ascii-kick", writer="test/user")
    store.remember("short kick", [kick], status="confirmed", domain="drums", writer="test/user")
    bass = store.event("preference", "open bassline", event_id="ascii-bass", writer="test/user")
    store.remember("open bassline", [bass], status="confirmed", domain="bass", writer="test/user")


def test_ascii_round_trip_keeps_stable_digest_profile_and_recall(tmp_path):
    original = TasteStore(tmp_path / "original.sqlite3")
    _seed(original)
    first = original.export()
    second = original.export()
    assert first == second
    assert original.digest() == original.digest()
    restored = TasteStore(tmp_path / "restored.sqlite3")
    restored.import_data(json.loads(json.dumps(first)))
    assert restored.digest() == original.digest()
    assert restored.export() == first
    for domain in ("drums", "bass"):
        assert restored.profile(domain) == original.profile(domain)
    statements = {p["statement"] for p in restored.profile()["preferences"]}
    assert statements == {"short kick", "open bassline"}
    for query in ("short kick", "open bassline"):
        assert restored.recall(query, 10)["total"] == original.recall(query, 10)["total"] >= 1


def test_malformed_import_entry_rolls_back_everything(tmp_path):
    source = TasteStore(tmp_path / "source.sqlite3")
    _seed(source)
    payload = source.export()
    target = TasteStore(tmp_path / "target.sqlite3")
    eid = target.event("preference", "short kick", event_id="target-kick")
    target.remember("short kick", [eid], status="confirmed", domain="drums")
    before = target.digest()
    broken = copy.deepcopy(payload)
    bad = dict(broken["preferences"][0])
    bad.pop("statement", None)
    bad["id"] = "malformed-pref"
    broken["preferences"] = broken["preferences"] + [bad]
    with pytest.raises((ValueError, KeyError, TypeError)):
        target.import_data(broken)
    assert target.digest() == before
    assert target.recall("open bassline", 10)["total"] == 0


def test_export_refuses_to_overwrite_existing_markdown(tmp_path):
    store = TasteStore(tmp_path / "store.sqlite3")
    _seed(store)
    destination = tmp_path / "memory.json"
    markdown = destination.with_suffix(".md")
    markdown.write_text("keep me", encoding="utf-8")
    with pytest.raises(ValueError):
        export_memory(store, str(destination))
    assert markdown.read_text(encoding="utf-8") == "keep me"
    assert not destination.exists()
