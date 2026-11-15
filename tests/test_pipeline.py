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
        self.assertEqual(len(body), 12)  # every media gets a decision row
        decisions = {r[3] for r in body}
        self.assertIn("MATCH", decisions)
        self.assertIn("QUARANTINE", decisions)
        self.assertIn("UNMATCHED", decisions)
        # quarantine report exists and is motivated
        qrows = list(csv.reader((self.reports / "quarantine_report.csv").open(encoding="utf-8")))
        self.assertEqual(qrows[0], ["media_path", "sidecar_path", "reason"])
        self.assertEqual(len(qrows) - 1, 1)
        self.assertTrue(qrows[1][2])  # reason is non-empty

    def test_cascade_matches_and_orphan_stays_unmatched(self) -> None:
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
        by_rule: dict[str, list[str]] = {}
        for d in decisions:
            by_rule.setdefault(d.rule or d.decision, []).append(d.media)
        # R1: IMG_0001, IMG_0004, IMG_0004(1), IMG_0005-edited, video,
        # sous-dossier pair, IMG_0006 = 7
        self.assertEqual(len(by_rule.get("R1_exact", [])), 7)
        self.assertEqual(len(by_rule.get("R2_truncated", [])), 1)  # long name
        self.assertEqual(len(by_rule.get("R3_title", [])), 1)       # IMG_0003
        # R5: perceptual pair -> QUARANTINE proposal, never a write
        self.assertEqual(len(by_rule.get("R5_perceptual", [])), 1)
        quarantined = by_rule["R5_perceptual"]
        self.assertTrue(quarantined[0].endswith("IMG_0006-copy.jpg"))
        unmatched = by_rule.get("UNMATCHED", [])
        orphans = [p for p in unmatched if p.endswith("orphan.jpg")]
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
