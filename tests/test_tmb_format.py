import struct

import pytest
from tmb_format import write_tmb, read_tmb


def test_write_then_read_round_trip(tmp_path):
    path = tmp_path / "test.tmb"
    deltas = [(0, -0.2948112, 0.6714706, -2.386154), (23368, 0.0, 0.0, 0.0)]

    write_tmb(str(path), vertex_count=23369, deltas=deltas)
    vertex_count, read_deltas = read_tmb(str(path))

    assert vertex_count == 23369
    assert len(read_deltas) == 2
    for original, roundtripped in zip(deltas, read_deltas):
        assert roundtripped[0] == original[0]
        assert roundtripped[1] == pytest.approx(original[1], abs=1e-6)
        assert roundtripped[2] == pytest.approx(original[2], abs=1e-6)
        assert roundtripped[3] == pytest.approx(original[3], abs=1e-6)


def test_write_empty_deltas(tmp_path):
    path = tmp_path / "empty.tmb"
    write_tmb(str(path), vertex_count=100, deltas=[])
    vertex_count, deltas = read_tmb(str(path))
    assert vertex_count == 100
    assert deltas == []


def test_read_rejects_bad_magic(tmp_path):
    path = tmp_path / "bad.tmb"
    path.write_bytes(b"NOPE" + b"\x00" * 12)
    with pytest.raises(ValueError, match="magic"):
        read_tmb(str(path))


def test_header_is_16_bytes_and_delta_is_16_bytes(tmp_path):
    path = tmp_path / "sizes.tmb"
    write_tmb(str(path), vertex_count=5, deltas=[(1, 1.0, 2.0, 3.0)])
    data = path.read_bytes()
    assert len(data) == 16 + 16


def test_read_rejects_file_under_16_bytes(tmp_path):
    """A .tmb file shorter than the 16-byte header should raise ValueError."""
    path = tmp_path / "short.tmb"
    path.write_bytes(b"TMB1" + b"\x00" * 10)  # Only 14 bytes total
    with pytest.raises(ValueError, match="header too short"):
        read_tmb(str(path))


def test_read_rejects_truncated_deltas(tmp_path):
    """A .tmb file with a header claiming more deltas than the file contains."""
    path = tmp_path / "truncated.tmb"
    # Write a valid 16-byte header claiming 2 deltas, but provide only 1 delta
    # (or incomplete delta data).
    header_data = struct.pack("<4siI4x", b"TMB1", 100, 2)  # vertex_count=100, delta_count=2
    one_delta = struct.pack("<Ifff", 0, 1.0, 2.0, 3.0)
    path.write_bytes(header_data + one_delta)  # Only 16 + 16 = 32 bytes total
    with pytest.raises(ValueError, match="Truncated|too short"):
        read_tmb(str(path))


def test_write_then_read_round_trip_with_negative_vertex_count(tmp_path):
    # Real DAZ .dsf files legitimately use vertex_count=-1 as a documented
    # DSON sentinel meaning "unspecified" -- confirmed against the user's
    # real library (e.g. several morphs under "Aave Nainen/Civilized Man").
    path = tmp_path / "sentinel.tmb"
    deltas = [(0, 1.0, 2.0, 3.0)]

    write_tmb(str(path), vertex_count=-1, deltas=deltas)
    vertex_count, read_deltas = read_tmb(str(path))

    assert vertex_count == -1
    assert len(read_deltas) == 1
