"""Module M1 — Ingestion.

Discovers media files and Takeout sidecar JSON files from a source folder
(already extracted) and registers them in the SQLite manifest.

v0 scope: extracted folders and .zip archives are handled; .tar.gz and the
"logical merge without extraction" of multiple archives are planned for v0.5.
Nothing here ever modifies source files.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path

# Google Takeout media extensions we care about (photos + videos).
MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".tiff",
    ".mp4", ".mov", ".m4v", ".avi", ".webm",
}

# Sidecar patterns, most recent first.
SIDECAR_SUFFIXES = (".supplemental-metadata.json", ".json")
ALBUM_METADATA_NAME = "metadata.json"


@dataclass(frozen=True)
class DiscoveredItem:
    path: Path
    kind: str  # "media" | "sidecar" | "album"
    album: str = ""


def extract_archives(source: Path, workdir: Path) -> Path:
    """Extract every .zip archive found under source into workdir.

    All archives are extracted into the SAME folder (Google splits a photo
    and its sidecar across different zips). Returns the extraction target.
    Never writes into source.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    zips = sorted(source.rglob("*.zip")) if source.is_dir() else [source]
    if not zips:
        return workdir
    for zp in zips:
        with zipfile.ZipFile(zp) as zf:
            zf.extractall(workdir)
    return workdir


def discover(root: Path) -> list[DiscoveredItem]:
    """Walk root and classify every media / sidecar / album metadata file."""
    items: list[DiscoveredItem] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        name = p.name
        if name == ALBUM_METADATA_NAME:
            items.append(DiscoveredItem(p, "album", p.parent.name))
        elif name.endswith(SIDECAR_SUFFIXES) and name.endswith(".json"):
            items.append(DiscoveredItem(p, "sidecar", p.parent.name))
        elif p.suffix.lower() in MEDIA_EXTENSIONS:
            items.append(DiscoveredItem(p, "media", p.parent.name))
        # everything else (prints, archives metadata, ...) is ignored
    return items
