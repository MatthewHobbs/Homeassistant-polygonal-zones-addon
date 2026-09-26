#!/usr/bin/env bash
# Hold the add-on's declared Home Assistant floor to the global rule (ADR 0001
# row 12): config.yaml's `homeassistant:` is never newer than the `.1` release
# of the month before current stable Core, and January rolls back to the
# previous December. Current stable is `homeassistant.default` in stable.json.
#
#   scripts/ha-floor-check.sh              # fetch stable.json and check
#   scripts/ha-floor-check.sh --declared   # print the declared floor, nothing else
#
# Environment:
#   HA_STABLE_JSON   a local stable.json to use instead of fetching (offline tests).
#   HA_STABLE_URL    where to fetch from (default version.home-assistant.io).
#   HA_ADDON_CONFIG  the config.yaml to read (default the add-on's).
#
# Deliberately not a required check (owner, 2026-09-25): it reads a third
# party's endpoint, and an outage there must not block every merge.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${HA_ADDON_CONFIG:-$REPO_ROOT/polygonal_zones_editor/config.yaml}"
STABLE_URL="${HA_STABLE_URL:-https://version.home-assistant.io/stable.json}"

fail() {
  printf 'FAIL ha-floor: %s\n' "$*" >&2
  exit 1
}

# Same shape supervisor-pilot.sh reads `version:` with: a quoted or bare
# scalar alone on its line. Anything else is left for python to reject.
declared_floor() {
  sed -n 's/^homeassistant: *"\{0,1\}\([^"#]*\)"\{0,1\} *$/\1/p' "$CONFIG"
}

mode="${1:-check}"
case "$mode" in
  check | --declared) ;;
  *)
    echo "usage: $0 [--declared]" >&2
    exit 2
    ;;
esac

[[ -r "$CONFIG" ]] || fail "cannot read $CONFIG"
declared="$(declared_floor)"

stable_file=""
if [[ "$mode" == check ]]; then
  if [[ -n "${HA_STABLE_JSON:-}" ]]; then
    stable_file="$HA_STABLE_JSON"
  else
    stable_file="$(mktemp)"
    trap 'rm -f "$stable_file"' EXIT
    # curl, not urllib: the endpoint answers urllib's default User-Agent
    # with 403. --fail turns a 403 or 404 body into an error, not a pass.
    curl -sS --fail --max-time 20 "$STABLE_URL" -o "$stable_file" ||
      fail "could not fetch $STABLE_URL"
  fi
fi

python3 - "$mode" "$declared" "$stable_file" "$CONFIG" <<'PY'
import json
import re
import sys

mode, declared, stable_path, config = sys.argv[1:5]
VERSION = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d+)$")


def parse(label, text):
    match = VERSION.match(text.strip())
    if not match:
        sys.exit(f"FAIL ha-floor: {label} {text!r} is not a YYYY.M.P version")
    year, month, patch = (int(g) for g in match.groups())
    if not 1 <= month <= 12:
        sys.exit(f"FAIL ha-floor: {label} {text!r} has month {month}")
    return (year, month, patch)


def fmt(version):
    return ".".join(str(n) for n in version)


if not declared.strip():
    sys.exit(
        f"FAIL ha-floor: no homeassistant: key in {config} "
        "(expected a quoted or bare YYYY.M.P version alone on its line)"
    )
floor = parse("declared floor", declared)
if mode == "--declared":
    print(fmt(floor))
    sys.exit(0)

try:
    with open(stable_path) as f:
        current = json.load(f)["homeassistant"]["default"]
except (OSError, ValueError, KeyError, TypeError) as exc:
    sys.exit(f"FAIL ha-floor: stable.json unusable ({type(exc).__name__}: {exc})")
if not isinstance(current, str):
    sys.exit(f"FAIL ha-floor: stable.json homeassistant.default is {current!r}")
stable = parse("stable Core", current)

year, month = stable[0], stable[1] - 1
if month == 0:
    year, month = year - 1, 12
bound = (year, month, 1)

if floor > bound:
    sys.exit(
        f"FAIL ha-floor: declared floor {fmt(floor)} is newer than the bound "
        f"{fmt(bound)} (stable Core {fmt(stable)}): the floor may be at most "
        "the .1 of the month before current stable"
    )
print(f"OK ha-floor: declared floor {fmt(floor)} <= bound {fmt(bound)} (stable Core {fmt(stable)})")
PY
