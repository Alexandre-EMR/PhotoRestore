"""Unit tests for the manifest (M2): states, resumability, sidecar parsing."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from photorestore.manifest import Manifest, sha256_file  # noqa: E402
from tests.make_fixtures import generate, sidecar  # noqa: E402


class ManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="manifest-test-"))
        generate(self.tmp / "corpus")
        self.db = self.tmp / "manifest.db"

    def test_state_machine_and_resume(self) -> None:
        m = Manifest(self.db)
        media = sorted((self.tmp / "corpus").rglob("*.jpg"))[0]
        m.register_media(media, "album", media.stat().st_size)
        # a fresh media item is in 'discovered' state: pending() must see it
        self.assertEqual(len(m.pending_media(states=("discovered",))), 1)
        row = m.pending_media(states=("discovered", "hashed"))[0]
        m.set_hashed(row.path, sha256_file(media))
        # after hashing, the item is no longer 'discovered': resume skips it
        self.assertEqual(m.pending_media(states=("discovered",)), [])
        self.assertEqual(len(m.pending_media(states=("hashed",))), 1)
        m.set_matched(row.path, None, "R1_exact")
        self.assertEqual(m.counts().get("matched"), 1)
        m.close()

    def test_sidecar_parsing(self) -> None:
        m = Manifest(self.db)
        sc = self.tmp / "sample.supplemental-metadata.json"
        sc.write_text(
            __import__("json").dumps(sidecar("photo.jpg", timestamp=1_600_000_000)),
            encoding="utf-8",
        )
        m.register_sidecar(sc, "album")
        row = m.db.execute("SELECT title, taken_timestamp, latitude FROM sidecars").fetchone()
        self.assertEqual(row[0], "photo.jpg")
        self.assertEqual(row[1], 1_600_000_000)
        self.assertAlmostEqual(row[2], 43.2965)
        m.close()

    def test_bad_json_sidecar_does_not_crash(self) -> None:
        m = Manifest(self.db)
        sc = self.tmp / "broken.json"
        sc.write_text("{not json", encoding="utf-8")
        m.register_sidecar(sc, "album")  # must not raise
        row = m.db.execute("SELECT taken_timestamp FROM sidecars").fetchone()
        self.assertIsNone(row[0])
        m.close()


if __name__ == "__main__":
    unittest.main()
