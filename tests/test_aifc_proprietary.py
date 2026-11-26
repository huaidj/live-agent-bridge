"""Ableton Factory Pack .aif files are AIFC with compression 'able': no local decoder reads them.

Real case: 346 library files failed with an opaque LibsndfileError ("unimplemented format").
They must fail with a specific, non-retryable reason, while ordinary AIFC/AIFF still decodes.
"""

import struct

import pytest

from live_agent_bridge.audio.features import UndecodableAudio, aifc_compression, check_decodable


def _aifc(path, codec, extra_chunk=True):
    comm = struct.pack(">hIh", 1, 0, 16) + b"\x40\x0e\xac\x44" + b"\x00" * 6 + codec + b"\x00\x00"
    chunks = b""
    if extra_chunk:  # FVER precedes COMM in Ableton's files
        chunks += b"FVER" + struct.pack(">I", 4) + b"\xa2\x80\x51\x40"
    chunks += b"COMM" + struct.pack(">I", len(comm)) + comm
    chunks += b"SSND" + struct.pack(">I", 8) + b"\x00" * 8
    path.write_bytes(b"FORM" + struct.pack(">I", 4 + len(chunks)) + b"AIFC" + chunks)
    return str(path)


def test_able_codec_detected_and_rejected(tmp_path):
    p = _aifc(tmp_path / "Kick.aif", b"able")
    assert aifc_compression(p) == b"able"
    with pytest.raises(UndecodableAudio, match="only Live can decode"):
        check_decodable(p)


def test_able_rejected_by_analyze_file(tmp_path):
    pytest.importorskip("soundfile")
    from live_agent_bridge.audio.features import analyze_file

    with pytest.raises(UndecodableAudio):
        analyze_file(_aifc(tmp_path / "Kick.aif", b"able"))


@pytest.mark.parametrize("codec", [b"NONE", b"sowt", b"fl32"])
def test_standard_aifc_passes_check(tmp_path, codec):
    check_decodable(_aifc(tmp_path / "ok.aif", codec, extra_chunk=False))


def test_non_aifc_files_pass(tmp_path):
    wav = tmp_path / "a.aif"
    wav.write_bytes(b"FORM\x00\x00\x00\x04AIFF")
    assert aifc_compression(str(wav)) is None
    check_decodable(str(wav))
    truncated = tmp_path / "t.aif"
    truncated.write_bytes(b"FOR")
    check_decodable(str(truncated))
