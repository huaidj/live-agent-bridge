"""Removing or losing a sample root must drop its entries from the index on the next scan."""

import shutil

from live_agent_bridge.samples import SampleIndex


def _wav(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF0000WAVE")


def _index(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "noappdata"))
    monkeypatch.delenv("AGENT_BRIDGE_SAMPLE_DIRS", raising=False)
    # Only the folders a test adds; never the machine's real Live/Splice libraries.
    monkeypatch.setattr("live_agent_bridge.samples.discover_roots", lambda: [])
    return SampleIndex(str(tmp_path / "samples.db"))


def test_remove_folder_drops_its_samples_on_rescan(tmp_path, monkeypatch):
    ix = _index(tmp_path, monkeypatch)
    keep, drop = tmp_path / "keep", tmp_path / "drop"
    _wav(keep / "kick_keep.wav")
    _wav(drop / "kick_drop.wav")
    ix.add_root(str(keep))
    ix.add_root(str(drop))
    ix.scan()
    assert ix.search("kick")["total"] == 2
    ix.remove_root(str(drop))
    result = ix.scan()
    names = [r["name"] for r in ix.search("kick")["results"]]
    assert names == ["kick_keep.wav"]
    assert result["removed"] == 1


def test_root_deleted_from_disk_is_dropped(tmp_path, monkeypatch):
    ix = _index(tmp_path, monkeypatch)
    gone = tmp_path / "usb"
    _wav(gone / "snare_usb.wav")
    ix.add_root(str(gone))
    ix.scan()
    assert ix.count == 1
    shutil.rmtree(gone)
    ix.scan()
    assert ix.count == 0
    assert ix.search("snare")["total"] == 0
