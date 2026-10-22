#!/usr/bin/env bash
# check-lock-pins.sh <requirements file> <lock file>
#
# Every exact pin (name==version) in the requirements file must appear, same
# version, in its hash lock. CI installs only the lock, while ruff's CI job
# reads its version from the requirements file, so a one-sided edit would
# otherwise go unnoticed.
set -euo pipefail
src="$1" lock="$2"
for f in "$src" "$lock"; do
  [ -r "$f" ] || { echo "::error::cannot read $f" >&2; exit 1; }
done
# Package names compare case- and separator-blind (PEP 503); versions exactly.
norm() { awk -F'==' '{ n = tolower($1); gsub(/[_.]/, "-", n); print n "==" $2 }'; }
locked=$(grep -E '^[A-Za-z0-9._-]+==' "$lock" | sed 's/[ \\].*//' | norm)
missing=0 pins=0
while IFS= read -r pin; do
  pins=$((pins + 1))
  if ! grep -qxF "$pin" <<<"$locked"; then
    echo "::error::$pin from $src is not in $lock; regenerate it with the command in its header" >&2
    missing=1
  fi
done < <(sed 's/#.*//; s/[[:space:]]//g; s/\[[^]]*\]==/==/' "$src" | grep -E '^[A-Za-z0-9._-]+==' | norm || true)
# No pins read means the check checked nothing; say so rather than pass.
[ "$pins" -gt 0 ] || { echo "::error::no name==version pins in $src" >&2; exit 1; }
exit "$missing"
