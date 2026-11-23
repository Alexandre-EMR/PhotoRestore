"""Unit tests for the matching cascade (M3): R1 + stub policy."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from photorestore.manifest import Manifest, sha256_file  # noqa: E402
from photorestore.ingest import discover  # noqa: E402
from photorestore.config import MatchConfig  # noqa: E402
from photorestore.match import (  # noqa: E402
    build_sidecar_index,
    match_all,
    match_r1_exact,
    match_r2_truncated,
)


class MatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="match-test-"))

    def test_r1_exact_hit(self) -> None:
        album = self.tmp / "a"
        album.mkdir()
        (album / "photo.jpg").write_bytes(b"x")
        sc = album / "photo.jpg.supplemental-metadata.json"
        sc.write_text("{}", encoding="utf-8")
        decision = match_r1_exact(album / "photo.jpg", {sc.name: str(sc)})
        self.assertIsNotNone(decision)
        self.assertEqual(decision.rule, "R1_exact")
        self.assertEqual(decision.decision, "MATCH")

    def test_r1_exact_miss_then_new_format(self) -> None:
        album = self.tmp / "b"
        album.mkdir()
        (album / "photo.jpg").write_bytes(b"x")
        sc = album / "photo.jpg.json"  # OLD takeout naming
        sc.write_text("{}", encoding="utf-8")
        decision = match_r1_exact(album / "photo.jpg", {sc.name: str(sc)})
        self.assertIsNotNone(decision)

    def test_r2_truncated_unique_match_and_ambiguity(self) -> None:
        album = self.tmp / "d"
        album.mkdir()
        long = "a-very-long-google-photos-filename-over-forty-six-characters.jpg"
        (album / long).write_bytes(b"x")
        sc = album / f"{long[:46]}.json"
        sc.write_text("{}", encoding="utf-8")
        decision = match_r2_truncated(
            album / long, {sc.name: str(sc)}, {long}, trunc=46
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision.rule, "R2_truncated")
        self.assertEqual(decision.decision, "MATCH")
        # ambiguity: two media share the truncated prefix => QUARANTINE
        twin = long.replace("characters.jpg", "characters-B.jpg")
        (album / twin).write_bytes(b"x")
        decision = match_r2_truncated(
            album / long, {sc.name: str(sc)}, {long, twin}, trunc=46
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision.decision, "QUARANTINE")

    def test_r1_ambiguity_quarantines(self) -> None:
        # both sidecar formats for one media => QUARANTINE, never a silent pick
        album = self.tmp / "c"
        album.mkdir()
        (album / "photo.jpg").write_bytes(b"x")
        new_sc = album / "photo.jpg.supplemental-metadata.json"
        old_sc = album / "photo.jpg.json"
        new_sc.write_text("{}", encoding="utf-8")
        old_sc.write_text("{}", encoding="utf-8")
        decision = match_r1_exact(
            album / "photo.jpg", {new_sc.name: str(new_sc), old_sc.name: str(old_sc)}
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision.decision, "QUARANTINE")

    def test_same_sidecar_name_in_two_folders_both_match(self) -> None:
        # the sidecar index must be folder-keyed: same filename in two
        # albums must not collide
        db = self.tmp / "idx.db"
        for folder in ("albumA", "albumB"):
            d = self.tmp / folder
            d.mkdir()
            (d / "photo.jpg").write_bytes(b"x")
            (d / "photo.jpg.json").write_text("{}", encoding="utf-8")
        m = Manifest(db)
        for item in discover(self.tmp):
            if item.kind == "media":
                m.register_media(item.path, item.album, 1)
            elif item.kind == "sidecar":
                m.register_sidecar(item.path, item.album)
        for r in m.pending_media(states=("discovered",)):
            m.set_hashed(r.path, sha256_file(Path(r.path)))
        m.commit()
        index = build_sidecar_index(m)
        self.assertEqual(len(index), 2)  # two distinct folder keys
        decisions = match_all(m, MatchConfig())
        self.assertEqual(len([d for d in decisions if d.decision == "MATCH"]), 2)
        m.close()


if __name__ == "__main__":
    unittest.main()
