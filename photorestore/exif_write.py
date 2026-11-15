"""Module M4 — EXIF/XMP writing via the exiftool binary (subprocess wrapper).

The real exiftool is an external dependency (never reimplemented here);
tests inject a fake exiftool binary through WriteConfig.exiftool.

Policies (config [write] policy):
  - fill_missing (DEFAULT): only fill absent EXIF fields. Re-running --apply
    is a no-op on disk (idempotence invariant).
  - json_wins: the Takeout JSON overrides existing EXIF values.
  - ask: conflicts are prompted interactively (default answer: keep existing).

Writes, from the sidecar JSON:
  - DateTimeOriginal / FileModifyDate from the epoch-UTC photoTakenTime
    timestamp: exact time with +00:00 offset, NO local-timezone guessing.
  - GPSLatitude / GPSLongitude as signed values (exiftool derives the Ref
    from the sign). A (0,0) GPS is an anomaly: skipped, never written.
  - description -> XMP-dc:Description.

Safety rails:
  - formats that reject EXIF get an XMP sidecar (<media>.xmp), never a crash.
  - lying extensions (PNG bytes named .jpg, sniffed by magic bytes) are
    renamed with the correct extension; the rename is journaled in the
    manifest and fix_report.csv.
  - post-write verification: EXIF is read back and compared to the expected
    values; state becomes 'verified', or a blocking failure is reported.
  - dates outside [min_year, max_year] are skipped as anomalies.

INVARIANT: nothing is ever deleted; dry-run is the caller's default.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import WriteConfig
from .manifest import Manifest

POLICIES = ("fill_missing", "json_wins", "ask")

READ_ARGS = ("-j", "-n", "-DateTimeOriginal", "-GPSLatitude", "-GPSLongitude",
             "-XMP-dc:Description", "-FileModifyDate")

# Magic-byte sniffing -> canonical extension (lying-extension detection).
_FORMAT_EXT = {
    "png": ".png", "jpeg": ".jpg", "gif": ".gif", "webp": ".webp",
    "heic": ".heic", "mp4": ".mp4", "mov": ".mov", "tiff": ".tiff", "bmp": ".bmp",
}


@dataclass(frozen=True)
class WriteAction:
    tag: str
    value: str
    why: str  # "missing" | "json_wins" | "ask"


@dataclass(frozen=True)
class FixResult:
    media: str
    sidecar: str | None
    status: str  # "dry_run" | "verified" | "verified_xmp" | "failed" | "skipped" | "noop"
    detail: str
    actions: tuple[WriteAction, ...]


def resolve_exiftool(cfg: WriteConfig) -> str | None:
    """Absolute path to the exiftool binary, or None when unavailable."""
    candidate = Path(cfg.exiftool)
    if candidate.is_file():
        return str(candidate)
    return shutil.which(cfg.exiftool)


def format_exif_datetime(ts: int) -> str:
    """Epoch-UTC -> exiftool date string with explicit offset, no tz guessing."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y:%m:%d %H:%M:%S+00:00")


def exif_datetime_to_epoch(value: str) -> int | None:
    """Parse an exiftool date string back to epoch (naive values read as UTC)."""
    value = value.strip()
    for fmt in ("%Y:%m:%d %H:%M:%S%z", "%Y:%m:%d %H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    return None


def read_exif(path: Path, exiftool: str | None) -> dict[str, str]:
    """Current tags of a file as {tag: value} ({} when exiftool is absent)."""
    if exiftool is None:
        return {}
    proc = subprocess.run(
        [exiftool, *READ_ARGS, str(path)], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        return {}
    try:
        rows = json.loads(proc.stdout or "[]")
    except ValueError:
        return {}
    return {k: v for k, v in (rows[0] if rows else {}).items() if isinstance(v, (str, int, float))}


def sniff_format(path: Path) -> str | None:
    """Real format from magic bytes (lying-extension detection), else None."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return None
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"mif1", b"hevc", b"heim"):
            return "heic"
        if brand in (b"qt  ", b"mp41", b"mp42", b"isom", b"M4V "):
            return "mov" if brand == b"qt  " else "mp4"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    if head.startswith(b"BM"):
        return "bmp"
    return None


def plan_for_media(
    current: dict[str, str],
    ts: int | None,
    lat: float | None,
    lon: float | None,
    description: str | None,
    cfg: WriteConfig,
) -> tuple[list[WriteAction], str]:
    """Build the write actions for one media according to the policy.

    Returns (actions, skip_reason). An out-of-bounds date or a (0,0) GPS
    never produces a write action — anomalies are skipped, not guessed.
    """
    if ts is not None:
        year = datetime.fromtimestamp(ts, tz=timezone.utc).year
        if not cfg.min_year <= year <= cfg.max_year:
            return [], f"anomaly: taken-time year {year} outside [{cfg.min_year},{cfg.max_year}]"
    actions: list[WriteAction] = []

    def decide(tag: str, value: str) -> None:
        existing = current.get(tag)
        if existing is None or existing == "":
            actions.append(WriteAction(tag, value, "missing"))
        elif existing != value:
            if cfg.policy == "json_wins":
                actions.append(WriteAction(tag, value, "json_wins"))
            elif cfg.policy == "ask":
                actions.append(WriteAction(tag, value, "ask"))
            # fill_missing: keep the existing value — never overwrite

    if ts is not None:
        dt = format_exif_datetime(ts)
        decide("DateTimeOriginal", dt)
        decide("FileModifyDate", dt)
    if lat is not None and lon is not None and not (lat == 0.0 and lon == 0.0):
        decide("GPSLatitude", repr(float(lat)))
        decide("GPSLongitude", repr(float(lon)))
    if description:
        decide("XMP-dc:Description", description)
    return actions, ""


def write_exif(path: Path, actions: list[WriteAction], exiftool: str) -> bool:
    """Write tags via exiftool -overwrite_original. False = format rejected EXIF."""
    args = [exiftool, "-overwrite_original"]
    args += [f"-{a.tag}={a.value}" for a in actions]
    args.append(str(path))
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return False
    out = (proc.stdout or "") + (proc.stderr or "")
    return "1 image files updated" in out


_XMP_TEMPLATE = """<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:exif="http://ns.adobe.com/exif/1.0/"
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:dc="http://purl.org/dc/elements/1.1/">
   {fields}
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""


def write_xmp_sidecar(path: Path, ts: int | None, lat: float | None, lon: float | None,
                      description: str | None) -> Path:
    """XMP sidecar fallback for formats that do not accept EXIF."""
    fields = []
    if ts is not None:
        iso = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        fields.append(f'<exif:DateTimeOriginal>{iso}</exif:DateTimeOriginal>')
        fields.append(f'<xmp:ModifyDate>{iso}</xmp:ModifyDate>')
    if lat is not None and lon is not None and not (lat == 0.0 and lon == 0.0):
        fields.append(f"<exif:GPSLatitude>{lat}</exif:GPSLatitude>")
        fields.append(f"<exif:GPSLongitude>{lon}</exif:GPSLongitude>")
    if description:
        fields.append(f"<dc:description><rdf:Alt><rdf:li xml:lang=\"x-default\">{description}</rdf:li></rdf:Alt></dc:description>")
    xmp = path.parent / f"{path.name}.xmp"
    xmp.write_text(_XMP_TEMPLATE.format(fields="\n   ".join(fields)), encoding="utf-8")
    return xmp


def verify_actions(path: Path, actions: tuple[WriteAction, ...],
                   exiftool: str | None) -> tuple[bool, str]:
    """Read the EXIF back and compare ONLY the tags we wrote.

    Under fill_missing, pre-existing fields are deliberately left untouched
    (possibly different from the JSON): they must not fail verification.
    """
    current = read_exif(path, exiftool)
    for a in actions:
        got = current.get(a.tag)
        if a.tag in ("DateTimeOriginal", "FileModifyDate"):
            if exif_datetime_to_epoch(str(got or "")) != exif_datetime_to_epoch(a.value):
                return False, f"{a.tag} read back as {got!r}, expected {a.value!r}"
        elif a.tag in ("GPSLatitude", "GPSLongitude"):
            try:
                if abs(float(got) - float(a.value)) > 1e-6:  # type: ignore[arg-type]
                    return False, f"{a.tag} read back as {got!r}, expected {a.value!r}"
            except (TypeError, ValueError):
                return False, f"{a.tag} missing after write"
        elif got != a.value:
            return False, f"{a.tag} read back as {got!r}, expected {a.value!r}"
    return True, "read-back matches the written values"


def verify_xmp(xmp_path: Path, ts: int | None, description: str | None) -> tuple[bool, str]:
    """Sidecar fallback verification: the .xmp exists and holds the values."""
    if not xmp_path.is_file():
        return False, "XMP sidecar missing"
    text = xmp_path.read_text(encoding="utf-8")
    if ts is not None:
        iso = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        if iso not in text:
            return False, "XMP sidecar lacks DateTimeOriginal"
    if description and description not in text:
        return False, "XMP sidecar lacks description"
    return True, "XMP sidecar contains expected values"


def _confirm_ask(actions: list[WriteAction]) -> list[WriteAction]:
    """Policy 'ask': interactive confirmation per conflicting field, default keep."""
    kept: list[WriteAction] = []
    for a in actions:
        if a.why != "ask":
            kept.append(a)
            continue
        answer = input(f"{a.tag}: existing value differs — overwrite with {a.value!r}? [y/N] ")
        if answer.strip().lower() in ("y", "yes", "oui", "o"):
            kept.append(a)
    return kept


def fix_all(manifest: Manifest, cfg: WriteConfig, apply: bool) -> list[FixResult]:
    """Plan (dry-run) or apply (with verification) EXIF writes for matched media.

    Processes 'matched' and 'applied' media: re-running --apply re-reads the
    EXIF, finds the fields already present under fill_missing, writes nothing
    and re-verifies — the idempotence invariant made visible.
    """
    if cfg.policy not in POLICIES:
        raise ValueError(f"unknown write policy: {cfg.policy!r} (expected one of {POLICIES})")
    exiftool = resolve_exiftool(cfg)
    results: list[FixResult] = []
    for media_path, _title, ts, lat, lon, desc in manifest.matched_with_sidecar():
        path = Path(media_path)
        current = read_exif(path, exiftool)
        actions, skip = plan_for_media(current, ts, lat, lon, desc, cfg)
        sidecar_note = "" if exiftool else " (exiftool absent: current EXIF assumed empty)"

        if skip:
            results.append(FixResult(media_path, None, "skipped", skip, ()))
            if not apply:
                manifest.set_fix_note(media_path, f"dry-run: {skip}")
            else:
                manifest.set_fix_note(media_path, skip)
            continue

        # lying extension: sniffed format != declared extension -> journaled rename
        fmt = sniff_format(path)
        rename_to: Path | None = None
        if fmt is not None and _FORMAT_EXT[fmt] != path.suffix.lower():
            candidate = path.with_suffix(_FORMAT_EXT[fmt])
            if not candidate.exists():
                rename_to = candidate

        if not apply:
            detail = "dry-run: would write " + ", ".join(a.tag for a in actions) if actions else "dry-run: nothing to write"
            if rename_to is not None:
                detail += f"; would rename to {rename_to.name}"
            detail += sidecar_note
            manifest.set_fix_note(media_path, detail)
            results.append(FixResult(media_path, None, "dry_run", detail, tuple(actions)))
            continue

        if exiftool is None:
            results.append(FixResult(media_path, None, "failed",
                                     "exiftool binary required for --apply", ()))
            manifest.set_fix_note(media_path, "failed: exiftool binary not found")
            continue

        rename_note = ""
        if rename_to is not None:
            os.rename(path, rename_to)
            manifest.rename_media(media_path, str(rename_to))
            rename_note = f"renamed {Path(media_path).name} -> {rename_to.name} (lying extension); "
            path = rename_to
            media_path = str(rename_to)

        if cfg.policy == "ask":
            actions = _confirm_ask(actions)

        if not actions:
            # fill_missing idempotence: fields already present -> no write.
            manifest.set_verified(media_path, "no write needed (fields already present)")
            results.append(FixResult(media_path, None, "verified",
                                     "no write needed (fields already present)", ()))
            continue

        manifest.set_applied(media_path)
        if write_exif(path, actions, exiftool):
            ok, why = verify_actions(path, tuple(actions), exiftool)
            if ok:
                manifest.set_verified(
                    media_path, f"{rename_note}verified: {', '.join(a.tag for a in actions)}"
                )
                results.append(FixResult(media_path, None, "verified", rename_note + why, tuple(actions)))
            else:
                manifest.set_fix_note(media_path, f"verify_failed: {why}")
                results.append(FixResult(media_path, None, "failed", why, tuple(actions)))
        else:
            xmp = write_xmp_sidecar(path, ts, lat, lon, desc)
            ok, why = verify_xmp(xmp, ts, desc)
            if ok:
                manifest.set_verified(media_path, f"{rename_note}verified_xmp: {xmp.name}")
                results.append(FixResult(media_path, None, "verified_xmp",
                                         f"{rename_note}format rejected EXIF -> XMP sidecar {xmp.name}",
                                         tuple(actions)))
            else:
                manifest.set_fix_note(media_path, f"verify_failed (xmp): {why}")
                results.append(FixResult(media_path, None, "failed", why, tuple(actions)))
    manifest.commit()
    return results
