"""Module M4 — EXIF/XMP writing (planned for P2).

Non-destructive policies:
  - fill_missing (DEFAULT): only fill absent EXIF fields. Running --apply
    twice is a no-op (idempotence invariant).
  - json_wins: the Takeout JSON overrides existing EXIF values.

Writes (planned): DateTimeOriginal from the epoch-UTC `photoTakenTime`
timestamp (exact time with offset, NO timezone guessing), GPSLatitude /
GPSLongitude (Ref derived from sign), description -> XMP-dc:Description,
FileModifyDate synced. Formats without EXIF support get an XMP sidecar.

v0 is a stub: it refuses politely and explains what P2 will implement.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .config import WriteConfig

POLICIES = ("fill_missing", "json_wins")


class ExifWriteNotImplemented(NotImplementedError):
    """P2 scope. Raised instead of writing anything: safety first."""


def exiftool_available() -> bool:
    """The v0+ design wraps the real exiftool binary; never reimplements EXIF."""
    return shutil.which("exiftool") is not None


def apply_exif(manifest, cfg: WriteConfig, apply: bool) -> int:  # noqa: ANN001
    """Planned: write sidecar metadata into matched media. Stub in v0."""
    raise ExifWriteNotImplemented(
        "M4 (EXIF writing) is planned for P2. "
        "Run `photorestore match` then review match_report.csv; "
        "meanwhile use the reference exiftool command from README.md."
    )
