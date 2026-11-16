"""End-to-end pipeline tests (stdlib unittest — zero dependency).

Covers the P1 invariants of the cahier des charges:
  * fixtures generate the full edge-case corpus
  * ingest + match (R1) work end-to-end in dry-run
  * reports are well-formed (header present even when needed)
  * IDEMPOTENCE: running the pipeline twice yields identical reports
  * orphan media stay unmatched and are never written
"""

from __future__ import annotations

import csv
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from photorestore.cli import main as cli_main  # noqa: E402
from photorestore.config import load_config  # noqa: E402
from photorestore.manifest import Manifest, sha256_file  # noqa: E402
from photorestore.match import match_all  # noqa: E402
from tests.make_fixtures import generate  # noqa: E402

try:  # R5 needs the documented optional deps (Pillow + ImageHash)
    import imagehash  # noqa: F401
    import PIL.Image  # noqa: F401

    HAS_PERCEPTUAL = True
except ImportError:
    HAS_PERCEPTUAL = False


class PipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="photorestore-test-"))
        self.source = self.tmp / "corpus"
        stats = generate(self.source)
        self.assertGreaterEqual(stats["media"], 9)
        self.db = self.tmp / "manifest.db"
        self.reports = self.tmp / "reports"

    def run_cli(self, *argv: str) -> int:
        return cli_main(list(argv))

    def test_full_pipeline_dry_run(self) -> None:
        rc = self.run_cli(
            "--db", str(self.db), "--report-dir", str(self.reports),
            "run", str(self.source),
        )
        self.assertEqual(rc, 0)

        match_report = self.reports / "match_report.csv"
        summary = self.reports / "summary.html"
        self.assertTrue(match_report.exists())
        self.assertTrue(summary.exists())
        rows = list(csv.reader(match_report.open(encoding="utf-8")))
        self.assertEqual(rows[0], ["media_path", "rule", "sidecar_path", "decision", "note"])
        body = rows[1:]
        self.assertGreaterEqual(len(body), 9)  # every media gets a decision row

    def test_ambiguous_media_quarantined_and_reported(self) -> None:
        rc = self.run_cli(
            "--db", str(self.db), "--report-dir", str(self.reports),
            "run", str(self.source),
        )
        self.assertEqual(rc, 0)
        # the ambiguous fixture (both sidecar formats) must be QUARANTINE
        match_rows = list(csv.DictReader((self.reports / "match_report.csv").open(encoding="utf-8")))
        q = [r for r in match_rows if r["decision"] == "QUARANTINE"]
        # one quarantine per rule-ambiguity fixture (+1 R5 proposal if deps present)
        self.assertEqual(len(q), 5 + (1 if HAS_PERCEPTUAL else 0))
        q9 = [r for r in q if r["media_path"].endswith("IMG_0009.jpg")]
        self.assertEqual(len(q9), 1)
        self.assertIn("ambiguous", q9[0]["note"])
        # quarantine_report.csv exists, well-formed, with the reason
        q_report = self.reports / "quarantine_report.csv"
        self.assertTrue(q_report.exists())
        q_rows = list(csv.reader(q_report.open(encoding="utf-8")))
        self.assertEqual(q_rows[0], ["media_path", "sidecar_path", "reason"])
        self.assertEqual(len(q_rows), 1 + len(q))  # header + quarantined items
        self.assertIn("ambiguous", q_rows[1][2])

    def test_r1_matches_and_orphan_stays_unmatched(self) -> None:
        cfg = load_config(None)
        manifest = Manifest(self.db)
        # simulate ingest+hash (media AND sidecars must be registered)
        from photorestore.ingest import discover

        for item in discover(self.source):
            if item.kind == "media":
                manifest.register_media(item.path, item.album, item.path.stat().st_size)
            elif item.kind == "sidecar":
                manifest.register_sidecar(item.path, item.album)
        for m in manifest.pending_media(states=("discovered",)):
            manifest.set_hashed(m.path, sha256_file(Path(m.path)))
        manifest.commit()
        decisions = match_all(manifest, cfg.match)
        matched = [d for d in decisions if d.decision == "MATCH"]
        unmatched = [d for d in decisions if d.decision == "UNMATCHED"]
        quarantined = [d for d in decisions if d.decision == "QUARANTINE"]
        # R1: IMG_0001, IMG_0004, IMG_0004(1), IMG_0005-edited, the video,
        #     the sous-dossier pair, both R4 originals, ref.jpg, IMG_0010.HEIC
        # R2: the truncated long name; R3: IMG_0003; R4: IMG_0007;
        #     live_photo_pair: IMG_0010.MOV  -> 14 matches in total
        self.assertEqual(len(matched), 14)
        # one quarantine per ambiguity fixture (+1 R5 proposal if deps present)
        self.assertEqual(len(quarantined), 5 + (1 if HAS_PERCEPTUAL else 0))
        orphans = [d for d in unmatched if Path(d.media).name == "orphan.jpg"]
        self.assertEqual(len(orphans), 1, "orphan must stay unmatched")
        manifest.close()

    def test_idempotence_two_runs_identical(self) -> None:
        for _ in range(2):
            rc = self.run_cli(
                "--db", str(self.db), "--report-dir", str(self.reports),
                "run", str(self.source),
            )
            self.assertEqual(rc, 0)
        report = (self.reports / "match_report.csv").read_text(encoding="utf-8")
        rc = self.run_cli(
            "--db", str(self.db), "--report-dir", str(self.reports),
            "match",
        )
        self.assertEqual(rc, 0)
        report2 = (self.reports / "match_report.csv").read_text(encoding="utf-8")
        self.assertEqual(report, report2, "re-running must not change decisions")

    def test_fix_and_dedup_are_safe_stubs(self) -> None:
        rc = self.run_cli("--db", str(self.db), "--report-dir", str(self.reports), "fix")
        self.assertEqual(rc, 2)  # documented STUB_EXIT_CODE
        rc = self.run_cli(
            "--db", str(self.db), "--report-dir", str(self.reports), "dedup", str(self.source)
        )
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
