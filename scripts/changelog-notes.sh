#!/usr/bin/env bash
# Print the CHANGELOG notes for version $1, or fail if there are none.
# The release reads its notes this way, and every PR runs it against
# config.yaml's version, so a bump with no usable entry fails before merge.
set -euo pipefail
version="$1"
changelog="${2:-polygonal_zones_editor/CHANGELOG.md}"
f='(0|[1-9][0-9]{0,8})'
[[ "$version" =~ ^$f\.$f\.$f$ ]] || { echo "::error::'$version' is not X.Y.Z" >&2; exit 1; }
# The heading is "## X.Y.Z" alone or followed by a space, matched as text so
# "0.2.1" never matches "0.2.10" and the dots are not wildcards.
notes=$(awk -v h="## $version" '
  index($0, h " ") == 1 || $0 == h {found=1; next}
  found && /^## / {exit}
  found {print}
' "$changelog")
if [ -z "$(printf '%s' "$notes" | tr -d '[:space:]')" ]; then
  # stderr: stdout carries the notes, and callers capture or discard it.
  echo "::error::No CHANGELOG entry for $version in $changelog (the heading must be '## $version')" >&2
  exit 1
fi
printf '%s\n' "$notes"
