"""Fail when a vendored file no longer matches the hash recorded beside it.

The record is ``app/static/vendor/VENDOR.md``: every fenced line of the form
``<sha256>  <path>`` is checked against the file it names, and every file in the
vendor tree must be named there. A file copied in without a record, or edited in
place, is refused.

Usage: python3 scripts/check-vendor-hashes.py
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import sys

VENDOR_DIR = pathlib.Path("polygonal_zones_editor/app/static/vendor")
RECORD = VENDOR_DIR / "VENDOR.md"
LINE = re.compile(r"^([0-9a-f]{64}) {2}(\S+)$")


def recorded() -> dict[str, str]:
    """The hash each path is expected to have, from the fenced blocks."""
    hashes: dict[str, str] = {}
    for line in RECORD.read_text(encoding="utf-8").splitlines():
        match = LINE.match(line)
        if match:
            hashes[match.group(2)] = match.group(1)
    return hashes


def main() -> int:
    """Compare the tree with its record; return a process exit code."""
    if not RECORD.is_file():
        print(f"missing {RECORD}", file=sys.stderr)
        return 1
    want = recorded()
    present = {
        str(path.relative_to(VENDOR_DIR))
        for path in VENDOR_DIR.rglob("*")
        if path.is_file() and path.name != "VENDOR.md"
    }
    problems: list[str] = []
    for name in sorted(present - want.keys()):
        problems.append(f"{name}: present but not recorded")
    for name in sorted(want.keys() - present):
        problems.append(f"{name}: recorded but missing")
    for name in sorted(present & want.keys()):
        digest = hashlib.sha256((VENDOR_DIR / name).read_bytes()).hexdigest()
        if digest != want[name]:
            problems.append(f"{name}: hash {digest}, recorded {want[name]}")
    if problems:
        print("\n".join(problems), file=sys.stderr)
        print("Update VENDOR.md from the upstream release, do not edit the file.", file=sys.stderr)
        return 1
    print(f"{len(want)} vendored files match VENDOR.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
