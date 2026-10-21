#!/usr/bin/env bash
# Checks for scripts/check-version-order.sh: a release must be above every
# other released tag, compared as numbers, and may re-run for its own tag.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
fails=0
check() {
  local name="$1" want="$2" version="$3" tags="$4" got=0
  printf '%s\n' "$tags" | ./check-version-order.sh "$version" >/dev/null 2>&1 || got=$?
  [ "$got" -ne 0 ] && got=1
  if [ "$got" = "$want" ]; then echo "ok - $name"; else echo "not ok - $name (exit $got, wanted $want)"; fails=$((fails + 1)); fi
}
TAGS=$'v0.5.8\nv0.5.9\nv0.6.0'
check "a version above every tag passes" 0 0.6.1 "$TAGS"
check "a re-run for the latest tag passes" 0 0.6.0 "$TAGS"
check "a version below the latest is refused" 1 0.5.10 "$TAGS"
check "an older version re-run after a newer one is refused" 1 0.5.9 "$TAGS"
check "fields compare as numbers" 0 0.6.10 $'v0.6.9\nv0.6.2'
check "a lower major with a higher minor is refused" 1 0.9.0 $'v1.0.0'
check "tags that are not X.Y.Z are ignored" 0 0.1.0 $'vtest\nv1.0\nnot-a-tag'
check "no tags at all passes" 0 0.1.0 ""
check "a version that is not X.Y.Z is refused" 1 0.6 "$TAGS"
if [ "$fails" -gt 0 ]; then echo "FAILED: $fails check(s)" >&2; exit 1; fi
echo "All check-version-order.sh checks passed"
