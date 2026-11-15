"""Module M6 — Reports: every decision is traceable, auditable, reversible.

Outputs (under the report dir):
  match_report.csv     : one row per decision (media, rule, sidecar, decision, note)
  quarantine_report.csv: quarantined items WITH the reason
  summary.html         : standalone dashboard (rates per rule, anomalies)
  audit_export.jsonl   : TEXT-only export (paths + stats, never pixels)
                         intended for an external LLM audit agent
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from .config import PathsConfig

MATCH_HEADER = ("media_path", "rule", "sidecar_path", "decision", "note")
QUARANTINE_HEADER = ("media_path", "sidecar_path", "reason")


def write_match_report(rows: list[tuple], report_dir: Path, cfg: PathsConfig) -> Path:
    """Write match_report.csv — well-formed even when empty (header only)."""
    report_dir.mkdir(parents=True, exist_ok=True)
    out = report_dir / cfg.match_report
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(MATCH_HEADER)
        for media, rule, sidecar, decision, note in rows:
            w.writerow((media, rule, sidecar, decision, note))
    return out


def write_quarantine_report(rows: list[tuple], report_dir: Path, cfg: PathsConfig) -> Path:
    out = report_dir / cfg.quarantine_report
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(QUARANTINE_HEADER)
        w.writerows(rows)
    return out


_HTML_TEMPLATE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<title>PhotoRestore — rapport d'audit</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:820px;margin:2rem auto;padding:0 1rem}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ddd;padding:.4rem .6rem;text-align:left}}
.ok{{color:#0a7d28}}.warn{{color:#b45309}}
</style></head><body>
<h1>PhotoRestore — rapport d'audit</h1>
<p>Mode : <strong>{mode}</strong> · Médias inventoriés : <strong>{total}</strong></p>
<h2>Décisions de matching</h2>
<table><tr><th>Règle</th><th>Décision</th><th>Nombre</th></tr>
{rows}
</table>
<h2>Prochaines étapes</h2>
<p>Lis <code>match_report.csv</code> puis relance avec <code>--apply</code> après relecture.
Après application sur le NAS : Synology Photos → icône Compte → Paramètres →
Indexation → <strong>Ré-indexer</strong> (sinon les dates affichées ne bougent pas).</p>
</body></html>
"""


def write_summary(
    counts: dict[tuple[str, str], int], total: int, mode: str, report_dir: Path, cfg: PathsConfig
) -> Path:
    """Standalone summary.html — no framework, one string template."""
    rows = "\n".join(
        f"<tr><td>{rule or '—'}</td><td>{decision}</td><td>{n}</td></tr>"
        for (rule, decision), n in sorted(counts.items())
    )
    out = report_dir / cfg.summary_html
    out.write_text(
        _HTML_TEMPLATE.format(mode=mode, total=total, rows=rows), encoding="utf-8"
    )
    return out


def write_audit_export(
    decisions: list[dict], report_dir: Path, cfg: PathsConfig
) -> Path:
    """Text-only JSONL export for an external audit agent. Paths + stats ONLY:
    privacy invariant — pixels never leave the machine."""
    out = report_dir / cfg.audit_export
    with open(out, "w", encoding="utf-8") as f:
        for d in decisions:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    return out
