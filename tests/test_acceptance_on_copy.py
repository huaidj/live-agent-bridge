"""The copy-set acceptance runner must never switch away from a set with unsaved changes."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "acceptance_on_copy", Path(__file__).resolve().parents[1] / "scripts" / "acceptance_on_copy.py")
aoc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aoc)


def test_parse_title():
    assert aoc.parse_title("ai1* - Ableton Live 12 Suite") == ("ai1", True)
    assert aoc.parse_title("ai1 - Ableton Live 12 Suite") == ("ai1", False)
    assert aoc.parse_title(None) == (None, False)
    assert aoc.parse_title("Untitled") == (None, False)


def test_preflight_refuses_unsaved_wrong_set_and_missing(tmp_path):
    als = tmp_path / "ai1.als"
    als.write_bytes(b"x")
    assert "unsaved" in aoc.preflight(als, "ai1* - Ableton Live 12 Suite")
    assert "not 'ai1'" in aoc.preflight(als, "other - Ableton Live 12 Suite")
    assert "not running" in aoc.preflight(als, None)
    assert "not an .als" in aoc.preflight(tmp_path / "nope.als", "ai1 - Ableton Live 12 Suite")
    assert aoc.preflight(als, "ai1 - Ableton Live 12 Suite") is None


def test_copy_sits_next_to_original(tmp_path):
    p = aoc.copy_path(tmp_path / "ai1.als")
    assert p.parent == tmp_path and p.name == "ai1 ZZ-acceptance-copy.als"
