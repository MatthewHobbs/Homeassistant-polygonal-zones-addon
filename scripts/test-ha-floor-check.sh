#!/usr/bin/env bash
# Offline checks for the bound in scripts/ha-floor-check.sh, through its
# HA_ADDON_CONFIG and HA_STABLE_JSON hooks.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
fails=0

# check <name> <declared floor> <stable Core> <wanted exit>
check() {
  local got=0
  printf 'homeassistant: "%s"\n' "$2" >"$WORK/config.yaml"
  printf '{"homeassistant": {"default": "%s"}}\n' "$3" >"$WORK/stable.json"
  HA_ADDON_CONFIG="$WORK/config.yaml" HA_STABLE_JSON="$WORK/stable.json" \
    "$REPO_ROOT/scripts/ha-floor-check.sh" >/dev/null 2>&1 || got=$?
  if [ "$got" = "$4" ]; then
    echo "ok - $1"
  else
    echo "not ok - $1 (floor $2, stable $3: exit $got, wanted $4)"
    fails=$((fails + 1))
  fi
}

check "in bound: the .1 of the month before stable" 2026.8.1 2026.9.3 0
check "out of bound: newer than that .1" 2026.8.2 2026.9.0 1
# Bound is 2026.12.1. Without the rollback it would be 2027.0.1, which passes.
check "January rolls back to the previous December" 2026.12.2 2027.1.0 1

if [ "$fails" -gt 0 ]; then
  echo "FAILED: $fails check(s)" >&2
  exit 1
fi
echo "All ha-floor-check.sh checks passed"
