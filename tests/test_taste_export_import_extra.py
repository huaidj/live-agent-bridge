import json

from live_agent_bridge.taste.cli import export_memory
from live_agent_bridge.taste.store import TasteStore

CONFIRMED = "偏好 溫暖 的 貝斯"
PENDING = "可能 喜歡 明亮 的 鼓聲"


def _seed(path):
    store = TasteStore(path)
    explicit = store.event("preference", CONFIRMED, event_id="使用者-1", writer="test/user")
    confirmed = store.remember(CONFIRMED, [explicit], status="confirmed", domain="貝斯", writer="test/user")
    inferred = store.event("preference", PENDING, source="inference", event_id="推論-1")
    pending = store.remember(PENDING, [inferred], domain="鼓組")
    return store, confirmed, pending


def _text(value):
    return json.dumps(value, ensure_ascii=False)


def test_unicode_confirmed_and_pending_profile_and_recall(tmp_path):
    store, confirmed, pending = _seed(tmp_path / "記憶.sqlite3")
    profile = store.profile()
    statements = [p["statement"] for p in profile["preferences"]]
    assert statements == [CONFIRMED]
    assert PENDING not in statements
    assert profile["pending_count"] == 1
    found = store.recall("溫暖")
    assert found["total"] >= 1
    assert CONFIRMED in _text(found)
    guessed = store.recall("明亮")
    assert guessed["total"] >= 1
    assert PENDING in _text(guessed)
    assert "pending" in _text(guessed)


def test_unicode_json_and_markdown_export(tmp_path):
    store, _, _ = _seed(tmp_path / "記憶.sqlite3")
    payload = store.export()
    destination = tmp_path / "匯出" / "品味.json"
    destination.parent.mkdir()
    export_memory(store, str(destination))
    raw = destination.read_bytes().decode("utf-8")
    assert json.loads(raw) == payload
    assert CONFIRMED in _text(json.loads(raw))
    markdown = destination.with_suffix(".md").read_text(encoding="utf-8")
    assert CONFIRMED in markdown


def test_unicode_import_round_trip_preserves_status_and_recall(tmp_path):
    original, confirmed, pending = _seed(tmp_path / "原始.sqlite3")
    destination = tmp_path / "品味.json"
    export_memory(original, str(destination))
    payload = json.loads(destination.read_text(encoding="utf-8"))
    restored = TasteStore(tmp_path / "還原.sqlite3")
    restored.import_data(payload)
    assert restored.digest() == original.digest()
    assert restored.profile() == original.profile()
    assert [p["statement"] for p in restored.profile()["preferences"]] == [CONFIRMED]
    assert restored.profile()["pending_count"] == 1
    assert CONFIRMED in _text(restored.recall("溫暖"))
    assert PENDING in _text(restored.recall("明亮"))
    assert all(v == 0 for v in restored.import_data(payload)["imported"].values())
