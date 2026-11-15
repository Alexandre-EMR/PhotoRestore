"""Module M3 — Matching cascade (the anti-error core).

Rules run from MOST certain to LEAST certain:
  R1 exact     : media.ext <-> media.ext.supplemental-metadata.json (or .json)
  R2 truncated : sidecar names cut at 46 chars, unique candidate in folder
  R3 title     : JSON "title" holds the original media filename (same folder)
  R4 heuristic : size +/-2% AND taken-time +/-48h AND same album
  R5 perceptual: pHash distance <= 3 -> quarantine PROPOSAL, never auto-write

POLICY (invariant): an ambiguous rule result is QUARANTINE, never a write.
A sidecar is claimed by at most one media (tracked in the manifest across
runs), so two media can never write the same JSON metadata.

Known v0 limitations, flagged to the owner: R4 uses the file mtime as the
media-side date proxy until M4/exiftool provides DateTimeOriginal, and its
size criterion compares against the media named by the sidecar stem when
that file exists (otherwise the criterion is unverifiable -> not eligible);
R3 is folder-local (a same-name sidecar in another folder is an L2
ambiguity, not a title match); R5 computes pHash for PNG only (stdlib
decoder, see phash.py).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .config import MatchConfig
from .manifest import Manifest, MediaRow
from .phash import hamming_hex, phash_file

RULES_ORDER = ("R1_exact", "R2_truncated", "R3_title", "R4_heuristic", "R5_perceptual")

_HOUR_S = 3600
_SIDECAR_SUFFIXES = (".supplemental-metadata.json", ".json")


@dataclass(frozen=True)
class MatchDecision:
    media: str
    rule: str
    sidecar: str | None
    decision: str  # "MATCH" | "QUARANTINE" | "UNMATCHED"
    note: str = ""


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
    media_path: Path,
    sidecar_index: dict[str, str],
    claimed: set[str],
    folder_media: list[str],
    trunc: int,
) -> MatchDecision | None:
    """R2: sidecar truncated at `trunc` chars; match only if the candidate is unique.

    Ambiguity (another media in the folder starting with the same 46 chars)
    => QUARANTINE, never a write.
    """
    suffixed = media_path.name + ".supplemental-metadata"
    if len(suffixed) <= trunc:
        return None  # not truncated: R1 already covers this name
    stem = suffixed[:trunc]
    candidates = [
        c for c in (f"{stem}.json", f"{stem}.supplemental-metadata.json")
        if c in sidecar_index and sidecar_index[c] not in claimed
    ]
    if not candidates:
        return None
    peers = [p for p in folder_media if p != str(media_path) and Path(p).name.startswith(stem)]
    if peers:
        return MatchDecision(
            str(media_path), "R2_truncated", sidecar_index[candidates[0]], "QUARANTINE",
            note=f"truncated sidecar '{candidates[0]}' is ambiguous: {len(peers)} media "
                 f"in the folder start with the same {trunc} chars",
        )
    return MatchDecision(str(media_path), "R2_truncated", sidecar_index[candidates[0]], "MATCH")


def match_r3_title(
    media_path: Path,
    folder_sidecars: dict[str, tuple[str | None, int | None]],
    claimed: set[str],
) -> MatchDecision | None:
    """R3: an unclaimed sidecar of the same folder has a "title" containing
    the media filename. More than one candidate => QUARANTINE."""
    hits = [
        path for path, (title, _ts) in folder_sidecars.items()
        if path not in claimed and title is not None and media_path.name in title
    ]
    if not hits:
        return None
    if len(hits) > 1:
        return MatchDecision(
            str(media_path), "R3_title", hits[0], "QUARANTINE",
            note=f"{len(hits)} sidecars have a title containing '{media_path.name}'",
        )
    return MatchDecision(str(media_path), "R3_title", hits[0], "MATCH")


def match_r4_heuristic(
    manifest: Manifest,
    media_path: Path,
    media_row: MediaRow,
    sidecar_meta: dict[str, tuple[str, str | None, int | None]],
    claimed: set[str],
    cfg: MatchConfig,
) -> MatchDecision | None:
    """R4: size +/-2% AND taken-time +/-48h AND same album — all must hold.

    Media-side date is the file mtime (proxy until M4 reads DateTimeOriginal).
    The size criterion compares the pending media with the media named by the
    sidecar stem (manifest-wide by filename, else on disk in the same folder);
    an unverifiable criterion makes the sidecar ineligible (ambiguity
    aversion). >1 candidate => QUARANTINE.
    """
    mtime = int(os.stat(media_path).st_mtime)
    window = cfg.heuristic_time_window_hours * _HOUR_S
    tol = cfg.heuristic_size_tolerance
    hits: list[str] = []
    for path, (sc_album, _title, ts) in sidecar_meta.items():
        if path in claimed or sc_album != media_row.album or ts is None:
            continue
        if abs(ts - mtime) > window:
            continue
        stem = Path(path).name
        for suffix in _SIDECAR_SUFFIXES:
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        if stem == media_path.name:
            continue  # that case is R1 territory, not a heuristic
        peer_size = _peer_size(manifest, media_path.parent, stem)
        if peer_size is not None and abs(peer_size - media_row.size) <= tol * media_row.size:
            hits.append(path)
    if not hits:
        return None
    if len(hits) > 1:
        return MatchDecision(
            str(media_path), "R4_heuristic", hits[0], "QUARANTINE",
            note=f"{len(hits)} sidecars within size +/-{tol:.0%} and "
                 f"+/-{cfg.heuristic_time_window_hours}h in album '{media_row.album}'",
        )
    return MatchDecision(str(media_path), "R4_heuristic", hits[0], "MATCH")


def _peer_size(manifest: Manifest, folder: Path, stem: str) -> int | None:
    """Size of the media a sidecar stem refers to: manifest first, disk fallback."""
    size = manifest.media_size_by_name(stem)
    if size is not None:
        return size
    peer = folder / stem
    if peer.is_file():
        return peer.stat().st_size
    return None


def match_r5_perceptual(
    manifest: Manifest,
    media_path: Path,
    album: str,
    cfg: MatchConfig,
) -> MatchDecision | None:
    """R5: pHash distance <= max to an already-matched media of the same album.

    PROPOSAL for human review only: the decision is always QUARANTINE,
    never MATCH (perceptual similarity is not proof).
    """
    phash = _ensure_phash(manifest, str(media_path))
    if phash is None:
        return None
    near = []
    for other_path, other_album, other_phash in manifest.matched_media_with_phash():
        if other_path == str(media_path) or other_album != album or not other_phash:
            continue
        if hamming_hex(phash, other_phash) <= cfg.perceptual_hamming_max:
            near.append(other_path)
    if not near:
        return None
    return MatchDecision(
        str(media_path), "R5_perceptual", None, "QUARANTINE",
        note="perceptual proposal for human review (never auto-written); near: "
             + ", ".join(sorted(Path(p).name for p in near)),
    )


def _ensure_phash(manifest: Manifest, path: str) -> str | None:
    """pHash from the manifest cache, computing + caching it on first use."""
    cached = manifest.get_phash(path)
    if cached:
        return cached
    computed = phash_file(Path(path))
    if computed is not None:
        manifest.set_phash(path, computed)
    return computed


def build_sidecar_index(manifest: Manifest) -> dict[str, str]:
    """Map sidecar filename -> full path, for the folder-local lookups."""
    rows = manifest.db.execute("SELECT path FROM sidecars").fetchall()
    return {Path(r[0]).name: r[0] for r in rows}


def match_all(manifest: Manifest, cfg: MatchConfig) -> list[MatchDecision]:
    """Run the cascade R1->R5 over every hashed-but-unmatched media item.

    Deterministic and idempotent: matched items leave the 'hashed' state, so
    a second run re-evaluates only the still-unmatched items and produces
    exactly the same decisions (reports are rebuilt from the manifest).
    """
    index = build_sidecar_index(manifest)
    sidecar_meta = {
        path: (album, title, ts) for path, album, title, ts in manifest.sidecar_rows()
    }
    claimed = manifest.claimed_sidecars()
    rows = sorted(manifest.pending_media(states=("hashed",)), key=lambda r: r.path)

    # Pass 1: nominal rules R1-R4 (they do not depend on other media).
    still_open: list[MediaRow] = []
    by_path: dict[str, MatchDecision] = {}
    for m in rows:
        media_path = Path(m.path)
        folder = media_path.parent
        local_index = {n: p for n, p in index.items() if Path(p).parent == folder}
        local_sidecars = {
            p: (sidecar_meta[p][1], sidecar_meta[p][2])
            for p in local_index.values() if p in sidecar_meta
        }
        folder_media = manifest.media_paths_in_folder(folder)

        decision = match_r1_exact(media_path, local_index)
        if decision is not None and decision.sidecar in claimed:
            decision = None
        if decision is None:
            decision = match_r2_truncated(
                media_path, local_index, claimed, folder_media, cfg.trunc_length
            )
        if decision is None:
            decision = match_r3_title(media_path, local_sidecars, claimed)
        if decision is None:
            decision = match_r4_heuristic(manifest, media_path, m, sidecar_meta, claimed, cfg)

        if decision is not None and decision.decision == "MATCH" and decision.sidecar:
            manifest.set_matched(m.path, decision.sidecar, decision.rule, decision.note)
            claimed.add(decision.sidecar)
            by_path[m.path] = decision
        else:
            if decision is not None:
                by_path[m.path] = decision  # quarantine from R2-R4: final
            still_open.append(m)

    # Pass 2: R5 perceptual needs the matched pool, so it runs after pass 1.
    # The pHash cache is warmed for every matched media (resumable: stored
    # in the manifest, so a re-run never recomputes them).
    for path, _album in manifest.matched_media():
        _ensure_phash(manifest, path)
    for m in still_open:
        if m.path in by_path:  # already quarantined by a nominal rule
            manifest.set_decision(
                m.path, by_path[m.path].decision, by_path[m.path].rule, by_path[m.path].note
            )
            continue
        media_path = Path(m.path)
        decision = match_r5_perceptual(manifest, media_path, m.album, cfg)
        if decision is None:
            decision = MatchDecision(
                str(media_path), "", None, "UNMATCHED",
                note="no sure rule matched (R1-R5 evaluated)",
            )
        manifest.set_decision(m.path, decision.decision, decision.rule, decision.note)
        by_path[m.path] = decision

    manifest.commit()
    return [by_path[m.path] for m in rows]
