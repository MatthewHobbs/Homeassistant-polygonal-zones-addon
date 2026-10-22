#!/usr/bin/env bash
# Checks for scripts/changelog-notes.sh: it finds a version's notes by exact
# heading and refuses a missing, empty or misspelt entry.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
log="$(mktemp)"
trap 'rm -f "$log"' EXIT
fails=0
# want: the expected notes, or "refuse".
check() {
  local name="$1" want="$2" version="$3" text="$4" got
  printf '%b' "$text" >"$log"
  # Blank lines are dropped: the release keeps them, and they are not the notes.
  got=$(./changelog-notes.sh "$version" "$log" 2>/dev/null | sed '/^[[:space:]]*$/d') || got=refuse
  if [ "$got" = "$want" ]; then echo "ok - $name"; else echo "not ok - $name (got '$got', wanted '$want')"; fails=$((fails + 1)); fi
}
check "a dated heading gives its notes" "- fix" 0.6.1 '# Changelog\n\n## 0.6.1 — 2026-10-22\n\n- fix\n\n## 0.6.0 — 2026-10-21\n\n- old\n'
check "a bare heading gives its notes" "- fix" 0.6.1 '## 0.6.1\n- fix\n## 0.6.0\n- old\n'
check "the notes stop at the next heading" "- old" 0.6.0 '## 0.6.1\n- fix\n## 0.6.0\n- old\n## 0.5.9\n- older\n'
check "a v-prefixed heading is refused" refuse 0.6.1 '## v0.6.1\n- fix\n'
check "0.6.1 does not match 0.6.10" refuse 0.6.1 '## 0.6.10\n- fix\n'
check "the dots are not wildcards" refuse 0.6.1 '## 0x6x1 — 2026-10-22\n- fix\n'
check "a heading with no notes is refused" refuse 0.6.1 '## 0.6.1\n\n## 0.6.0\n- old\n'
check "a missing entry is refused" refuse 0.6.2 '## 0.6.1\n- fix\n'
if [ "$fails" -gt 0 ]; then echo "FAILED: $fails check(s)" >&2; exit 1; fi
echo "All changelog-notes.sh checks passed"
