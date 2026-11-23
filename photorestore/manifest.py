"""Module M2 — SQLite manifest: the single source of truth for the run.

Every media file and sidecar gets one row. A run is RESUMABLE: interrupted
executions pick up where they stopped (state machine per media item).

States (per media):
  discovered -> hashed -> matched -> applied -> verified
Sidecars are stateless: registered once with their parsed metadata.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

MEDIA_STATES = ("discovered", "hashed", "matched", "applied", "verified")

SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    album TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT,
    phash TEXT,
    state TEXT NOT NULL DEFAULT 'discovered'
        CHECK (state IN ('discovered','hashed','matched','applied','verified')),
    matched_sidecar INTEGER REFERENCES sidecars(id),
    match_rule TEXT,
    match_note TEXT,
    fix_note TEXT
);
CREATE TABLE IF NOT EXISTS sidecars (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    album TEXT NOT NULL DEFAULT '',
    title TEXT,
    taken_timestamp INTEGER,
    latitude REAL,
    longitude REAL,
    description TEXT
);
CREATE INDEX IF NOT EXISTS idx_media_state ON media(state);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    match_id INTEGER NOT NULL REFERENCES media(id),
    verdict TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    timestamp INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class MediaRow:
    path: str
    album: str
    size: int
    state: str


class Manifest:
    """Thin, explicit wrapper around sqlite3. WAL mode for safe resumption."""

    def __init__(self, db_path: Path) -> None:
        self.db = sqlite3.connect(db_path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        cols = {row[1] for row in self.db.execute("PRAGMA table_info(media)")}
        if "fix_note" not in cols:  # migration for DBs created before M4
            self.db.execute("ALTER TABLE media ADD COLUMN fix_note TEXT")
        self.db.commit()

    # -- registration ----------------------------------------------------

    def register_media(self, path: Path, album: str, size: int) -> None:
        """Upsert: a re-ingest refreshes size/album (file may have changed)
        without ever resetting the pipeline state of an existing row."""
        self.db.execute(
            """INSERT INTO media (path, album, size) VALUES (?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET album=excluded.album, size=excluded.size""",
            (str(path), album, size),
        )

    def register_sidecar(self, path: Path, album: str) -> None:
        """Insert the sidecar row (if new) and parse its Takeout JSON fields."""
        self.db.execute(
            "INSERT OR IGNORE INTO sidecars (path, album) VALUES (?, ?)",
            (str(path), album),
        )
        meta = _parse_sidecar(path)
        self.db.execute(
            """UPDATE sidecars SET title=?, taken_timestamp=?, latitude=?,
               longitude=?, description=? WHERE path=?""",
            (meta["title"], meta["timestamp"], meta["latitude"],
             meta["longitude"], meta["description"], str(path)),
        )

    # -- pipeline state ----------------------------------------------------

    def pending_media(self, states: tuple[str, ...] = ("discovered",)) -> list[MediaRow]:
        q = ",".join("?" * len(states))
        rows = self.db.execute(
            f"SELECT path, album, size, state FROM media WHERE state IN ({q})",
            states,
        ).fetchall()
        return [MediaRow(*r) for r in rows]

    def set_hashed(self, path: str, sha256: str) -> None:
        self.db.execute(
            "UPDATE media SET sha256=?, state='hashed' WHERE path=?", (sha256, path)
        )

    def set_matched(self, path: str, sidecar_path: str | None, rule: str, note: str = "") -> None:
        sidecar_id: int | None = None
        if sidecar_path:
            row = self.db.execute(
                "SELECT id FROM sidecars WHERE path=?", (sidecar_path,)
            ).fetchone()
            sidecar_id = row[0] if row else None
        self.db.execute(
            """UPDATE media SET state='matched', matched_sidecar=?, match_rule=?,
               match_note=? WHERE path=?""",
            (sidecar_id, rule, note, path),
        )

    def set_quarantined(self, path: str, rule: str, note: str = "") -> None:
        """Record a quarantine decision WITHOUT changing the media state:
        the item stays in 'hashed' so a re-run re-evaluates it identically."""
        self.db.execute(
            "UPDATE media SET match_rule=?, match_note=? WHERE path=?", (rule, note, path)
        )

    def quarantined_media(self) -> list[tuple[str, str, str]]:
        """Rows for the quarantine report: (path, rule, reason)."""
        return self.db.execute(
            """SELECT path, COALESCE(match_rule, ''), COALESCE(match_note, '')
               FROM media WHERE state='hashed' AND match_rule IS NOT NULL"""
        ).fetchall()

    def counts(self) -> dict[str, int]:
        rows = self.db.execute(
            "SELECT state, COUNT(*) FROM media GROUP BY state"
        ).fetchall()
        return dict(rows)

    def matched_decisions(self) -> list[tuple[str, str, str, str]]:
        """Rows for the match report: (media_path, rule, sidecar_path, note)."""
        return self.db.execute(
            """SELECT m.path, COALESCE(m.match_rule, ''), COALESCE(s.path, ''),
                      COALESCE(m.match_note, '')
               FROM media m LEFT JOIN sidecars s ON m.matched_sidecar = s.id
               WHERE m.state IN ('matched','applied','verified')"""
        ).fetchall()

    def record_decision(self, path: str, verdict: str, note: str = "") -> None:
        """Append a decision to the audit journal (idempotent).

        The cascade re-evaluates quarantined items identically on re-run, so
        an identical (media, verdict, note) row is NOT inserted twice:
        running the whole pipeline 2x leaves the journal unchanged.
        """
        row = self.db.execute("SELECT id FROM media WHERE path=?", (path,)).fetchone()
        if row is None:
            return
        media_id = row[0]
        exists = self.db.execute(
            "SELECT 1 FROM decisions WHERE match_id=? AND verdict=? AND note=?",
            (media_id, verdict, note),
        ).fetchone()
        if exists:
            return
        self.db.execute(
            "INSERT INTO decisions (match_id, verdict, note, timestamp) VALUES (?, ?, ?, ?)",
            (media_id, verdict, note, int(time.time())),
        )

    # -- M4: EXIF/XMP fix (exif_write) ------------------------------------

    def matched_with_sidecar(
        self, states: tuple[str, ...] = ("matched", "applied")
    ) -> list[tuple[str, str | None, int | None, float | None, float | None, str | None]]:
        """(media_path, title, taken_timestamp, lat, lon, description) per matched media."""
        q = ",".join("?" * len(states))
        return self.db.execute(
            f"""SELECT m.path, s.title, s.taken_timestamp, s.latitude, s.longitude, s.description
                FROM media m JOIN sidecars s ON m.matched_sidecar = s.id
                WHERE m.state IN ({q}) ORDER BY m.path""",
            states,
        ).fetchall()

    def set_fix_note(self, path: str, note: str) -> None:
        """Record a fix outcome (dry-run plan / failure) without changing state."""
        self.db.execute("UPDATE media SET fix_note=? WHERE path=?", (note, path))

    def rename_media(self, old: str, new: str) -> None:
        """After a journaled extension correction (lying extension)."""
        self.db.execute("UPDATE media SET path=? WHERE path=?", (new, old))

    def set_applied(self, path: str) -> None:
        self.db.execute("UPDATE media SET state='applied' WHERE path=?", (path,))

    def set_verified(self, path: str, note: str = "") -> None:
        self.db.execute(
            "UPDATE media SET state='verified', fix_note=? WHERE path=?", (note, path)
        )

    def fix_decisions(self) -> list[tuple[str, str, str, str, str]]:
        """(media, sidecar, rule, state, fix_note) for every media carrying a sidecar."""
        return self.db.execute(
            """SELECT m.path, COALESCE(s.path, ''), COALESCE(m.match_rule, ''),
                      m.state, COALESCE(m.fix_note, '')
               FROM media m LEFT JOIN sidecars s ON m.matched_sidecar = s.id
               WHERE m.matched_sidecar IS NOT NULL ORDER BY m.path"""
        ).fetchall()

    def commit(self) -> None:
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _parse_sidecar(path: Path) -> dict[str, object]:
    """Extract the Takeout fields we need. Returns defaults on malformed JSON."""
    out: dict[str, object] = {
        "title": None, "timestamp": None,
        "latitude": None, "longitude": None, "description": None,
    }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    out["title"] = data.get("title")
    out["description"] = data.get("description") or None
    geo = data.get("geoData") or {}
    out["latitude"] = geo.get("latitude")
    out["longitude"] = geo.get("longitude")
    taken = data.get("photoTakenTime") or {}
    ts = taken.get("timestamp")
    try:
        out["timestamp"] = int(ts) if ts is not None else None
    except (TypeError, ValueError):
        out["timestamp"] = None
    return out
