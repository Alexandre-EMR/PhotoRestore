"""Configuration loading for photorestore.

The default configuration ships as config.toml next to the repo root and is
overridable per-run. Values map 1:1 to the cahier des charges thresholds.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class MatchConfig:
    """Thresholds for the matching cascade (module M3)."""
    trunc_length: int = 46  # Google truncates sidecar names at 46 chars
    heuristic_size_tolerance: float = 0.02  # +/- 2%
    heuristic_time_window_hours: int = 48
    perceptual_hamming_max: int = 3  # R5: proposal only, quarantined for human review


@dataclass(frozen=True)
class DedupConfig:
    """Thresholds for perceptual deduplication (module M5)."""
    phash_prefilter_size: int = 8
    phash_confirm_size: int = 16
    hamming_same_image_max: int = 5  # <=5 near-certain duplicate
    hamming_variant_min: int = 6     # 6..10 = "variant" (crop/filter) -> human review
    hamming_variant_max: int = 10
    video_frame_interval_s: int = 10
    video_max_frames: int = 30
    video_dup_min_frames: int = 3
    video_frame_hamming_max: int = 8
    # Conservation priority: original > -edited > (1) > recompressed
    keep_priority: tuple[str, ...] = ("original", "edited", "numbered", "recompressed")


@dataclass(frozen=True)
class WriteConfig:
    """EXIF write policies (module M4)."""
    # "fill_missing": only fill absent EXIF fields (default, non-destructive).
    # "json_wins": sidecar JSON overrides existing EXIF values.
    # "ask": prompt interactively on conflicts (default: keep existing).
    policy: str = "fill_missing"
    min_year: int = 1900
    max_year: int = 2026
    exiftool: str = "exiftool"  # binary name or absolute path (external dep)


@dataclass(frozen=True)
class PathsConfig:
    duplicates_dir: str = "_duplicates"
    quarantine_dir: str = "_quarantine"
    match_report: str = "match_report.csv"
    quarantine_report: str = "quarantine_report.csv"
    summary_html: str = "summary.html"
    audit_export: str = "audit_export.jsonl"
    undo_manifest: str = "undo_manifest.csv"
    fix_report: str = "fix_report.csv"


@dataclass(frozen=True)
class AppConfig:
    match: MatchConfig = field(default_factory=MatchConfig)
    dedup: DedupConfig = field(default_factory=DedupConfig)
    write: WriteConfig = field(default_factory=WriteConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)


def load_config(path: Path | str | None = None) -> AppConfig:
    """Load config from a TOML file, falling back to defaults for every key.

    Never raises on partial files: unknown or missing keys keep defaults,
    so a minimal user config stays valid.
    """
    cfg = AppConfig()
    if path is None:
        return cfg
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"config file not found: {p}")
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    sections = {
        "match": (cfg.match, MatchConfig),
        "dedup": (cfg.dedup, DedupConfig),
        "write": (cfg.write, WriteConfig),
        "paths": (cfg.paths, PathsConfig),
    }
    rebuilt: dict[str, object] = {}
    for name, (current, cls) in sections.items():
        overrides = {k: v for k, v in data.get(name, {}).items() if k in current.__dict__}
        rebuilt[name] = cls(**{**current.__dict__, **overrides})  # type: ignore[operator]
    return AppConfig(**rebuilt)  # type: ignore[arg-type]
