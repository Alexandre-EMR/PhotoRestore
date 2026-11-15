"""Unit tests for M4 (exif_write) using a fake exiftool binary.

Covers the DoD of AGENTS.md P2/M4:
  - real write of DateTimeOriginal (epoch UTC -> exact time +00:00)
  - GPS with sign-derived Ref (N/S/E/W)
  - fill_missing idempotence (existing fields never overwritten, 2x apply = no-op)
  - json_wins policy
  - XMP sidecar fallback for a format that rejects EXIF (.gif)
  - lying extension (PNG named .jpg) -> journaled rename
  - out-of-bounds date -> skipped anomaly, zero write
  - dry-run writes nothing
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from photorestore.config import MatchConfig, WriteConfig  # noqa: E402
from photorestore.exif_write import fix_all, format_exif_datetime, sniff_format  # noqa: E402
from photorestore.ingest import discover  # noqa: E402
from photorestore.manifest import Manifest, sha256_file  # noqa: E402
from photorestore.match import match_all  # noqa: E402
from tests.make_fixtures import sidecar, write_png  # noqa: E402

FAKE_SRC = REPO / "tests" / "fake_exiftool.py"
TS_A = 1_600_000_000  # 2020-09-13 05:46:40 UTC


def make_exiftool(tmp: Path) -> str:
    dst = tmp / "fake-exiftool"
    shutil.copy(FAKE_SRC, dst)
    dst.chmod(0o755)
    return str(dst)


def build_corpus(root: Path) -> None:
    album = root / "corpus" / "album"
    album.mkdir(parents=True)
    (album / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 32)  # real JPEG magic
    (album / "a.jpg.supplemental-metadata.json").write_text(
        json.dumps(sidecar("a.jpg", timestamp=TS_A, lat=43.2965, lon=5.3698, description="plage")),
        encoding="utf-8",
    )
    (album / "b.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 32)
    (album / "b.jpg.supplemental-metadata.json").write_text(
        json.dumps(sidecar("b.jpg", timestamp=TS_A + 100, lat=-33.8688, lon=151.2093)),
        encoding="utf-8",
    )
    write_png(album / "lying.jpg", rgb=(70, 80, 90))  # PNG bytes, .jpg name
    (album / "lying.jpg.supplemental-metadata.json").write_text(
        json.dumps(sidecar("lying.jpg", timestamp=TS_A + 200)), encoding="utf-8"
    )
    (album / "anim.gif").write_bytes(b"GIF89a" + b"\x00" * 32)
    (album / "anim.gif.supplemental-metadata.json").write_text(
        json.dumps(sidecar("anim.gif", timestamp=TS_A + 300, description="anim")),
        encoding="utf-8",
    )
    (album / "old.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 32)
    ts_1800 = int(datetime(1800, 1, 1, tzinfo=timezone.utc).timestamp())
    (album / "old.jpg.supplemental-metadata.json").write_text(
        json.dumps(sidecar("old.jpg", timestamp=ts_1800)), encoding="utf-8"
    )


def build_manifest(root: Path) -> Manifest:
    m = Manifest(root / "m.db")
    for item in discover(root / "corpus"):
        if item.kind == "media":
            m.register_media(item.path, item.album, item.path.stat().st_size)
        elif item.kind == "sidecar":
            m.register_sidecar(item.path, item.album)
    for row in m.pending_media(states=("discovered",)):
        m.set_hashed(row.path, sha256_file(Path(row.path)))
    m.commit()
    match_all(m, MatchConfig())
    m.commit()
    return m


def store_of(media: Path) -> dict:
    sp = Path(str(media) + ".fake_exif.json")
    return json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}


class ExifWriteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="exif-"))
        build_corpus(self.tmp)
        self.exiftool = make_exiftool(self.tmp)
        self.album = self.tmp / "corpus" / "album"

    def cfg(self, policy: str = "fill_missing", exiftool: str | None = None) -> WriteConfig:
        return WriteConfig(policy=policy, exiftool=exiftool or self.exiftool)

    def test_datetime_format_is_utc_exact(self) -> None:
        self.assertEqual(format_exif_datetime(TS_A), "2020:09:13 12:26:40+00:00")

    def test_sniff_detects_lying_extension(self) -> None:
        self.assertEqual(sniff_format(self.album / "lying.jpg"), "png")
        self.assertEqual(sniff_format(self.album / "anim.gif"), "gif")

    def test_dry_run_writes_nothing(self) -> None:
        m = build_manifest(self.tmp)
        results = fix_all(m, self.cfg(), apply=False)
        statuses = {r.status for r in results}
        self.assertEqual(statuses, {"dry_run", "skipped"})  # old.jpg = date anomaly
        self.assertEqual(list(self.album.glob("*.fake_exif.json")), [])
        self.assertFalse((self.album / "lying.png").exists())
        states = {r[0] for r in m.db.execute("SELECT DISTINCT state FROM media")}
        self.assertEqual(states, {"matched"})
        m.close()

    def test_apply_writes_and_verifies(self) -> None:
        m = build_manifest(self.tmp)
        results = {Path(r.media).name: r for r in fix_all(m, self.cfg(), apply=True)}
        a = store_of(self.album / "a.jpg")
        self.assertEqual(a["DateTimeOriginal"], "2020:09:13 12:26:40+00:00")
        self.assertEqual(a["FileModifyDate"], "2020:09:13 12:26:40+00:00")
        self.assertEqual(float(a["GPSLatitude"]), 43.2965)
        self.assertEqual(a["GPSLatitudeRef"], "N")
        self.assertEqual(a["GPSLongitudeRef"], "E")
        self.assertEqual(a["XMP-dc:Description"], "plage")
        self.assertEqual(results["a.jpg"].status, "verified")
        b = store_of(self.album / "b.jpg")
        self.assertEqual(b["GPSLatitudeRef"], "S")  # sign-derived Ref
        self.assertEqual(b["GPSLongitudeRef"], "E")
        state = m.db.execute(
            "SELECT state FROM media WHERE path LIKE '%a.jpg' AND path NOT LIKE '%.json'"
        ).fetchone()
        self.assertEqual(state[0], "verified")
        m.close()

    def test_fill_missing_keeps_existing_values(self) -> None:
        m = build_manifest(self.tmp)
        # pre-existing EXIF: different DateTimeOriginal, no GPS
        sp = self.album / "a.jpg.fake_exif.json"
        sp.write_text(json.dumps({"DateTimeOriginal": "2019:01:01 00:00:00+00:00"}),
                      encoding="utf-8")
        results = {Path(r.media).name: r for r in fix_all(m, self.cfg(), apply=True)}
        a = store_of(self.album / "a.jpg")
        self.assertEqual(a["DateTimeOriginal"], "2019:01:01 00:00:00+00:00", "fill_missing must NOT overwrite")
        self.assertIn("GPSLatitude", a)  # absent fields are filled
        self.assertEqual(results["a.jpg"].status, "verified")
        m.close()

    def test_json_wins_overwrites(self) -> None:
        m = build_manifest(self.tmp)
        sp = self.album / "a.jpg.fake_exif.json"
        sp.write_text(json.dumps({"DateTimeOriginal": "2019:01:01 00:00:00+00:00"}),
                      encoding="utf-8")
        fix_all(m, self.cfg(policy="json_wins"), apply=True)
        a = store_of(self.album / "a.jpg")
        self.assertEqual(a["DateTimeOriginal"], "2020:09:13 12:26:40+00:00")
        m.close()

    def test_apply_twice_is_noop(self) -> None:
        m = build_manifest(self.tmp)
        fix_all(m, self.cfg(), apply=True)
        snap1 = m.fix_decisions()
        stores1 = {p.name: p.read_text() for p in self.album.glob("*.fake_exif.json")}
        results2 = fix_all(m, self.cfg(), apply=True)
        self.assertTrue(all(r.status == "skipped" for r in results2))
        self.assertEqual(len(results2), 1, "only the anomaly is re-evaluated")
        self.assertEqual(m.fix_decisions(), snap1)
        stores2 = {p.name: p.read_text() for p in self.album.glob("*.fake_exif.json")}
        self.assertEqual(stores1, stores2, "no byte changed on the second run")
        m.close()

    def test_xmp_sidecar_fallback_for_gif(self) -> None:
        m = build_manifest(self.tmp)
        results = {Path(r.media).name: r for r in fix_all(m, self.cfg(), apply=True)}
        self.assertEqual(results["anim.gif"].status, "verified_xmp")
        xmp = (self.album / "anim.gif.xmp").read_text(encoding="utf-8")
        self.assertIn("2020-09-13T12:31:40+00:00", xmp)
        self.assertIn("anim", xmp)
        m.close()

    def test_lying_extension_is_renamed_and_journaled(self) -> None:
        m = build_manifest(self.tmp)
        fix_all(m, self.cfg(), apply=True)
        self.assertFalse((self.album / "lying.jpg").exists())
        self.assertTrue((self.album / "lying.png").exists())
        paths = [r[0] for r in m.fix_decisions()]
        self.assertTrue(any(p.endswith("lying.png") for p in paths))
        notes = [r[4] for r in m.fix_decisions() if r[0].endswith("lying.png")]
        self.assertTrue(any("renamed" in n for n in notes), notes)
        m.close()

    def test_out_of_bounds_date_is_skipped(self) -> None:
        m = build_manifest(self.tmp)
        results = {Path(r.media).name: r for r in fix_all(m, self.cfg(), apply=True)}
        self.assertEqual(results["old.jpg"].status, "skipped")
        self.assertIn("1800", results["old.jpg"].detail)
        self.assertEqual(store_of(self.album / "old.jpg"), {})
        m.close()

    def test_apply_without_exiftool_fails_loudly(self) -> None:
        m = build_manifest(self.tmp)
        results = fix_all(m, self.cfg(exiftool="/nonexistent/exiftool"), apply=True)
        statuses = {Path(r.media).name: r.status for r in results}
        self.assertEqual(statuses["a.jpg"], "failed")
        self.assertEqual(statuses["old.jpg"], "skipped")  # anomaly decided before exiftool need
        m.close()


if __name__ == "__main__":
    unittest.main()
