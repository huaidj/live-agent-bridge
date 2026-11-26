"""macOS archive leftovers (__MACOSX folders, AppleDouble '._' files) are not audio and must not
be indexed: on the real library they were 45 of the 231 analysis failures."""

from test_sample_index_roots import _index, _wav


def test_macos_resource_forks_are_skipped(tmp_path, monkeypatch):
    ix = _index(tmp_path, monkeypatch)
    root = tmp_path / "pack"
    _wav(root / "kick_real.wav")
    _wav(root / "._kick_real.wav")
    _wav(root / "__MACOSX" / "pack" / "._kick_real.wav")
    _wav(root / "__MACOSX" / "pack" / "kick_shadow.wav")
    ix.add_root(str(root))
    ix.scan()
    assert [r["name"] for r in ix.search("kick")["results"]] == ["kick_real.wav"]
