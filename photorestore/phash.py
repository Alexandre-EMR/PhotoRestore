"""Perceptual hash (pHash) in pure stdlib — zero-dependency policy.

v0 decodes 8-bit non-interlaced PNGs only (color types 0/2/4/6) with
zlib + struct; every other format returns None and is simply skipped by
the perceptual rules (R5, later L1 dedup) instead of failing.

pHash algorithm (classic DCT, same shape as imagehash.phash):
  grayscale -> box-resize to 32x32 -> DCT-II -> top-left hash_size x hash_size
  coefficients -> bit = coeff > median -> hex string (hash_size^2 bits).

This module is the foundation for rule R5 (perceptual proposal) and, in P3,
for L1 near-duplicate detection in dedup.py.
"""

from __future__ import annotations

import math
import statistics
import struct
import zlib
from pathlib import Path

_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_DCT_SIZE = 32  # resize target before DCT (imagehash convention)
_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}


def hamming_hex(a: str, b: str) -> int:
    """Hamming distance between two hex-encoded hashes of equal bit length."""
    if len(a) != len(b):
        raise ValueError(f"hash length mismatch: {len(a)} vs {len(b)}")
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def phash_file(path: Path, hash_size: int = 8) -> str | None:
    """pHash of an image file as hex, or None if the format is unsupported."""
    decoded = _decode_png_gray(path)
    if decoded is None:
        return None
    gray, w, h = decoded
    small = _resize_gray(gray, w, h, _DCT_SIZE)
    dct = _dct2(small)
    low = [dct[u][v] for u in range(hash_size) for v in range(hash_size)]
    med = statistics.median(low)
    bits = 0
    for val in low:
        bits = (bits << 1) | (1 if val > med else 0)
    n_hex = (hash_size * hash_size + 3) // 4
    return f"{bits:0{n_hex}x}"


def _decode_png_gray(path: Path) -> tuple[list[list[int]], int, int] | None:
    """Decode an 8-bit non-interlaced PNG to a grayscale matrix. None if unsupported."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data.startswith(_PNG_SIG):
        return None
    pos = len(_PNG_SIG)
    idat = bytearray()
    w = h = 0
    bitdepth = colortype = interlace = -1
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos : pos + 4], "big")
        ctype = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            w, h, bitdepth, colortype, _comp, _filt, interlace = struct.unpack(">IIBBBBB", chunk)
        elif ctype == b"IDAT":
            idat += chunk
        elif ctype == b"IEND":
            break
    if bitdepth != 8 or interlace != 0 or colortype not in _CHANNELS or w == 0 or h == 0:
        return None
    channels = _CHANNELS[colortype]
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error:
        return None
    stride = w * channels
    if len(raw) < h * (stride + 1):
        return None
    rows = _unfilter(raw, w, h, channels)
    gray: list[list[int]] = []
    for y in range(h):
        base = y * stride
        row: list[int] = []
        for x in range(w):
            o = base + x * channels
            if colortype in (0, 4):
                row.append(rows[o])
            else:  # 2 = RGB, 6 = RGBA (alpha ignored)
                row.append((rows[o] + rows[o + 1] + rows[o + 2]) // 3)
        gray.append(row)
    return gray, w, h


def _unfilter(raw: bytes, w: int, h: int, channels: int) -> bytes:
    """Reverse PNG scanline filters (types 0-4) for 8-bit data."""
    stride = w * channels
    out = bytearray(h * stride)
    prev = bytearray(stride)
    p = 0
    for y in range(h):
        ftype = raw[p]
        p += 1
        line = bytearray(raw[p : p + stride])
        p += stride
        if ftype == 1:  # Sub
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:  # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:  # Average
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:  # Paeth
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                pp = a + b - c
                pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        out[y * stride : (y + 1) * stride] = line
        prev = line
    return bytes(out)


def _resize_gray(gray: list[list[int]], w: int, h: int, size: int) -> list[list[float]]:
    """Box-average resize to size x size (works for up- and down-scaling)."""
    out: list[list[float]] = []
    for j in range(size):
        y0 = j * h // size
        y1 = max(y0 + 1, (j + 1) * h // size)
        row: list[float] = []
        for i in range(size):
            x0 = i * w // size
            x1 = max(x0 + 1, (i + 1) * w // size)
            total = 0
            count = 0
            for y in range(y0, min(y1, h)):
                grow = gray[y]
                for x in range(x0, min(x1, w)):
                    total += grow[x]
                    count += 1
            row.append(total / count)
        out.append(row)
    return out


def _dct2(mat: list[list[float]]) -> list[list[float]]:
    """2D DCT-II via separable matrix product: out = C @ mat @ C^T."""
    n = len(mat)
    cos = [[math.cos((2 * x + 1) * u * math.pi / (2 * n)) for x in range(n)] for u in range(n)]
    tmp = [[sum(cos[u][x] * mat[x][v] for x in range(n)) for v in range(n)] for u in range(n)]
    out = [[0.0] * n for _ in range(n)]
    for u in range(n):
        cu = 1.0 / math.sqrt(2.0) if u == 0 else 1.0
        for v in range(n):
            cv = 1.0 / math.sqrt(2.0) if v == 0 else 1.0
            out[u][v] = 0.25 * cu * cv * sum(tmp[u][x] * cos[v][x] for x in range(n))
    return out
