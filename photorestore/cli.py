"""Module M8 — Command-line interface (stdlib argparse; zero dependencies).

Global contract:
  * dry-run is the DEFAULT; --apply is explicit and requires confirmation.
  * every subcommand is idempotent (safe to re-run).
  * no code path ever deletes a file.

Commands: ingest | match | fix | dedup | report | run | undo
`run` chains ingest -> match -> report (v0 pipeline). `fix` and `dedup`
are stubs that explain the roadmap step instead of doing anything unsafe.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from . import __version__
from .config import AppConfig, load_config
from .dedup import DedupNotImplemented, undo
from .exif_write import ExifWriteNotImplemented
from .ingest import discover, extract_archives
from .manifest import Manifest, sha256_file
from .match import match_all
from .report import (
    write_audit_export,
    write_match_report,
    write_quarantine_report,
    write_summary,
)

STUB_EXIT_CODE = 2  # explicit, documented: subcommand planned but not implemented


def _confirm_apply() -> bool:
    answer = input("Écriture RÉELLE demandée (--apply). Continuer ? [oui/N] ")
    return answer.strip().lower() in ("oui", "o", "y", "yes")


def cmd_ingest(args: argparse.Namespace, cfg: AppConfig) -> int:
    source = Path(args.source).expanduser().resolve()
    workdir = Path(args.workdir).expanduser().resolve() if args.workdir else None
    if args.zip_mode:
        if workdir is None:
            print("erreur: --workdir requis avec --zip-mode", file=sys.stderr)
            return 1
        root = extract_archives(source, workdir)
        print(f"archives extraites vers {root}")
    else:
        root = source
    items = discover(root)
    manifest = Manifest(Path(args.db))
    n_media = n_sidecar = n_album = 0
    for item in items:
        if item.kind == "media":
            manifest.register_media(item.path, item.album, item.path.stat().st_size)
            n_media += 1
        elif item.kind == "sidecar":
            manifest.register_sidecar(item.path, item.album)
            n_sidecar += 1
        else:
            n_album += 1
    manifest.commit()
    print(f"ingest: {n_media} médias, {n_sidecar} sidecars, {n_album} albums -> {args.db}")
    manifest.close()
    return 0


def cmd_match(args: argparse.Namespace, cfg: AppConfig) -> int:
    manifest = Manifest(Path(args.db))
    # hashing is part of match (resumable: only unhashed items are hashed)
    for m in manifest.pending_media(states=("discovered",)):
        manifest.set_hashed(m.path, sha256_file(Path(m.path)))
    manifest.commit()
    match_all(manifest, cfg.match)

    # Reports are rebuilt FROM THE MANIFEST (global state), not from the
    # current run: re-running is a strict no-op on the report files.
    quarantined = sorted(manifest.quarantined_media())
    q_paths = {q[0] for q in quarantined}
    rows = [
        (media, rule, sidecar, "MATCH", note)
        for media, rule, sidecar, note in sorted(manifest.matched_decisions())
    ]
    rows += [(path, rule, "", "QUARANTINE", note) for path, rule, note in quarantined]
    rows += [
        (m.path, "", "", "UNMATCHED", "no sure rule matched (R2-R5 planned)")
        for m in sorted(
            (r for r in manifest.pending_media(states=("discovered", "hashed"))
             if r.path not in q_paths),
            key=lambda r: r.path,
        )
    ]
    rows.sort(key=lambda r: r[0])
    report_dir = Path(args.report_dir)
    write_match_report(rows, report_dir, cfg.paths)
    write_quarantine_report(
        [(path, "", note) for path, _rule, note in quarantined], report_dir, cfg.paths
    )
    counts: dict[tuple[str, str], int] = {}
    for _media, rule, _sc, decision, _note in rows:
        counts[(rule, decision)] = counts.get((rule, decision), 0) + 1
    total = sum(manifest.counts().values())
    mode = "APPLY" if args.apply else "DRY-RUN (aucune écriture)"
    write_summary(counts, total, mode, report_dir, cfg.paths)
    write_audit_export(
        [{"media": media, "rule": rule, "decision": decision}
         for media, rule, _sc, decision, _note in rows],
        report_dir,
        cfg.paths,
    )
    n_ok = sum(1 for r in rows if r[3] == "MATCH")
    print(f"match: {n_ok} correspondances sûres / {len(rows)} évalués [{mode}]")
    print(f"rapports: {report_dir.resolve()}")
    manifest.close()
    return 0


def cmd_fix(args: argparse.Namespace, cfg: AppConfig) -> int:
    try:
        from .exif_write import apply_exif  # local import keeps stub obvious

        apply_exif(None, cfg.write, args.apply)
    except ExifWriteNotImplemented as e:
        print(f"[prévu en P2] {e}", file=sys.stderr)
        return STUB_EXIT_CODE
    return 0


def cmd_dedup(args: argparse.Namespace, cfg: AppConfig) -> int:
    from .dedup import dedup as run_dedup

    try:
        run_dedup(Path(args.source), cfg.dedup, args.apply)
    except DedupNotImplemented as e:
        print(f"[prévu en P3] {e}", file=sys.stderr)
        return STUB_EXIT_CODE
    return 0


def cmd_undo(args: argparse.Namespace, cfg: AppConfig) -> int:
    manifest_path = Path(args.report_dir) / cfg.paths.undo_manifest
    n = undo(manifest_path)
    print(f"undo: {n} déplacements annulés depuis {manifest_path}")
    return 0


def cmd_run(args: argparse.Namespace, cfg: AppConfig) -> int:
    rc = cmd_ingest(args, cfg)
    if rc != 0:
        return rc
    rc = cmd_match(args, cfg)
    return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="photorestore",
        description=(
            "Réassocie les métadonnées JSON Google Takeout à tes photos, "
            "prépare le dédoublonnage par pixels et la ré-indexation Synology. "
            "Dry-run par défaut : rien n'est écrit sans --apply."
        ),
    )
    p.add_argument("--version", action="version", version=f"photorestore {__version__}")
    p.add_argument("--config", default=None, help="fichier config.toml (optionnel)")
    p.add_argument("--db", default="photorestore.db", help="chemin de la base SQLite")
    p.add_argument("--report-dir", default="reports", help="dossier des rapports")
    p.add_argument(
        "--apply", action="store_true",
        help="autorise les écritures réelles (sinon dry-run complet)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("ingest", help="inventorier une arborescence extraite")
    sp.add_argument("source")
    sp.add_argument("--workdir", default=None, help="cible d'extraction (mode zip)")
    sp.add_argument("--zip-mode", action="store_true", help="source = dossier d'archives .zip")
    sp.set_defaults(func=cmd_ingest)

    sub.add_parser("match", help="cascade de matching + rapports").set_defaults(
        func=cmd_match, source=None, workdir=None, zip_mode=False
    )

    sub.add_parser("fix", help="(P2) écriture EXIF/XMP").set_defaults(func=cmd_fix)

    ded = sub.add_parser("dedup", help="(P3) dédoublonnage par pixels")
    ded.add_argument("source")
    ded.set_defaults(func=cmd_dedup)

    sub.add_parser("undo", help="annule les déplacements de doublons").set_defaults(
        func=cmd_undo
    )

    runp = sub.add_parser("run", help="pipeline complet: ingest + match + rapports")
    runp.add_argument("source")
    runp.add_argument("--workdir", default=None)
    runp.add_argument("--zip-mode", action="store_true")
    runp.set_defaults(func=cmd_run)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help(sys.stderr)
        return 1
    if getattr(args, "apply", False) and not _confirm_apply():
        print("abandon : --apply refusé, reste en dry-run.")
        return 1
    cfg = load_config(args.config)
    try:
        return int(args.func(args, cfg) or 0)
    except (OSError, sqlite3.Error) as e:
        print(f"erreur: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
