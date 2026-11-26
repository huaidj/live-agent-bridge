import copy
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from live_agent_bridge.taste.cli import export_memory
from live_agent_bridge.taste.store import TasteStore
from live_agent_bridge.taste.tools import standalone


def test_evidence_confirmation_retirement_and_forgetting(tmp_path):
    store = TasteStore(tmp_path / "taste.db")
    with pytest.raises(ValueError, match="evidence"):
        store.remember("short kicks", [])
    inferred = store.event("preference", "short kicks", source="inference")
    with pytest.raises(ValueError, match="explicit"):
        store.remember("short kicks", [inferred], status="confirmed")
    pending = store.remember("short kicks", [inferred])
    assert store.profile()["preferences"] == []
    explicit = store.event("preference", "我喜歡短促的底鼓", writer="test/user")
    pref = store.remember("我喜歡短促的底鼓", [explicit], status="confirmed", domain="drums")
    assert store.recall("底鼓")["total"] == 2
    assert store.profile("drums")["preferences"][0]["evidence"] == [explicit]
    store.remember(pref["statement"], [explicit], preference_id=pref["id"], status="retired", domain="drums")
    assert store.profile()["preferences"] == []
    assert store.profile()["retired"][0]["id"] == pref["id"]
    with pytest.raises(ValueError, match="Retired"):
        store.remember(pref["statement"], [inferred], preference_id=pref["id"])
    store.forget(preference_id=pref["id"])
    assert store.recall("底鼓")["total"] == 0
    assert store.profile()["pending_count"] == 1
    store.forget(preference_id=pending["id"])


def test_inference_cannot_replace_confirmed_memory_and_conflicts_remain_visible(tmp_path):
    store = TasteStore(tmp_path / "taste.db")
    eid = store.event("preference", "Prefer sparse bass")
    first = store.remember("Prefer sparse bass", [eid], status="confirmed", domain="bass")
    inferred = store.event("preference", "Prefer dense bass", source="inference")
    with pytest.raises(ValueError, match="downgrade"):
        store.remember("Prefer dense bass", [inferred], preference_id=first["id"])
    second_id = store.event("preference", "Prefer dense bass")
    second = store.remember("Prefer dense bass", [second_id], status="confirmed", domain="bass", contradicts=[first["id"]])
    profile = store.profile()
    assert len(profile["preferences"]) == 2 and len(profile["conflicts"]) == 1
    assert all(p["needs_user_resolution"] for p in profile["preferences"])
    assert "sparse" not in profile["summary"] and "dense" not in profile["summary"]
    store.remember(second["statement"], [second_id], preference_id=second["id"], status="retired", domain="bass")
    assert store.profile()["conflicts"] == []
    assert store.profile()["summary"] == "Prefer sparse bass"


def test_export_round_trip_and_conflicting_import_rolls_back(tmp_path):
    original = TasteStore(tmp_path / "one.db")
    eid = original.event("preference", "sparse bass", event_id="e1")
    original.remember("sparse bass", [eid], status="confirmed", writer="provider-one")
    payload = original.export()
    restored = TasteStore(tmp_path / "two.db")
    restored.import_data(payload)
    assert restored.digest() == original.digest()
    assert restored.profile() == original.profile()
    assert all(v == 0 for v in restored.import_data(payload)["imported"].values())
    changed = copy.deepcopy(payload)
    new = dict(changed["events"][0], id="brand-new")
    changed["events"] = [new] + changed["events"]
    changed["preferences"][0]["statement"] = "changed without approval"
    before = restored.digest()
    with pytest.raises(ValueError, match="Conflicting"):
        restored.import_data(changed)
    assert restored.digest() == before
    destination = tmp_path / "export.json"
    export_memory(original, str(destination))
    assert json.loads(destination.read_text(encoding="utf-8")) == payload
    assert "sparse bass" in destination.with_suffix(".md").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="new JSON"):
        export_memory(original, str(destination))


def test_sources_tokens_and_invalid_schema_are_rejected(tmp_path):
    store = TasteStore(tmp_path / "one.db")
    with pytest.raises(ValueError, match="Unsupported"):
        store.event("play", "unknown song", source="unknown-service")
    with pytest.raises(ValueError, match="Credentials"):
        store.event("preference", "bad", data={"refresh_token": "never-store-this"})
    payload = store.export()
    payload["schema_version"] = 99
    with pytest.raises(ValueError, match="version"):
        store.import_data(payload)
    assert store.profile()["references"] == []


def test_parallel_writers_and_source_delete_recompute(tmp_path):
    path = tmp_path / "shared.db"
    TasteStore(path)

    def write(i):
        store = TasteStore(path)
        return store.event("choice", str(i), event_id=f"choice-{i}")

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert len(set(pool.map(write, range(30)))) == 30
    store = TasteStore(path)
    event = store.event("library_import", "test", source="rekordbox", event_id="rb")
    store.fact(source="rekordbox", source_id="1", name="Test", data={"bpm": 126, "genre": "House"}, event_id=event)
    assert store.profile()["library_distribution"]["bpm"][0]["count"] == 1
    store.forget(source="rekordbox")
    assert store.profile()["library_distribution"] == {}
    assert store.profile()["evidence"]["event_counts"] == {"user": 30}


async def test_standalone_mcp_has_no_live_dependency(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_DATA_DIR", str(tmp_path))
    first = standalone()
    async with create_connected_server_and_client_session(first._mcp_server) as session:
        names = {t.name for t in (await session.list_tools()).tools}
        assert names == {"taste_profile", "taste_recall", "taste_remember", "music_reference"}
        response = await session.call_tool("taste_remember", {"statement": "Prefer sparse bass", "origin": "user",
                                                            "writer": "host-one/model-one", "domain": "bass"})
        assert not response.isError
        profile = await session.call_tool("taste_profile", {})
        initial = json.loads(profile.content[0].text)
        assert initial["preferences"][0]["statement"] == "Prefer sparse bass"
        invalid = await session.call_tool("taste_remember", {"statement": "guess", "action": "confirm",
                                                           "origin": "inference", "preference_id": "fake"})
        assert invalid.isError
    # A new server (another provider/session) reads the identical on-disk memory.
    async with create_connected_server_and_client_session(standalone()._mcp_server) as session:
        profile = await session.call_tool("taste_profile", {})
        assert json.loads(profile.content[0].text) == initial


async def test_real_stdio_start_initialize_discover_call_and_exit(tmp_path):
    params = StdioServerParameters(command=sys.executable, args=["-m", "live_agent_bridge.cli", "--data-dir",
                                   str(tmp_path), "taste-serve"], env=dict(os.environ))
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        initialized = await session.initialize()
        assert initialized.serverInfo.name == "local-music-memory"
        assert len((await session.list_tools()).tools) == 4
        response = await session.call_tool("taste_profile", {})
        assert not response.isError
        assert json.loads(response.content[0].text)["preferences"] == []


def test_two_real_processes_share_wal_memory(tmp_path):
    path = tmp_path / "shared.db"
    TasteStore(path)
    code = ("import sys; from live_agent_bridge.taste.store import TasteStore; "
            "s=TasteStore(sys.argv[1]); "
            "[s.event('choice',str(i),event_id=sys.argv[2]+str(i)) for i in range(20)]")
    children = [subprocess.Popen([sys.executable, "-c", code, str(path), prefix],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE) for prefix in ("one", "two")]
    for child in children:
        _, errors = child.communicate(timeout=15)
        assert child.returncode == 0, errors.decode(errors="replace")
    assert TasteStore(path).profile()["evidence"]["event_counts"] == {"user": 40}
