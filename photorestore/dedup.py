"""Module M5 — Pixel-content deduplication (planned for P3).

Four families of duplicates, detected by CONTENT, never by name alone:

  L0 exact bytes  : identical sha256, different names -> "doublon exact".
  L1 near-duplicates: pHash (DCT) Hamming distance <= 5 -> near-certain same
     image; 6..10 -> "variant" (crop/filter) flagged for human review.
  L2 same name, different content: both kept; the conservation-policy loser
     is renamed with a .CONFLICT-<sha8> suffix and quarantined.
  L3 videos: sha256, then ffmpeg frame sampling (1/10s, max 30) + pHash per
     frame; duplicate if >= 3 frames at distance <= 8.

INVARIANTS: the loser is ALWAYS MOVED to <root>/_duplicates/, NEVER deleted;
every move is recorded in undo_manifest.csv (origin -> destination) and
reversible with `photorestore undo`.

v0 is a stub with the data model and undo manifest only.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from .config import DedupConfig

UNDO_HEADER = ("origin", "destination", "family", "reason")


@dataclass(frozen=True)
class DuplicateGroup:
    family: str  # "L0" | "L1" | "L2" | "L3"
    keep: str
    losers: tuple[str, ...]
    reason: str = ""


class DedupNotImplemented(NotImplementedError):
    """P3 scope. The detection engine lands with Pillow/imagededup."""


def read_undo_manifest(path: Path) -> list[tuple[str, str, str, str]]:
    """Parse undo_manifest.csv; returns [] when absent (fresh run)."""
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [(r["origin"], r["destination"], r["family"], r["reason"]) for r in csv.DictReader(f)]


def dedup(root: Path, cfg: DedupConfig, apply: bool) -> list[DuplicateGroup]:  # noqa: ARG001
    """Planned: detect the 4 families and quarantine losers. Stub in v0."""
    raise DedupNotImplemented("M5 (dedup) is planned for P3 — see AGENTS.md prompt M5.")


def undo(undo_manifest: Path) -> int:
    """Revert every recorded move (destination -> origin). Never deletes."""
    moves = read_undo_manifest(undo_manifest)
    reverted = 0
    for origin, destination, _family, _reason in moves:
        src, dst = Path(destination), Path(origin)
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            reverted += 1
    return reverted
