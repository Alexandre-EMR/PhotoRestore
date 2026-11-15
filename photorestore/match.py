"""Module M3 — Matching cascade (the anti-error core).

Rules run from MOST certain to LEAST certain:
  R1 exact     : media.ext <-> media.ext.supplemental-metadata.json (or .json)
  R2 truncated : sidecar names cut at 46 chars, unique candidate in folder
  R3 title     : JSON "title" holds the original media filename
  R4 heuristic : size +/-2% AND taken-time +/-48h AND same album
  R5 perceptual: pHash distance <= 3 -> quarantine PROPOSAL, never auto-write

POLICY (invariant): an ambiguous rule result is QUARANTINE, never a write.

v0 implements R1 fully; R2-R5 are registered as stubs and clearly skipped
(see ROADMAP in README.md and AGENTS.md module prompts).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .config import MatchConfig
from .manifest import Manifest

RULES_ORDER = ("R1_exact", "R2_truncated", "R3_title", "R4_heuristic", "R5_perceptual")

NOT_IMPLEMENTED = {"R2_truncated", "R3_title", "R4_heuristic", "R5_perceptual"}


@dataclass(frozen=True)
class MatchDecision:
    media: str
    rule: str
    sidecar: str | None
    decision: str  # "MATCH" | "QUARANTINE" | "UNMATCHED"
    note: str = ""


class RuleNotImplemented(LookupError):
    """Raised when a stub rule is invoked: signals ROADMAP work, not a bug."""


def _sidecar_candidates(media_path: Path) -> list[str]:
    """The two possible sidecar names for a media file, newest format first."""
    return [f"{media_path.name}.supplemental-metadata.json", f"{media_path.name}.json"]


def match_r1_exact(media_path: Path, sidecar_index: dict[str, str]) -> MatchDecision | None:
    """R1: exact sidecar filename in the same folder. None = rule does not apply."""
    for cand in _sidecar_candidates(media_path):
        if cand in sidecar_index:
            return MatchDecision(str(media_path), "R1_exact", sidecar_index[cand], "MATCH")
    return None


def match_r2_truncated(
    media_path: Path, sidecar_index: dict[str, str], trunc: int
) -> MatchDecision | None:
    """R2: sidecar truncated at `trunc` chars; match only if the candidate is unique.

    Ambiguity (two media starting with the same 46 chars) => QUARANTINE.
    """
    stem = media_path.name[:trunc]
    hits = [p for p in sidecar_index if Path(p).name.startswith(stem) and p != str(media_path)]
    raise RuleNotImplemented("R2 planned for P2 — see AGENTS.md prompt M3")


def build_sidecar_index(manifest: Manifest) -> dict[str, str]:
    """Map sidecar filename -> full path, for the folder-local lookups."""
    rows = manifest.db.execute("SELECT path FROM sidecars").fetchall()
    return {Path(r[0]).name: r[0] for r in rows}


def match_all(manifest: Manifest, cfg: MatchConfig) -> list[MatchDecision]:
    """Run the cascade over every hashed-but-unmatched media item.

    Deterministic, idempotent: running twice yields the same decisions,
    because matched items leave the 'hashed' state.
    """
    index = build_sidecar_index(manifest)
    decisions: list[MatchDecision] = []
    rows = manifest.pending_media(states=("hashed",))
    for m in rows:
        media_path = Path(m.path)
        folder = media_path.parent
        local_index = {n: p for n, p in index.items() if Path(p).parent == folder}

        matched = match_r1_exact(media_path, local_index)
        if matched is not None:
            decisions.append(matched)
            manifest.set_matched(m.path, matched.sidecar, "R1_exact")
            continue
        # R2-R5 are planned (P2+): leave item in 'hashed' state, report it.
        decisions.append(
            MatchDecision(
                str(media_path), "", None, "UNMATCHED",
                note="R1 no exact sidecar; R2-R5 not implemented in v0",
            )
        )
    manifest.commit()
    return decisions
