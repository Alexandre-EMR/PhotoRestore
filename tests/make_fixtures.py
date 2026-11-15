"""Synthetic Takeout fixture corpus generator (stdlib only — no Pillow).

Generates a tiny but complete Google-Takeout-like corpus covering every
edge case of the cahier des charges:

  1. exact sidecar pair (R1)
  2. sidecar truncated at 46 chars (R2)
  3. title-based match (R3)
  4. duplicate "(1)" naming
  5. "-edited" copy
  6. video without EXIF-able date (.mp4)
  7. media WITHOUT any sidecar (must stay unmatched)
  8. same name, different content (future L2 dedup case)
  9. media with BOTH sidecar formats (ambiguous -> quarantine, never a write)
 10. R2 ambiguity: two media sharing the same 46-char truncated prefix
 11. R3 ambiguity: two sidecars with the same "title"
 12. R4 heuristic: orphan copy near an already-matched original (size/time/album)
 13. R5 perceptual: orphan visually identical to a matched media (proposal only)
 14. iOS Live Photo: .HEIC + .MOV pair sharing one sidecar

All images are deterministic textured patterns (solid colors have identical
pHash values and would make every perceptual test a false positive).

Usage: python tests/make_fixtures.py <target_dir>
"""

from __future__ import annotations

import json
import os
import struct
import sys
import time
import zlib
from pathlib import Path


def write_png(path: Path, width: int = 16, height: int = 8, rgb: tuple[int, int, int] = (180, 40, 40)) -> None:
    """Minimal 8-bit RGB PNG writer using only zlib + struct."""

    def chunk(ctype: bytes, data: bytes) -> bytes:
        c = ctype + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def write_png_pattern(path: Path, width: int = 32, height: int = 32, seed: int = 0) -> None:
    """Deterministic textured RGB PNG: distinct seeds give distant pHashes."""

    def chunk(ctype: bytes, data: bytes) -> bytes:
        c = ctype + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    raw = b""
    for y in range(height):
        row = bytearray(b"\x00")
        for x in range(width):
            row += bytes((
                (x * 7 + y * 13 + seed * 29) % 256,
                (x * 3 + y * 11 + seed * 17) % 256,
                (x * 5 + y * 2 + seed * 41) % 256,
            ))
        raw += bytes(row)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def sidecar(
    title: str,
    timestamp: int = 1_600_000_000,
    lat: float = 43.2965,
    lon: float = 5.3698,
    description: str = "",
) -> dict:
    return {
        "title": title,
        "description": description,
        "geoData": {
            "latitude": lat,
            "longitude": lon,
            "altitude": 0.0,
            "latitudeSpan": 0.0,
            "longitudeSpan": 0.0,
        },
        "photoTakenTime": {"timestamp": str(timestamp), "formatted": "Sun, 13 Sep 2020 05:46:40 UTC"},
    }


LONG_NAME = "a-very-long-google-photos-filename-over-forty-six-characters.jpg"
TRUNC = (LONG_NAME + ".supplemental-metadata")[:46]


def generate(target: Path) -> dict[str, int]:
    album = target / "Takeout" / "Google Photos" / "Album de test"
    album.mkdir(parents=True, exist_ok=True)

    # 1. exact pair
    write_png_pattern(album / "IMG_0001.jpg", seed=1)
    (album / "IMG_0001.jpg.supplemental-metadata.json").write_text(
        json.dumps(sidecar("IMG_0001.jpg")), encoding="utf-8"
    )

    # 2. truncated sidecar (46-char cut)
    write_png_pattern(album / LONG_NAME, seed=2)
    (album / f"{TRUNC}.json").write_text(
        json.dumps(sidecar(LONG_NAME)), encoding="utf-8"
    )

    # 3. title-based (sidecar named after ANOTHER file, title holds real name)
    write_png_pattern(album / "IMG_0003.jpg", seed=3)
    (album / "unrelated-name.supplemental-metadata.json").write_text(
        json.dumps(sidecar("IMG_0003.jpg")), encoding="utf-8"
    )

    # 4. duplicate "(1)"
    write_png_pattern(album / "IMG_0004.jpg", seed=4)
    write_png_pattern(album / "IMG_0004(1).jpg", seed=4)
    (album / "IMG_0004.jpg.json").write_text(
        json.dumps(sidecar("IMG_0004.jpg")), encoding="utf-8"
    )
    (album / "IMG_0004(1).jpg.supplemental-metadata.json").write_text(
        json.dumps(sidecar("IMG_0004(1).jpg")), encoding="utf-8"
    )

    # 5. edited copy (old-style sidecar)
    write_png_pattern(album / "IMG_0005-edited.jpg", seed=5)
    (album / "IMG_0005-edited.jpg.json").write_text(
        json.dumps(sidecar("IMG_0005-edited.jpg", description="retouche")), encoding="utf-8",
    )

    # 6. video
    (album / "VID_20200913.mp4").write_bytes(b"\x00\x00\x00 ftypisom" + b"\x00" * 64)
    (album / "VID_20200913.mp4.supplemental-metadata.json").write_text(
        json.dumps(sidecar("VID_20200913.mp4", timestamp=1_600_010_000)), encoding="utf-8"
    )

    # 7. media without sidecar (must stay unmatched, zero write)
    write_png_pattern(album / "orphan.jpg", seed=6)

    # 8. same name, different content (L2 case for dedup)
    other = album / "same-name.jpg"
    write_png_pattern(other, seed=7)
    sub = album / "sous-dossier"
    sub.mkdir(exist_ok=True)
    write_png_pattern(sub / "same-name.jpg", seed=8)
    (sub / "same-name.jpg.supplemental-metadata.json").write_text(
        json.dumps(sidecar("same-name.jpg")), encoding="utf-8"
    )

    # 9. both sidecar formats for one media: ambiguous -> QUARANTINE
    write_png_pattern(album / "IMG_0009.jpg", seed=9)
    (album / "IMG_0009.jpg.supplemental-metadata.json").write_text(
        json.dumps(sidecar("IMG_0009.jpg")), encoding="utf-8"
    )
    (album / "IMG_0009.jpg.json").write_text(
        json.dumps(sidecar("IMG_0009.jpg")), encoding="utf-8"
    )

    # 10. R2 ambiguity: two media sharing the same 46-char truncated prefix
    album_r2 = target / "Takeout" / "Google Photos" / "Album R2 ambigu"
    album_r2.mkdir(parents=True, exist_ok=True)
    long_a = "b-very-long-google-photos-filename-over-forty-six-characters.jpg"
    long_b = "b-very-long-google-photos-filename-over-forty-six-characters-B.jpg"
    write_png_pattern(album_r2 / long_a, seed=10)
    write_png_pattern(album_r2 / long_b, seed=11)
    (album_r2 / f"{long_a[:46]}.json").write_text(
        json.dumps(sidecar(long_a)), encoding="utf-8"
    )

    # 11. R3 ambiguity: two sidecars whose "title" names the same media
    album_r3 = target / "Takeout" / "Google Photos" / "Album R3 ambigu"
    album_r3.mkdir(parents=True, exist_ok=True)
    write_png_pattern(album_r3 / "dup.jpg", seed=12)
    (album_r3 / "d1.json").write_text(
        json.dumps(sidecar("dup.jpg")), encoding="utf-8"
    )
    (album_r3 / "d2.json").write_text(
        json.dumps(sidecar("dup.jpg")), encoding="utf-8"
    )

    # 12. R4 heuristic: orphan copies near already-matched originals.
    # Sizes are identical (same seed); timestamps drive candidate filtering.
    album_r4 = target / "Takeout" / "Google Photos" / "Album R4 heuristique"
    album_r4.mkdir(parents=True, exist_ok=True)
    t0 = int(time.time())
    write_png_pattern(album_r4 / "IMG_0000A.jpg", seed=13)
    (album_r4 / "IMG_0000A.jpg.supplemental-metadata.json").write_text(
        json.dumps(sidecar("IMG_0000A.jpg", timestamp=t0)), encoding="utf-8"
    )
    write_png_pattern(album_r4 / "IMG_0000B.jpg", seed=13)
    (album_r4 / "IMG_0000B.jpg.supplemental-metadata.json").write_text(
        json.dumps(sidecar("IMG_0000B.jpg", timestamp=t0 + 3 * 86400)), encoding="utf-8"
    )
    # unique candidate (only IMG_0000A is within +/-48h) -> R4 MATCH
    write_png_pattern(album_r4 / "IMG_0007.jpg", seed=13)
    os.utime(album_r4 / "IMG_0007.jpg", (t0, t0))
    # two candidates (within +/-48h of BOTH originals) -> R4 QUARANTINE
    write_png_pattern(album_r4 / "IMG_0008.jpg", seed=13)
    os.utime(album_r4 / "IMG_0008.jpg", (t0 + 36 * 3600, t0 + 36 * 3600))

    # 13. R5 perceptual: orphan visually identical to a matched media
    album_r5 = target / "Takeout" / "Google Photos" / "Album R5 perceptuel"
    album_r5.mkdir(parents=True, exist_ok=True)
    write_png_pattern(album_r5 / "ref.jpg", seed=14)
    (album_r5 / "ref.jpg.supplemental-metadata.json").write_text(
        json.dumps(sidecar("ref.jpg")), encoding="utf-8"
    )
    write_png_pattern(album_r5 / "dup-orphan.jpg", seed=14)

    # 14. iOS Live Photo: .HEIC + .MOV pair sharing one sidecar
    album_lp = target / "Takeout" / "Google Photos" / "Album Live Photo"
    album_lp.mkdir(parents=True, exist_ok=True)
    (album_lp / "IMG_0010.HEIC").write_bytes(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64)
    (album_lp / "IMG_0010.MOV").write_bytes(b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 64)
    (album_lp / "IMG_0010.HEIC.supplemental-metadata.json").write_text(
        json.dumps(sidecar("IMG_0010.HEIC")), encoding="utf-8"
    )

    # album metadata
    (target / "Takeout" / "Google Photos" / "Album de test" / "metadata.json").write_text(
        json.dumps({"title": "Album de test", "description": ""}), encoding="utf-8"
    )

    root = target / "Takeout" / "Google Photos"
    n_media = sum(
        1
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".heic", ".mp4", ".mov"}
    )
    n_json = sum(
        1 for p in root.rglob("*.json") if p.name != "metadata.json"
    )
    return {"media": n_media, "sidecars": n_json}


if __name__ == "__main__":
    dest = Path(sys.argv[1] if len(sys.argv) > 1 else "fixtures")
    stats = generate(dest)
    print(f"corpus généré dans {dest.resolve()}: {stats['media']} médias, {stats['sidecars']} sidecars")
