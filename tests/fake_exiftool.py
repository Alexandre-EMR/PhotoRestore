#!/usr/bin/env python3
"""Fake exiftool binary for tests (the sandbox has no real exiftool).

Simulates exiftool with a JSON tag store per file: <file>.fake_exif.json.
  read mode  : exiftool -j -n -Tag... <file>     -> prints a JSON array
  write mode : exiftool -overwrite_original -Tag=value ... <file>
Formats .gif/.bmp "reject EXIF" (exit 1) to exercise the XMP sidecar fallback.
GPS Refs are derived from the sign, like the real exiftool.
"""

import json
import os
import sys


def main() -> int:
    args = sys.argv[1:]
    if not args:
        return 1
    if "-j" in args:  # read mode
        tags = [
            a[1:] for a in args
            if a.startswith("-") and "=" not in a and a not in ("-j", "-n")
        ]
        path = args[-1]
        store = {}
        sp = path + ".fake_exif.json"
        if os.path.exists(sp):
            with open(sp, encoding="utf-8") as f:
                store = json.load(f)
        print(json.dumps([{t: store[t] for t in tags if t in store}]))
        return 0
    path = args[-1]
    if path.endswith((".gif", ".bmp")):
        print("Error: Unsupported file format", file=sys.stderr)
        return 1
    sp = path + ".fake_exif.json"
    store = {}
    if os.path.exists(sp):
        with open(sp, encoding="utf-8") as f:
            store = json.load(f)
    for a in args:
        if a.startswith("-") and "=" in a:
            tag, val = a[1:].split("=", 1)
            store[tag] = val
            if tag == "GPSLatitude":
                store["GPSLatitudeRef"] = "S" if val.startswith("-") else "N"
            if tag == "GPSLongitude":
                store["GPSLongitudeRef"] = "W" if val.startswith("-") else "E"
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(store, f)
    print("    1 image files updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
