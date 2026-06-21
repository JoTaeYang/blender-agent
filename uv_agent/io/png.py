"""Minimal pure-Python PNG writer (stdlib ``zlib`` only).

The MVP 1 UV layout PNG must be produced **headless**: Blender 5.x's
``bpy.ops.uv.export_layout`` PNG mode requires GPU drawing, which is unavailable
in ``blender --background`` (it raises "GPU functions for drawing are not
available in background mode"). So the UV layout is rasterized with NumPy and
encoded here, with no Blender, no Pillow, and no GPU — it works everywhere the
engine runs and is therefore unit-testable offline (plan §7, §13).
"""

from __future__ import annotations

import struct
import zlib


def write_png(path: str, rgba) -> str:
    """Write an ``(H, W, 4)`` uint8 RGBA array to ``path`` as a PNG. Returns ``path``."""
    import numpy as np

    arr = np.ascontiguousarray(rgba, dtype=np.uint8)
    if arr.ndim != 3 or arr.shape[2] != 4:
        raise ValueError(f"expected (H, W, 4) RGBA array, got shape {arr.shape}")
    height, width = arr.shape[0], arr.shape[1]

    # Each scanline is prefixed with a filter-type byte (0 = none).
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw.extend(arr[y].tobytes())

    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit, RGBA
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _chunk(b"IEND", b"")
    )
    with open(path, "wb") as fh:
        fh.write(png)
    return path
