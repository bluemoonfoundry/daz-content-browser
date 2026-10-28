"""Binary read/write for the .tmb (Turbo Morph Binary) format.

Layout:
  HEADER (16 bytes): magic b"TMB1" (4) | vertex_count int32 (4) |
                      delta_count uint32 (4) | reserved (4, zero)
  DATA: delta_count x { vertex_index uint32, dx float32, dy float32, dz float32 }

vertex_count is signed: DAZ .dsf files legitimately use -1 as a documented
DSON sentinel meaning "unspecified/same as base mesh".
"""

import struct

_MAGIC = b"TMB1"
_HEADER = struct.Struct("<4siI4x")
_DELTA = struct.Struct("<Ifff")


def write_tmb(path: str, vertex_count: int, deltas) -> None:
    """Writes a .tmb file. `deltas` is an iterable of (vertex_index, dx, dy, dz)."""
    deltas = list(deltas)
    with open(path, "wb") as f:
        f.write(_HEADER.pack(_MAGIC, vertex_count, len(deltas)))
        for vertex_index, dx, dy, dz in deltas:
            f.write(_DELTA.pack(vertex_index, dx, dy, dz))


def read_tmb(path: str):
    """Reads a .tmb file, returning (vertex_count, deltas) where deltas is a
    list of (vertex_index, dx, dy, dz) tuples."""
    with open(path, "rb") as f:
        header = f.read(_HEADER.size)
        if len(header) < _HEADER.size:
            raise ValueError("Truncated .tmb file: header too short")
        magic, vertex_count, delta_count = _HEADER.unpack(header)
        if magic != _MAGIC:
            raise ValueError(f"Not a TMB file (bad magic bytes: {magic!r})")

        # Check if remaining file has enough bytes for all deltas
        remaining = f.read()
        if len(remaining) < delta_count * _DELTA.size:
            raise ValueError(
                f"Truncated .tmb file: expected {delta_count} deltas "
                f"({delta_count * _DELTA.size} bytes) but file only has {len(remaining)} bytes"
            )

        deltas = []
        for i in range(delta_count):
            offset = i * _DELTA.size
            delta_bytes = remaining[offset:offset + _DELTA.size]
            deltas.append(_DELTA.unpack(delta_bytes))
    return vertex_count, deltas
