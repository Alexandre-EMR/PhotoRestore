"""Unit tests for the matching cascade rules R2-R5 (M3, P2).

Covers the DoD of the P2a prompt: one test per rule (match case + ambiguous
case ending in QUARANTINE), Live Photo pairing, decision journaling in the
manifest "decisions" table, and idempotence (running the cascade twice
changes nothing).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from photorestore.config import MatchConfig  # noqa: E402
from photorestore.ingest import discover  # noqa: E402
from photorestore.manifest import Manifest, sha256_file  # noqa: E402
from photorestore.match import match_all  # noqa: E402
from tests.make_fixtures import LONG_NAME, generate  # noqa: E402

try:  # R5 needs the documented optional deps (Pillow + ImageHash)
    import imagehash  # noqa: F401
    import PIL.Image  # noqa: F401

    HAS_PERCEPTUAL = True
except ImportError:
    HAS_PERCEPTUAL = False


def build_manifest(source: Path, db: Path) -> Manifest:
    manifest = Manifest(db)
    for item in discover(source):
        if item.kind == "media":
            manifest.register_media(item.path, item.album, item.path.stat().st_size)
        elif item.kind == "sidecar":
            manifest.register_sidecar(item.path, item.album)
    for m in manifest.pending_media(states=("discovered",)):
        manifest.set_hashed(m.path, sha256_file(Path(m.path)))
    manifest.commit()
    return manifest


def state_of(manifest: Manifest, suffix: str) -> list[tuple[str, str, str]]:
    rows = manifest.db.execute(
        "SELECT path, state, COALESCE(match_rule,'') FROM media WHERE path LIKE ?",
        (f"%{suffix}",),
    ).fetchall()
    return [(Path(r[0]).name, r[1], r[2]) for r in rows]


class RuleCascadeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rules-test-"))
        self.source = self.tmp / "corpus"
        generate(self.source)
        self.manifest = build_manifest(self.source, self.tmp / "manifest.db")
        self.cfg = MatchConfig()

    def tearDown(self) -> None:
        self.manifest.close()

    def run_cascade(self) -> list:
        return match_all(self.manifest, self.cfg)

    def test_r2_truncated_match(self) -> None:
        self.run_cascade()
        rows = state_of(self.manifest, LONG_NAME)
        self.assertEqual(len(rows), 1)
        _name, state, rule = rows[0]
        self.assertEqual(state, "matched")
        self.assertEqual(rule, "R2_truncated")

    def test_r2_ambiguous_prefix_quarantined(self) -> None:
        self.run_cascade()
        rows = [r for r in state_of(self.manifest, "%") if r[0].startswith("b-very-long")]
        self.assertEqual(len(rows), 2)
        for _n, state, rule in rows:
            self.assertEqual(state, "hashed")
            self.assertEqual(rule, "R2_truncated")

    def test_r3_title_match(self) -> None:
        self.run_cascade()
        rows = state_of(self.manifest, "IMG_0003.jpg")
        self.assertEqual(rows[0][1], "matched")
        self.assertEqual(rows[0][2], "R3_title")

    def test_r3_ambiguous_title_quarantined(self) -> None:
        self.run_cascade()
        rows = state_of(self.manifest, "dup.jpg")
        self.assertEqual(rows[0][1], "hashed")
        self.assertEqual(rows[0][2], "R3_title")

    def test_r4_heuristic_match(self) -> None:
        self.run_cascade()
        rows = state_of(self.manifest, "IMG_0007.jpg")
        self.assertEqual(rows[0][1], "matched")
        self.assertEqual(rows[0][2], "R4_heuristic")
        side = self.manifest.db.execute(
            """SELECT s.path FROM media m JOIN sidecars s ON m.matched_sidecar=s.id
               WHERE m.path LIKE '%IMG_0007.jpg'"""
        ).fetchone()
        self.assertIsNotNone(side)
        self.assertTrue(side[0].endswith("IMG_0000A.jpg.supplemental-metadata.json"))

    def test_r4_ambiguous_quarantined(self) -> None:
        self.run_cascade()
        rows = state_of(self.manifest, "IMG_0008.jpg")
        self.assertEqual(rows[0][1], "hashed")
        self.assertEqual(rows[0][2], "R4_heuristic")

    @unittest.skipUnless(HAS_PERCEPTUAL, "Pillow + ImageHash required for R5")
    def test_r5_perceptual_proposal_quarantined_never_matched(self) -> None:
        self.run_cascade()
        rows = state_of(self.manifest, "dup-orphan.jpg")
        self.assertEqual(rows[0][1], "hashed")
        self.assertEqual(rows[0][2], "R5_perceptual")
        note = self.manifest.db.execute(
            "SELECT match_note FROM media WHERE path LIKE '%dup-orphan.jpg'"
        ).fetchone()[0]
        self.assertIn("proposal", note)

    def test_live_photo_pair_shares_sidecar(self) -> None:
        self.run_cascade()
        heic = state_of(self.manifest, "IMG_0010.HEIC")
        mov = state_of(self.manifest, "IMG_0010.MOV")
        self.assertEqual(heic[0][1], "matched")
        self.assertEqual(mov[0][1], "matched")
        self.assertEqual(mov[0][2], "live_photo_pair")
        rows = self.manifest.db.execute(
            """SELECT m.path, s.path FROM media m JOIN sidecars s ON m.matched_sidecar=s.id
               WHERE m.path LIKE '%IMG_0010%'"""
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual({Path(r[1]).name for r in rows},
                         {"IMG_0010.HEIC.supplemental-metadata.json"})

    def test_decisions_journal_and_idempotence(self) -> None:
        decisions = self.run_cascade()
        n_rows = self.manifest.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        n_journal = sum(1 for d in decisions if d.decision in ("MATCH", "QUARANTINE"))
        self.assertEqual(n_rows, n_journal)
        # every journal row references a media row and carries a timestamp
        orphan = self.manifest.db.execute(
            """SELECT d.verdict, d.note, d.timestamp, m.path FROM decisions d
               JOIN media m ON d.match_id = m.id LIMIT 1"""
        ).fetchone()
        self.assertIsNotNone(orphan)
        self.assertGreater(int(orphan[2]), 0)
        # second run: zero new journal rows, zero state changes
        before = self.manifest.db.execute(
            "SELECT path, state, COALESCE(match_rule,'') FROM media ORDER BY path"
        ).fetchall()
        self.run_cascade()
        after = self.manifest.db.execute(
            "SELECT path, state, COALESCE(match_rule,'') FROM media ORDER BY path"
        ).fetchall()
        self.assertEqual(before, after)
        n_rows2 = self.manifest.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        self.assertEqual(n_rows, n_rows2)


if __name__ == "__main__":
    unittest.main()
