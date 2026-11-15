"""photorestore — restore Google Takeout JSON metadata to your media.

Local-first CLI that re-associates Google Photos Takeout sidecar metadata
(.supplemental-metadata.json / .json) with the matching photos and videos,
deduplicates by pixel content, logs every decision, and prepares Synology
Photos re-indexing.

Design invariants (see AGENTS.md):
  1. Dry-run by default; --apply is explicit.
  2. Idempotent: running twice changes nothing.
  3. No code path ever deletes a file; duplicates go to quarantine with an
     undo manifest.
"""

__version__ = "0.1.0"
