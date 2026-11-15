"""Unit tests for the matching cascade (M3): R1-R5 + quarantine policy.

Every rule gets a positive test; every ambiguity path must end in
QUARANTINE (never a write); R5 must only ever propose (QUARANTINE).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from photorestore.config import MatchConfig  # noqa: E402
from photorestore.ingest import discover  # noqa: E402
from photorestore.manifest import Manifest, sha256_file  # noqa: E402
from photorestore.match import (  # noqa: E402
    match_all,
    match_r1_exact,
    match_r2_truncated,
    match_r3_title,
)
from tests.make_fixtures import LONG_NAME, TRUNC, generate, sidecar, write_png  # noqa: E402

CFG = MatchConfig()


def build_manifest(root: Path, db: Path) -> Manifest:
    """ingest + hash a corpus folder into a fresh manifest."""
    m = Manifest(db)
    for item in discover(root):
        if item.kind == "media":
            m.register_media(item.path, item.album, item.path.stat().st_size)
        elif item.kind == "sidecar":
            m.register_sidecar(item.path, item.album)
    for row in m.pending_media(states=("discovered",)):
        m.set_hashed(row.path, sha256_file(Path(row.path)))
    m.commit()
    return m


class R1Test(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="r1-"))

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

    def test_r1_old_format_json(self) -> None:
        album = self.tmp / "b"
        album.mkdir()
        (album / "photo.jpg").write_bytes(b"x")
        sc = album / "photo.jpg.json"  # OLD takeout naming
        sc.write_text("{}", encoding="utf-8")
        self.assertIsNotNone(match_r1_exact(album / "photo.jpg", {sc.name: str(sc)}))


class R2Test(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="r2-"))
        self.album = self.tmp / "album"
        self.album.mkdir()

    def test_r2_unique_truncated_match(self) -> None:
        write_png(self.album / LONG_NAME)
        sc = self.album / f"{TRUNC}.json"
        sc.write_text(json.dumps(sidecar(LONG_NAME)), encoding="utf-8")
        m = build_manifest(self.tmp, self.tmp / "m.db")
        decisions = {d.media: d for d in match_all(m, CFG)}
        d = decisions[str(self.album / LONG_NAME)]
        self.assertEqual(d.decision, "MATCH")
        self.assertEqual(d.rule, "R2_truncated")
        self.assertEqual(d.sidecar, str(sc))
        m.close()

    def test_r2_ambiguous_prefix_goes_to_quarantine(self) -> None:
        # two media sharing the first 46 chars + one truncated sidecar
        base = LONG_NAME
        twin = base[:46] + "X" + base[46:]  # same 46-char prefix, different tail
        self.assertEqual(base[:46], twin[:46])
        write_png(self.album / base)
        write_png(self.album / twin)
        sc = self.album / f"{TRUNC}.json"
        sc.write_text(json.dumps(sidecar(base)), encoding="utf-8")
        m = build_manifest(self.tmp, self.tmp / "m.db")
        decisions = {d.media: d for d in match_all(m, CFG)}
        for name in (base, twin):
            d = decisions[str(self.album / name)]
            self.assertEqual(d.decision, "QUARANTINE", name)
            self.assertEqual(d.rule, "R2_truncated")
        # invariant: nothing was written (state stays hashed, no sidecar claimed)
        self.assertEqual(m.claimed_sidecars(), set())
        m.close()

    def test_r2_unit_level_quarantine(self) -> None:
        media = self.album / LONG_NAME
        sc = self.album / f"{TRUNC}.json"
        index = {sc.name: str(sc)}
        twin = self.album / (LONG_NAME[:46] + "X" + LONG_NAME[46:])
        d = match_r2_truncated(media, index, set(), [str(media), str(twin)], 46)
        self.assertIsNotNone(d)
        self.assertEqual(d.decision, "QUARANTINE")
        d = match_r2_truncated(media, index, set(), [str(media)], 46)
        self.assertEqual(d.decision, "MATCH")


class R3Test(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="r3-"))
        self.album = self.tmp / "album"
        self.album.mkdir()

    def test_r3_title_match(self) -> None:
        write_png(self.album / "IMG_0003.jpg")
        sc = self.album / "unrelated-name.supplemental-metadata.json"
        sc.write_text(json.dumps(sidecar("IMG_0003.jpg")), encoding="utf-8")
        m = build_manifest(self.tmp, self.tmp / "m.db")
        decisions = {d.media: d for d in match_all(m, CFG)}
        d = decisions[str(self.album / "IMG_0003.jpg")]
        self.assertEqual(d.decision, "MATCH")
        self.assertEqual(d.rule, "R3_title")
        m.close()

    def test_r3_two_candidates_quarantine(self) -> None:
        media = self.album / "photo.jpg"
        write_png(media)
        for name in ("a.supplemental-metadata.json", "b.supplemental-metadata.json"):
            (self.album / name).write_text(
                json.dumps(sidecar("photo.jpg")), encoding="utf-8"
            )
        folder_sidecars = {
            str(self.album / n): ("photo.jpg", 1_600_000_000)
            for n in ("a.supplemental-metadata.json", "b.supplemental-metadata.json")
        }
        d = match_r3_title(media, folder_sidecars, claimed=set())
        self.assertIsNotNone(d)
        self.assertEqual(d.decision, "QUARANTINE")
        self.assertEqual(d.rule, "R3_title")


class R4Test(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="r4-"))
        self.album = self.tmp / "album"
        self.album.mkdir()

    def _setup(self, sidecar_ts: int, mtime: int = 1_700_000_000) -> Manifest:
        write_png(self.album / "copy.jpg", rgb=(10, 200, 90))     # pending media
        write_png(self.album / "orig.jpg", rgb=(10, 200, 90))     # stem peer (unregistered)
        (self.album / "orig.jpg.json").write_text(
            json.dumps(sidecar("orig.jpg", timestamp=sidecar_ts)), encoding="utf-8"
        )
        copy = self.album / "copy.jpg"
        os.utime(copy, (mtime, mtime))  # media date proxy = mtime
        m = Manifest(self.tmp / "m.db")
        # register ONLY copy.jpg + the sidecar (orig.jpg exists on disk only)
        m.register_media(copy, "album", copy.stat().st_size)
        m.register_sidecar(self.album / "orig.jpg.json", "album")
        m.set_hashed(str(copy), sha256_file(copy))
        m.commit()
        return m

    def test_r4_all_criteria_match(self) -> None:
        now = 1_700_000_000
        m = self._setup(now)
        decisions = {d.media: d for d in match_all(m, CFG)}
        d = decisions[str(self.album / "copy.jpg")]
        self.assertEqual(d.decision, "MATCH")
        self.assertEqual(d.rule, "R4_heuristic")
        m.close()

    def test_r4_time_window_violation_no_match(self) -> None:
        now = 1_700_000_000
        m = self._setup(now + 72 * 3600, mtime=now)  # sidecar 72h away: outside +/-48h
        decisions = {d.media: d for d in match_all(m, CFG)}
        d = decisions[str(self.album / "copy.jpg")]
        self.assertEqual(d.decision, "UNMATCHED")
        m.close()

    def test_r4_two_candidates_quarantine(self) -> None:
        now = 1_700_000_000
        m = self._setup(now)
        # add a second eligible sidecar with its own same-size peer
        write_png(self.album / "orig2.jpg", rgb=(10, 200, 90))
        (self.album / "orig2.jpg.json").write_text(
            json.dumps(sidecar("orig2.jpg", timestamp=now)), encoding="utf-8"
        )
        m.register_sidecar(self.album / "orig2.jpg.json", "album")
        m.commit()
        decisions = {d.media: d for d in match_all(m, CFG)}
        d = decisions[str(self.album / "copy.jpg")]
        self.assertEqual(d.decision, "QUARANTINE")
        self.assertEqual(d.rule, "R4_heuristic")
        self.assertEqual(m.claimed_sidecars(), set())
        m.close()


class R5Test(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="r5-"))
        generate(self.tmp / "corpus")
        self.db = self.tmp / "m.db"

    def test_r5_proposal_is_quarantine_never_match(self) -> None:
        m = build_manifest(self.tmp / "corpus", self.db)
        decisions = {d.media: d for d in match_all(m, CFG)}
        copy = next(p for p in decisions if p.endswith("IMG_0006-copy.jpg"))
        d = decisions[copy]
        self.assertEqual(d.decision, "QUARANTINE")
        self.assertEqual(d.rule, "R5_perceptual")
        self.assertIn("IMG_0006.jpg", d.note)
        # the sidecar-carrying twin is matched via R1, the copy gets NO sidecar
        twin = next(p for p in decisions if p.endswith("/IMG_0006.jpg"))
        self.assertEqual(decisions[twin].decision, "MATCH")
        m.close()

    def test_orphan_stays_unmatched(self) -> None:
        m = build_manifest(self.tmp / "corpus", self.db)
        decisions = {d.media: d for d in match_all(m, CFG)}
        orphan = next(p for p in decisions if p.endswith("orphan.jpg"))
        self.assertEqual(decisions[orphan].decision, "UNMATCHED")
        m.close()


class CascadeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cascade-"))
        generate(self.tmp / "corpus")
        self.db = self.tmp / "m.db"

    def test_full_cascade_on_fixtures(self) -> None:
        m = build_manifest(self.tmp / "corpus", self.db)
        decisions = match_all(m, CFG)
        by_rule: dict[str, list[str]] = {}
        for d in decisions:
            by_rule.setdefault(d.rule or d.decision, []).append(Path(d.media).name)
        self.assertEqual(len(by_rule.get("R1_exact", [])), 7)   # 6 old + IMG_0006
        self.assertEqual(by_rule.get("R2_truncated"), [LONG_NAME])
        self.assertEqual(by_rule.get("R3_title"), ["IMG_0003.jpg"])
        self.assertEqual(by_rule.get("R5_perceptual"), ["IMG_0006-copy.jpg"])
        self.assertEqual(
            sorted(by_rule.get("UNMATCHED", [])), ["orphan.jpg", "same-name.jpg"]
        )
        m.close()

    def test_idempotence_two_runs_same_decisions(self) -> None:
        m = build_manifest(self.tmp / "corpus", self.db)

        def state() -> list[tuple]:
            return sorted(
                [(a, b, c, "MATCH", e) for a, b, c, e in m.matched_decisions()]
                + list(m.pending_decisions())
            )

        match_all(m, CFG)
        first = state()
        match_all(m, CFG)  # second run: only still-open items are re-evaluated
        second = state()
        self.assertEqual(first, second, "re-running the cascade must change nothing")
        m.close()

    def test_no_sidecar_double_claimed(self) -> None:
        m = build_manifest(self.tmp / "corpus", self.db)
        match_all(m, CFG)
        rows = m.db.execute(
            "SELECT COUNT(DISTINCT matched_sidecar) FROM media "
            "WHERE matched_sidecar IS NOT NULL"
        ).fetchone()
        matched = m.db.execute(
            "SELECT COUNT(*) FROM media WHERE matched_sidecar IS NOT NULL"
        ).fetchone()
        self.assertEqual(rows[0], matched[0], "each sidecar claimed by at most one media")
        m.close()


if __name__ == "__main__":
    unittest.main()
