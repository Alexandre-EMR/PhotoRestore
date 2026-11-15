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
    decision TEXT NOT NULL DEFAULT ''
        CHECK (decision IN ('', 'MATCH', 'QUARANTINE', 'UNMATCHED'))
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
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(media)")}
        if "decision" not in cols:  # migration for DBs created before P2
            self.db.execute("ALTER TABLE media ADD COLUMN decision TEXT NOT NULL DEFAULT ''")
        self.db.commit()

    # -- registration ----------------------------------------------------

    def register_media(self, path: Path, album: str, size: int) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO media (path, album, size) VALUES (?, ?, ?)",
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
               match_note=?, decision='MATCH' WHERE path=?""",
            (sidecar_id, rule, note, path),
        )

    def set_decision(self, path: str, decision: str, rule: str, note: str = "") -> None:
        """Record a non-matching cascade outcome (QUARANTINE/UNMATCHED).

        The state stays 'hashed' so a later run re-evaluates deterministically;
        the decision column keeps the report stable between runs (idempotence).
        """
        self.db.execute(
            "UPDATE media SET decision=?, match_rule=?, match_note=? WHERE path=?",
            (decision, rule, note, path),
        )

    # -- perceptual hash cache ---------------------------------------------

    def get_phash(self, path: str) -> str | None:
        row = self.db.execute("SELECT phash FROM media WHERE path=?", (path,)).fetchone()
        return row[0] if row else None

    def set_phash(self, path: str, phash: str) -> None:
        self.db.execute("UPDATE media SET phash=? WHERE path=?", (phash, path))

    # -- cascade support queries -------------------------------------------

    def sidecar_rows(self) -> list[tuple[str, str, str | None, int | None]]:
        """(path, album, title, taken_timestamp) for every registered sidecar."""
        return self.db.execute(
            "SELECT path, album, title, taken_timestamp FROM sidecars"
        ).fetchall()

    def claimed_sidecars(self) -> set[str]:
        """Sidecar paths already matched to a media (in this or a previous run)."""
        rows = self.db.execute(
            """SELECT s.path FROM media m JOIN sidecars s ON m.matched_sidecar = s.id
               WHERE m.state IN ('matched','applied','verified')"""
        ).fetchall()
        return {r[0] for r in rows}

    def media_size_by_name(self, name: str) -> int | None:
        """Size of any registered media with this filename (R4 size criterion)."""
        row = self.db.execute(
            "SELECT size FROM media WHERE path LIKE ? ORDER BY path LIMIT 1",
            ("%/" + name,),
        ).fetchone()
        return row[0] if row else None

    def media_paths_in_folder(self, folder: Path) -> list[str]:
        """All known media paths directly inside `folder` (for R2 uniqueness)."""
        prefix = str(folder) + "/"
        rows = self.db.execute(
            "SELECT path FROM media WHERE path LIKE ?", (prefix + "%",)
        ).fetchall()
        return [r[0] for r in rows if Path(r[0]).parent == folder]

    def matched_media(self) -> list[tuple[str, str]]:
        """(path, album) of every media already carrying a sidecar (R5 pool)."""
        return self.db.execute(
            "SELECT path, album FROM media WHERE state IN ('matched','applied','verified')"
        ).fetchall()

    def matched_media_with_phash(self) -> list[tuple[str, str, str]]:
        """(path, album, phash) of media that already carry a sidecar (R5 pool)."""
        return self.db.execute(
            """SELECT path, album, phash FROM media
               WHERE state IN ('matched','applied','verified') AND phash IS NOT NULL"""
        ).fetchall()

    def pending_decisions(self) -> list[tuple[str, str, str, str, str]]:
        """(path, rule, sidecar, decision, note) for media still in 'hashed'."""
        return self.db.execute(
            """SELECT m.path, COALESCE(m.match_rule, ''), COALESCE(s.path, ''),
                      COALESCE(m.decision, 'UNMATCHED'), COALESCE(m.match_note, '')
               FROM media m LEFT JOIN sidecars s ON m.matched_sidecar = s.id
               WHERE m.state IN ('discovered','hashed')"""
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
