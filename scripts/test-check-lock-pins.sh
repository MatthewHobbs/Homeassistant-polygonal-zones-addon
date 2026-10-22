#!/usr/bin/env bash
# Checks for scripts/check-lock-pins.sh.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fails=0
LOCK='# header\nruff==0.17.1 \\\n    --hash=sha256:aa\npytest-cov==7.1.0 \\\n    --hash=sha256:bb\n'
# want: pass, or refuse:<words> that the ::error:: line must contain.
check() {
  local name="$1" want="$2" src="$3" lock="$4" got=pass out
  printf '%b' "$src" >"$tmp/src"
  printf '%b' "$lock" >"$tmp/lock"
  out=$(./check-lock-pins.sh "$tmp/src" "$tmp/lock" 2>&1) || got=crash
  [ "$got" = crash ] && [[ "$out" == "::error::"* ]] && [[ "$want" == refuse:* ]] &&
    [[ "$out" == *"${want#refuse:}"* ]] && got="$want"
  if [ "$got" = "$want" ]; then echo "ok - $name"; else echo "not ok - $name (got $got, wanted $want: $out)"; fails=$((fails + 1)); fi
}
check "matching pins pass" pass 'ruff==0.17.1\npytest-cov==7.1.0\n' "$LOCK"
check "a bumped pin missing from the lock is refused" refuse:"ruff==0.17.2" 'ruff==0.17.2\n' "$LOCK"
check "a new package missing from the lock is refused" refuse:"httpx==0.28.1" 'ruff==0.17.1\nhttpx==0.28.1\n' "$LOCK"
check "names compare case and separator blind" pass 'Pytest_Cov==7.1.0\n' "$LOCK"
check "ranges and comments are ignored" pass '# tools\nstarlette>=1.8\nruff==0.17.1  # linter\n' "$LOCK"
check "a version prefix does not count as a match" refuse:"ruff==0.17" 'ruff==0.17\n' "$LOCK"
[ "$fails" -eq 0 ] || { echo "$fails check(s) failed"; exit 1; }
