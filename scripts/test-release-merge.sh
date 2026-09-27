#!/usr/bin/env bash
# Regression check for ADR 0002 row 7: #61 fixed wait_for_main_version to
# skip its poll under --dry-run (a dry run never merges, so main's version
# never moves, and the old code always timed out and failed a preview that
# would otherwise be clean). #61 was checked by hand only. This proves it
# mechanically, and fails again if the guard is ever removed.
#
# release-merge.sh has no sourcing guard — its whole CLI body runs on load,
# including exiting immediately with no args — so the functions under test
# are extracted from the file text instead of sourcing it whole. That keeps
# this check from touching release-merge.sh's control flow at all.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="$REPO_ROOT/scripts/release-merge.sh"
# Referenced only from inside wait_for_main_version's body, which is loaded
# by eval below — invisible to shellcheck's static analysis of this file.
# shellcheck disable=SC2034
EXIT_PREFLIGHT=3
fails=0

# Same extraction for both: every line between the opening brace and the
# matching top-level closing brace, verbatim.
extract() { sed -n "/^$1() {/,/^}/p" "$TARGET"; }

log() { echo "  $*"; }
fail() { echo "STATUS:FAILED $1" >&2; return "${2:-1}"; }
eval "$(extract wait_for_main_version)"

check() {
  local name="$1" want_exit="$2"
  shift 2
  local got_exit=0
  "$@" || got_exit=$?
  if [ "$got_exit" = "$want_exit" ]; then
    echo "ok - $name"
  else
    echo "not ok - $name (exit $got_exit, wanted $want_exit)"
    fails=$((fails + 1))
  fi
}

# read_version_at_ref calls `gh api` for real. Stubbed here so a call proves
# the dry-run guard was bypassed, rather than silently succeeding or hanging
# on a real network call in CI. Deliberately unused in the passing case
# below — that it's never called is exactly what's being proven.
# shellcheck disable=SC2329
read_version_at_ref() {
  echo "read_version_at_ref called with ref=$1 — the dry-run guard did not return early" >&2
  return 1
}

# The regression this row exists for: under --dry-run, must return
# immediately without ever calling read_version_at_ref, regardless of
# whether "expected" could ever match (it can't — main's version never
# moves in a dry run, which is exactly what broke before #61).
# shellcheck disable=SC2034 # consulted from inside the eval'd function body
DRY_RUN=1
check "dry-run returns success without polling main" 0 \
  wait_for_main_version "9.9.9-unreachable"

# Sanity check on the harness itself, not just the guard: outside dry-run,
# a version that matches on the first read must still succeed, so a stub
# that always failed regardless of input couldn't make the check above
# pass for the wrong reason.
# shellcheck disable=SC2034 # consulted from inside the eval'd function body
DRY_RUN=0
read_version_at_ref() { echo "1.2.3"; }
check "non-dry-run still succeeds on an immediate match" 0 \
  wait_for_main_version "1.2.3"

if [ "$fails" -gt 0 ]; then
  echo "FAILED: $fails check(s)" >&2
  exit 1
fi
echo "All release-merge.sh checks passed"
