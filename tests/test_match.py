"""Unit tests for the matching cascade (M3): R1 + stub policy."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from photorestore.config import MatchConfig  # noqa: E402
from photorestore.match import (  # noqa: E402
    RuleNotImplemented,
    match_all,
    match_r1_exact,
    match_r2_truncated,
)
from tests.make_fixtures import generate  # noqa: E402


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

    def test_r2_stub_raises_explicitly(self) -> None:
        # a stub must never silently "succeed" — it must announce its absence
        with self.assertRaises(RuleNotImplemented):
            match_r2_truncated(Path("x.jpg"), {}, trunc=46)


if __name__ == "__main__":
    unittest.main()
