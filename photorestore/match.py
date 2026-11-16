"""Module M3 — Matching cascade (the anti-error core).

Rules run from MOST certain to LEAST certain:
  R1 exact     : media.ext <-> media.ext.supplemental-metadata.json (or .json)
  R2 truncated : sidecar names cut at 46 chars, unique candidate in folder
  R3 title     : JSON "title" holds the original media filename
  R4 heuristic : copy of an already-matched media — size +/-2% AND
                 media file time within +/-48h of the sidecar taken-time
                 AND same album (all three MUST hold simultaneously)
  R5 perceptual: pHash distance <= 3 between a media without JSON and a
                 media carrying JSON -> quarantine PROPOSAL for human
                 review, never an automatic write.

POLICY (invariant): an ambiguous rule result is QUARANTINE, never a write.

R4 note on the "size +/-2%" criterion: Takeout JSON files carry no file
size, so the size is compared between the orphan media and the reference
media already matched to the JSON (duplicates "(1)", "-edited" and
recompressed copies keep a near-identical size). Documented deviation,
flagged for confirmation in the P2a delivery.

R5 note on dependencies: Pillow + ImageHash are the first documented
exceptions to the zero-dependency policy (optional extra "perceptual").
Without them, R5 is cleanly skipped and reported, never faked.

Every MATCH / QUARANTINE decision is journaled in the manifest "decisions"
table (match_id, verdict, note, timestamp) for the future review UI.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import MatchConfig
from .manifest import Manifest

RULES_ORDER = ("R1_exact", "R2_truncated", "R3_title", "R4_heuristic", "R5_perceptual")

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".webm"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".tiff"}
LIVE_PHOTO_MEDIA = {".heic", ".heif"}


@dataclass(frozen=True)
class MatchDecision:
    media: str
    rule: str
    sidecar: str | None
    decision: str  # "MATCH" | "QUARANTINE" | "UNMATCHED"
    note: str = ""
    confidence: str = "high"  # high (R1-R3) | medium (R4) | proposal (R5)


def _sidecar_candidates(media_path: Path) -> list[str]:
    """The two possible sidecar names for a media file, newest format first."""
    return [f"{media_path.name}.supplemental-metadata.json", f"{media_path.name}.json"]


def match_r1_exact(media_path: Path, sidecar_index: dict[str, str]) -> MatchDecision | None:
    """R1: exact sidecar filename in the same folder. None = rule does not apply.

    POLICY: if BOTH the new (.supplemental-metadata.json) and the old (.json)
    sidecars exist for the same media, the rule is AMBIGUOUS => QUARANTINE,
    never a silent pick of one of them.
    """
    found = [cand for cand in _sidecar_candidates(media_path) if cand in sidecar_index]
    if len(found) > 1:
        return MatchDecision(
            str(media_path), "R1_exact", None, "QUARANTINE",
            note="ambiguous: both .supplemental-metadata.json and .json sidecars exist",
        )
    if found:
        return MatchDecision(str(media_path), "R1_exact", sidecar_index[found[0]], "MATCH")
    return None


def match_r2_truncated(
    media_path: Path,
    sidecar_index: dict[str, str],
    folder_media_names: set[str],
    trunc: int = 46,
) -> MatchDecision | None:
    """R2: sidecar stem truncated at `trunc` chars, unique candidate in folder.

    A sidecar applies when its stem (name without ".json") is a strict
    prefix of the media name, at most `trunc` chars long (Google cuts the
    original filename at 46 chars before appending ".json").

    POLICY: if several media of the folder share the truncated prefix, or
    several truncated sidecars apply, the rule is AMBIGUOUS => QUARANTINE.
    """
    name = media_path.name
    candidates = [
        sc
        for sc in sidecar_index
        if sc.endswith(".json")
        and len(sc) - len(".json") <= trunc
        and len(sc) - len(".json") < len(name)
        and name.startswith(sc[: -len(".json")])
    ]
    if not candidates:
        return None
    if len(candidates) > 1:
        return MatchDecision(
            str(media_path), "R2_truncated", None, "QUARANTINE",
            note=f"ambiguous: {len(candidates)} truncated sidecars apply",
        )
    stem = candidates[0][: -len(".json")]
    starters = [n for n in folder_media_names if n.startswith(stem)]
    if len(starters) == 1:
        return MatchDecision(
            str(media_path), "R2_truncated", sidecar_index[candidates[0]], "MATCH",
            note=f"truncated sidecar stem ({len(stem)} chars) unique in folder",
        )
    return MatchDecision(
        str(media_path), "R2_truncated", None, "QUARANTINE",
        note=f"ambiguous: {len(starters)} media share the truncated prefix",
    )


def match_r3_title(
    media_path: Path, sidecar_meta: list[tuple[str, str | None, str]]
) -> MatchDecision | None:
    """R3: a sidecar of the same folder whose JSON "title" is the media name.

    Folder-scoped on purpose: two albums may hold sidecars with the same
    title, and a cross-folder lookup would silently pick the wrong one.

    POLICY: several sidecars with the same title => AMBIGUOUS => QUARANTINE.
    """
    name = media_path.name
    candidates = [sc for _name, title, sc in sidecar_meta if title == name]
    if not candidates:
        return None
    if len(candidates) == 1:
        return MatchDecision(
            str(media_path), "R3_title", candidates[0], "MATCH",
            note=f"JSON title is the media name: {name}",
        )
    return MatchDecision(
        str(media_path), "R3_title", None, "QUARANTINE",
        note=f"ambiguous: {len(candidates)} sidecars carry the title {name!r}",
    )


@dataclass(frozen=True)
class HeuristicRef:
    """An already-matched media usable as an R4 reference for its sidecar."""

    path: str
    size: int
    sidecar: str
    taken_timestamp: int | None


def match_r4_heuristic(
    media_path: Path,
    refs: list[HeuristicRef],
    cfg: MatchConfig,
    media_size: int,
    media_mtime: float,
) -> MatchDecision | None:
    """R4: orphan copy of a media already carrying a JSON sidecar.

    All three criteria must hold simultaneously:
      * size: |media_size - ref.size| <= +/-2% of the reference size
      * time : |media_mtime - sidecar taken_timestamp| <= +/-48h
      * album: refs are pre-filtered to the media album by the caller

    POLICY: several qualifying references => AMBIGUOUS => QUARANTINE.
    """
    tol = cfg.heuristic_size_tolerance
    window = cfg.heuristic_time_window_hours * 3600
    candidates = [
        ref
        for ref in refs
        if ref.taken_timestamp is not None
        and abs(media_size - ref.size) <= tol * max(ref.size, 1)
        and abs(media_mtime - ref.taken_timestamp) <= window
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        ref = candidates[0]
        return MatchDecision(
            str(media_path), "R4_heuristic", ref.sidecar, "MATCH",
            note=(
                f"heuristic copy of {Path(ref.path).name} "
                f"(size +/-{tol:.0%}, {cfg.heuristic_time_window_hours}h, same album)"
            ),
            confidence="medium",
        )
    names = ", ".join(sorted(Path(r.path).name for r in candidates))
    return MatchDecision(
        str(media_path), "R4_heuristic", None, "QUARANTINE",
        note=f"ambiguous: {len(candidates)} possible references ({names})",
        confidence="medium",
    )


def _phash_hex(path: Path) -> str | None:
    """pHash of an image, or None when Pillow/ImageHash are unavailable.

    First documented exception to the zero-dependency policy
    (optional extra "perceptual"); a clean skip, never a fake result.
    """
    try:
        import imagehash
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(path) as img:
            return str(imagehash.phash(img))
    except OSError:
        return None


def _hamming(a: str, b: str) -> int | None:
    try:
        import imagehash
    except ImportError:
        return None
    return imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b)


def match_r5_perceptual(
    media_path: Path,
    matched_hashes: list[tuple[str, str]],
    cfg: MatchConfig,
    phash_cache: dict[str, str],
) -> MatchDecision | None:
    """R5: perceptual proposal — QUARANTINE for human review, never a MATCH.

    Compares the orphan image to media already carrying a JSON sidecar.
    Videos are out of scope (frame sampling is the P3 dedup module).
    """
    if media_path.suffix.lower() not in IMAGE_SUFFIXES:
        return None
    key = str(media_path)
    if key not in phash_cache:
        h = _phash_hex(media_path)
        if h is None:
            return None
        phash_cache[key] = h
    h1 = phash_cache[key]
    close: list[tuple[int, str]] = []
    for ref_path, ref_hash in matched_hashes:
        d = _hamming(h1, ref_hash)
        if d is not None and d <= cfg.perceptual_hamming_max:
            close.append((d, ref_path))
    if not close:
        return None
    close.sort(key=lambda t: (t[0], t[1]))
    names = ", ".join(Path(p).name for _d, p in close)
    return MatchDecision(
        str(media_path), "R5_perceptual", None, "QUARANTINE",
        note=(
            f"proposal for human review: perceptual match (distance "
            f"<={cfg.perceptual_hamming_max}) with {names}"
        ),
        confidence="proposal",
    )


def build_sidecar_index(manifest: Manifest) -> dict[Path, dict[str, str]]:
    """Folder-keyed sidecar index: {folder: {sidecar_name: full_path}}.

    Keying by folder is mandatory: two albums may contain sidecars with the
    same filename, and a flat name-keyed map would silently drop one.
    """
    rows = manifest.db.execute("SELECT path FROM sidecars").fetchall()
    index: dict[Path, dict[str, str]] = {}
    for (p,) in rows:
        full = Path(p)
        index.setdefault(full.parent, {})[full.name] = p
    return index


def _sidecar_meta_by_folder(manifest: Manifest) -> dict[Path, list[tuple[str, str | None, str]]]:
    """{folder: [(sidecar_name, title, full_path)]} for R3 (folder-scoped)."""
    rows = manifest.db.execute("SELECT path, title FROM sidecars").fetchall()
    meta: dict[Path, list[tuple[str, str | None, str]]] = {}
    for p, title in rows:
        full = Path(p)
        meta.setdefault(full.parent, []).append((full.name, title, p))
    return meta


def _media_names_by_folder(manifest: Manifest) -> dict[Path, set[str]]:
    rows = manifest.db.execute("SELECT path FROM media").fetchall()
    folders: dict[Path, set[str]] = {}
    for (p,) in rows:
        full = Path(p)
        folders.setdefault(full.parent, set()).add(full.name)
    return folders


def _load_refs(manifest: Manifest) -> dict[str, list[HeuristicRef]]:
    """R4 references: media already carrying a sidecar, grouped by album."""
    rows = manifest.db.execute(
        """SELECT m.path, m.album, m.size, s.path, s.taken_timestamp
           FROM media m JOIN sidecars s ON m.matched_sidecar = s.id
           WHERE m.state IN ('matched','applied','verified')"""
    ).fetchall()
    refs: dict[str, list[HeuristicRef]] = {}
    for m_path, album, size, s_path, ts in rows:
        refs.setdefault(album, []).append(HeuristicRef(m_path, size, s_path, ts))
    return refs


def match_all(manifest: Manifest, cfg: MatchConfig) -> list[MatchDecision]:
    """Run the full cascade R1->R5 over every hashed-but-unmatched media item.

    Deterministic and idempotent: matched items leave the 'hashed' state,
    quarantined items are re-evaluated identically and their journal rows
    are deduplicated, so running twice yields the same decisions.

    Passes: (1) R1-R4 per media, (2) R5 proposals on still-orphan images,
    (3) iOS Live Photo pairing (.HEIC match extends to its .MOV sibling).
    """
    index = build_sidecar_index(manifest)
    sc_meta = _sidecar_meta_by_folder(manifest)
    folder_media = _media_names_by_folder(manifest)
    refs = _load_refs(manifest)
    decisions: list[MatchDecision] = []
    newly_matched: list[tuple[Path, str, str]] = []

    rows = manifest.pending_media(states=("hashed",))
    for m in sorted(rows, key=lambda r: r.path):
        media_path = Path(m.path)
        folder = media_path.parent
        local_index = index.get(folder, {})

        decision = match_r1_exact(media_path, local_index)
        if decision is None:
            decision = match_r2_truncated(
                media_path, local_index, folder_media.get(folder, set()), cfg.trunc_length
            )
        if decision is None:
            decision = match_r3_title(media_path, sc_meta.get(folder, []))
        if decision is None:
            decision = match_r4_heuristic(
                media_path,
                refs.get(m.album, []),
                cfg,
                m.size,
                media_path.stat().st_mtime,
            )
        if decision is None:
            decisions.append(
                MatchDecision(str(media_path), "", None, "UNMATCHED",
                              note="no rule matched in the cascade (R1-R5)")
            )
            continue
        decisions.append(decision)
        if decision.decision == "MATCH":
            manifest.set_matched(m.path, decision.sidecar, decision.rule, decision.note)
            sc = manifest.db.execute(
                "SELECT id, taken_timestamp FROM sidecars WHERE path=?", (decision.sidecar,)
            ).fetchone()
            if sc:
                refs.setdefault(m.album, []).append(
                    HeuristicRef(m.path, m.size, decision.sidecar, sc[1])
                )
            newly_matched.append((media_path, m.path, decision.sidecar))
        else:
            manifest.set_quarantined(m.path, decision.rule, decision.note)
        manifest.record_decision(m.path, decision.decision, f"[{decision.rule}] {decision.note}")

    # Pass 2: R5 perceptual proposals on images still without any decision.
    phash_cache: dict[str, str] = {}
    orphans = manifest.db.execute(
        "SELECT path FROM media WHERE state='hashed' AND match_rule IS NULL ORDER BY path"
    ).fetchall()
    matched_hashes: list[tuple[str, str]] = []
    if orphans:
        for (p,) in manifest.db.execute(
            """SELECT m.path FROM media m
               WHERE m.state IN ('matched','applied','verified')"""
        ).fetchall():
            if Path(p).suffix.lower() in VIDEO_SUFFIXES:
                continue
            h = phash_cache.get(p) or _phash_hex(Path(p))
            if h is not None:
                phash_cache[p] = h
                matched_hashes.append((p, h))
    for (p,) in orphans:
        media_path = Path(p)
        decision = match_r5_perceptual(media_path, matched_hashes, cfg, phash_cache)
        if decision is None:
            continue
        decisions.append(decision)
        manifest.set_quarantined(p, decision.rule, decision.note)
        manifest.record_decision(p, decision.decision, f"[{decision.rule}] {decision.note}")

    # Pass 3: iOS Live Photos — the .MOV sibling shares the .HEIC sidecar.
    for media_path, m_path, sidecar in newly_matched:
        if media_path.suffix.lower() not in LIVE_PHOTO_MEDIA:
            continue
        stem = media_path.stem
        folder = media_path.parent
        mov_name = next(
            (n for n in sorted(folder_media.get(folder, set()))
             if n.lower() == f"{stem.lower()}.mov"),
            None,
        )
        if mov_name is None:
            continue
        mov_path = folder / mov_name
        still = manifest.db.execute(
            "SELECT 1 FROM media WHERE path=? AND state='hashed' AND match_rule IS NULL",
            (str(mov_path),),
        ).fetchone()
        if not still:
            continue
        note = f"Live Photo pair: .MOV sibling of {media_path.name}"
        decision = MatchDecision(str(mov_path), "live_photo_pair", sidecar, "MATCH", note)
        decisions.append(decision)
        manifest.set_matched(str(mov_path), sidecar, "live_photo_pair", note)
        manifest.record_decision(str(mov_path), "MATCH", f"[live_photo_pair] {note}")

    manifest.commit()
    return decisions
