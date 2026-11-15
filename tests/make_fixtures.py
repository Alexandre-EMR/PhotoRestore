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
  9. perceptual pair: identical pixels, only one sidecar (R5 -> quarantine)
  10. lying extension: PNG bytes named .jpg (M4 rename, journaled)
  11. GIF (no EXIF support): M4 XMP sidecar fallback

Usage: python tests/make_fixtures.py <target_dir>
"""

from __future__ import annotations

import json
import struct
import sys
import zlib
from pathlib import Path


def write_png(path: Path, width: int = 16, height: int = 8, rgb: tuple[int, int, int] = (180, 40, 40)) -> None:
    """Minimal 8-bit RGB PNG writer using only zlib + struct.

    Pixels form a deterministic gradient derived from `rgb`, so two images
    with different rgb have different perceptual hashes (needed by R5/L1),
    while identical rgb produce byte-identical files (needed by L0/R5 pair).
    """

    def chunk(ctype: bytes, data: bytes) -> bytes:
        c = ctype + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    r, g, b = rgb
    rows = []
    for y in range(height):
        row = bytearray(b"\x00")
        for x in range(width):
            row += bytes(((r + x * 7 + y * 13) % 256, (g + x * 11 + y * 3) % 256, (b + x * 5 + y * 17) % 256))
        rows.append(bytes(row))
    raw = b"".join(rows)
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
    write_png(album / "IMG_0001.jpg", rgb=(180, 40, 40))
    (album / "IMG_0001.jpg.supplemental-metadata.json").write_text(
        json.dumps(sidecar("IMG_0001.jpg")), encoding="utf-8"
    )

    # 2. truncated sidecar (46-char cut)
    write_png(album / LONG_NAME, rgb=(40, 90, 180))
    (album / f"{TRUNC}.json").write_text(
        json.dumps(sidecar(LONG_NAME)), encoding="utf-8"
    )

    # 3. title-based (sidecar named after ANOTHER file, title holds real name)
    write_png(album / "IMG_0003.jpg", rgb=(60, 160, 60))
    (album / "unrelated-name.supplemental-metadata.json").write_text(
        json.dumps(sidecar("IMG_0003.jpg")), encoding="utf-8"
    )

    # 4. duplicate "(1)"
    write_png(album / "IMG_0004.jpg", rgb=(240, 200, 30))
    write_png(album / "IMG_0004(1).jpg", rgb=(240, 200, 30))
    (album / "IMG_0004.jpg.json").write_text(
        json.dumps(sidecar("IMG_0004.jpg")), encoding="utf-8"
    )
    (album / "IMG_0004(1).jpg.supplemental-metadata.json").write_text(
        json.dumps(sidecar("IMG_0004(1).jpg")), encoding="utf-8"
    )

    # 5. edited copy (old-style sidecar)
    write_png(album / "IMG_0005-edited.jpg", rgb=(30, 30, 30))
    (album / "IMG_0005-edited.jpg.json").write_text(
        json.dumps(sidecar("IMG_0005-edited.jpg", description="retouche")), encoding="utf-8",
    )

    # 6. video
    (album / "VID_20200913.mp4").write_bytes(b"\x00\x00\x00 ftypisom" + b"\x00" * 64)
    (album / "VID_20200913.mp4.supplemental-metadata.json").write_text(
        json.dumps(sidecar("VID_20200913.mp4", timestamp=1_600_010_000)), encoding="utf-8"
    )

    # 7. media without sidecar (must stay unmatched, zero write)
    write_png(album / "orphan.jpg", rgb=(120, 120, 20))

    # 7b. perceptual pair (R5): same pixels as IMG_0006.jpg (which HAS a
    # sidecar) but no sidecar of its own -> quarantine proposal, never a write
    write_png(album / "IMG_0006.jpg", rgb=(90, 30, 160))
    (album / "IMG_0006.jpg.supplemental-metadata.json").write_text(
        json.dumps(sidecar("IMG_0006.jpg", timestamp=1_600_020_000)), encoding="utf-8"
    )
    write_png(album / "IMG_0006-copy.jpg", rgb=(90, 30, 160))

    # 8. same name, different content (L2 case for dedup)
    other = album / "same-name.jpg"
    write_png(other, rgb=(10, 10, 10))
    sub = album / "sous-dossier"
    sub.mkdir(exist_ok=True)
    write_png(sub / "same-name.jpg", rgb=(200, 200, 200))
    (sub / "same-name.jpg.supplemental-metadata.json").write_text(
        json.dumps(sidecar("same-name.jpg")), encoding="utf-8"
    )

    # 10. lying extension: PNG bytes named .jpg (M4 renames it, journaled)
    write_png(album / "photo-fake.jpg", rgb=(200, 60, 120))
    (album / "photo-fake.jpg.supplemental-metadata.json").write_text(
        json.dumps(sidecar("photo-fake.jpg", timestamp=1_600_030_000)), encoding="utf-8"
    )

    # 11. format without EXIF support (GIF): M4 falls back to an .xmp sidecar
    (album / "anim.gif").write_bytes(b"GIF89a" + b"\x00" * 32)
    (album / "anim.gif.supplemental-metadata.json").write_text(
        json.dumps(sidecar("anim.gif", timestamp=1_600_040_000, description="anim")),
        encoding="utf-8",
    )

    # album metadata
    (target / "Takeout" / "Google Photos" / "Album de test" / "metadata.json").write_text(
        json.dumps({"title": "Album de test", "description": ""}), encoding="utf-8"
    )

    n_media = sum(1 for p in album.rglob("*") if p.suffix.lower() in {".jpg", ".mp4"})
    n_json = sum(1 for p in album.rglob("*.json") if p.name != "metadata.json")
    return {"media": n_media, "sidecars": n_json}


if __name__ == "__main__":
    dest = Path(sys.argv[1] if len(sys.argv) > 1 else "fixtures")
    stats = generate(dest)
    print(f"corpus généré dans {dest.resolve()}: {stats['media']} médias, {stats['sidecars']} sidecars")
