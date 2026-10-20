#!/usr/bin/env bash
# Checks for scripts/release-merge.sh: a release tags the PR's own merge
# commit, never main's HEAD, in both the normal and the resume path, and a dry
# run calls nothing on GitHub.
#
# release-merge.sh runs its whole CLI on load, so the functions under test are
# extracted from the file text and `gh` is stubbed as a function. The stub
# gives main's HEAD and the merge commit different SHAs, so tagging the wrong
# one fails here.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="$REPO_ROOT/scripts/release-merge.sh"
# Read inside the eval'd function bodies, which shellcheck cannot see.
# shellcheck disable=SC2034
REPO="owner/repo"
# shellcheck disable=SC2034
EXIT_TAG=5
MERGE_OID="1111111111111111111111111111111111111111"
MAIN_HEAD="2222222222222222222222222222222222222222"
CALLS="$(mktemp -d)"
trap 'rm -rf "$CALLS"' EXIT
fails=0

extract() { sed -n "/^$1() {/,/^}/p" "$TARGET"; }
for fn in merge_commit_of tag_and_watch resume_merged; do
  body="$(extract "$fn")"
  [ -n "$body" ] || { echo "not ok - $fn not found in $TARGET"; exit 1; }
  eval "$body"
done

say() { :; }
log() { :; }
fail() { echo "STATUS:FAILED $1" >&2; exit "${2:-1}"; }
watch_for_tag() { echo "$1" >"$CALLS/watched"; }
read_version_at_ref() { echo "$1" >"$CALLS/version-ref"; echo "1.2.3"; }

# shellcheck disable=SC2317,SC2329 # called from the eval'd bodies
gh() {
  echo "$*" >>"$CALLS/gh"
  case "$*" in
    *"pr view"*) [ -n "${STUB_NO_MERGE:-}" ] || echo "$MERGE_OID" ;;
    *commits/main*) echo "$MAIN_HEAD" ;;
    *"-X POST"*)
      local a
      for a in "$@"; do
        case "$a" in sha=*) echo "${a#sha=}" >"$CALLS/tagged" ;; esac
      done
      ;;
    *git/ref/tags/*) [ -n "${STUB_TAG_EXISTS:-}" ] ;;
    *) echo "unexpected gh call: $*" >&2; return 1 ;;
  esac
}

# Runs "$@" in a subshell from a clean call record, then compares its exit
# status and what it tagged (or "none").
check() {
  local name="$1" want_exit="$2" want_tagged="$3" got_exit=0 got_tagged
  shift 3
  rm -f "$CALLS"/*
  ("$@") 2>/dev/null || got_exit=$?
  got_tagged="$(cat "$CALLS/tagged" 2>/dev/null || echo none)"
  if [ "$got_exit" = "$want_exit" ] && [ "$got_tagged" = "$want_tagged" ]; then
    echo "ok - $name"
  else
    echo "not ok - $name (exit $got_exit, tagged $got_tagged; wanted exit $want_exit, tagged $want_tagged)"
    fails=$((fails + 1))
  fi
}

# shellcheck disable=SC2034 # read inside the eval'd bodies
DRY_RUN=0
check "merge path tags the PR's merge commit" 0 "$MERGE_OID" tag_and_watch 1.2.3 42
check "resume path tags the PR's merge commit" 0 "$MERGE_OID" resume_merged 42
if [ "$(cat "$CALLS/version-ref")" = "$MERGE_OID" ]; then
  echo "ok - resume path reads the version at the merge commit"
else
  echo "not ok - resume path read the version at $(cat "$CALLS/version-ref")"
  fails=$((fails + 1))
fi
STUB_TAG_EXISTS=1 check "resume path with the tag present tags nothing" 0 none resume_merged 42
STUB_NO_MERGE=1 check "an unknown merge commit stops before tagging" 5 none tag_and_watch 1.2.3 42

# shellcheck disable=SC2034
DRY_RUN=1
check "dry run tags nothing" 0 none tag_and_watch 1.2.3 42
if [ -e "$CALLS/gh" ]; then
  echo "not ok - dry run called gh: $(cat "$CALLS/gh")"
  fails=$((fails + 1))
else
  echo "ok - dry run makes no gh call"
fi

if [ "$fails" -gt 0 ]; then
  echo "FAILED: $fails check(s)" >&2
  exit 1
fi
echo "All release-merge.sh checks passed"
