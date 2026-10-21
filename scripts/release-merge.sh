#!/usr/bin/env bash
# release-merge.sh — squash-merge a PR and, if it bumped the addon version,
# tag the merge commit and watch the release workflow to completion.
#
# Usage:
#   scripts/release-merge.sh <pr-number>           # normal flow
#   scripts/release-merge.sh --dry-run <pr-number> # preview without mutating
#   scripts/release-merge.sh <pr-number>           # auto-resumes if PR is
#                                                  # already MERGED
#
# Exit codes:
#   0  success
#   2  usage error
#   3  pre-flight failure (PR state, base not main, checks, mergeability,
#      a bad or lower version, or a resume whose version is not main's)
#   4  merge failed
#   5  tag creation failed, or the merge commit is unknown
#   6  release workflow failed
#
# stdout uses STATUS:<TOKEN> lines so a wrapper
# can summarise progress without screen-scraping freeform text.

set -euo pipefail

REPO="MatthewHobbs/Homeassistant-polygonal-zones-addon"
CONFIG="polygonal_zones_editor/config.yaml"

# Required CI checks. If any of these is missing from the PR's status rollup,
# or hasn't reached SUCCESS, the script refuses to merge. Keep it equal to the
# checks main's branch protection requires.
REQUIRED_CHECKS=(
  "pytest"
  "lint"
  "shellcheck"
  "Build (amd64)"
  "Build (aarch64)"
  "apparmor"
)

EXIT_USAGE=2
EXIT_PREFLIGHT=3
EXIT_MERGE=4
EXIT_TAG=5
EXIT_RELEASE=6

usage() {
  cat <<EOF
usage: $0 [--dry-run] <pr-number>

Squash-merge the PR. If it bumps polygonal_zones_editor/config.yaml's
version, tag the merge commit vX.Y.Z and watch the release workflow.

If the PR is already MERGED, auto-resumes (creates the tag if missing,
otherwise locates the existing release run and watches it).

Options:
  --dry-run    Print every action without mutating anything.
  -h, --help   Show this help.
EOF
}

# ───── helpers ──────────────────────────────────────────────────────

say() { echo "STATUS:$1${2:+ $2}"; }
log() { echo "  $*"; }
fail() { echo "STATUS:FAILED $1" >&2; exit "${2:-1}"; }

read_version_at_ref() {
  gh api "repos/$REPO/contents/$CONFIG?ref=$1" --jq .content \
    | base64 -d \
    | awk -F'"' '/^version:/{print $2; exit}'
}

find_run_for_tag() {
  gh -R "$REPO" run list \
    --workflow=release.yml \
    --branch="$1" \
    --limit=1 \
    --json databaseId \
    --jq '.[0].databaseId // empty'
}

watch_run() {
  if [ "$DRY_RUN" = 1 ]; then
    log "[dry-run] would watch run $1"
    return 0
  fi
  if ! gh -R "$REPO" run watch "$1" --exit-status --interval 10; then
    fail "release-workflow $1" $EXIT_RELEASE
  fi
}

# The PR's own squash commit, not main's HEAD: another merge may already have
# moved main.
merge_commit_of() {
  local oid i
  # GitHub can report a just-merged PR before its merge commit is filled in.
  for i in 1 2 3 4 5; do
    oid=$(gh -R "$REPO" pr view "$1" --json mergeCommit -q '.mergeCommit.oid // empty')
    if [ -n "$oid" ]; then echo "$oid"; return 0; fi
    [ "$i" = 5 ] || sleep "${MERGE_POLL_SECONDS:-2}"
  done
}

# Releases come only from main: a PR into any other branch would tag and
# publish a commit that main never contains.
require_main_base() {
  [ "$1" = "main" ] || fail "pr-base-$1-not-main" $EXIT_PREFLIGHT
}

# A release must be X.Y.Z and above main's $2: a lower one would publish as the
# newest. Compared field by field, so 0.5.10 is above 0.5.9.
require_release_version() {
  # No leading zeros, and short enough that bash arithmetic cannot wrap.
  local f='(0|[1-9][0-9]{0,8})' re i new old
  re="^$f\.$f\.$f\$"
  [[ "$1" =~ $re ]] || fail "bad-pr-version: '$1'" $EXIT_PREFLIGHT
  [[ "$2" =~ $re ]] || fail "bad-main-version: '$2'" $EXIT_PREFLIGHT
  IFS=. read -ra new <<<"$1"
  IFS=. read -ra old <<<"$2"
  for i in 0 1 2; do
    ((10#${new[i]} > 10#${old[i]})) && return 0
    ((10#${new[i]} < 10#${old[i]})) && break
  done
  fail "version-not-above-main: $1, main is $2" $EXIT_PREFLIGHT
}

# Tag PR $2's merge commit v$1, find the resulting release run, and watch it.
tag_and_watch() {
  local tag="v$1" pr="$2" sha
  if [ "$DRY_RUN" = 1 ]; then
    log "[dry-run] would create tag $tag at #$pr's merge commit"
  else
    sha=$(merge_commit_of "$pr")
    [ -n "$sha" ] || fail "merge-commit-unknown: #$pr" $EXIT_TAG
    say TAGGING "$tag at $sha"
    if ! gh api -X POST "repos/$REPO/git/refs" \
        -f ref="refs/tags/$tag" -f sha="$sha" >/dev/null; then
      fail "tag-push-failed: $tag" $EXIT_TAG
    fi
  fi
  watch_for_tag "$tag"
}

# PR $1 is already merged: tag its merge commit with the version it carries,
# unless that tag exists, then watch the release run.
resume_merged() {
  local pr="$1" sha version
  sha=$(merge_commit_of "$pr")
  [ -n "$sha" ] || fail "merge-commit-unknown: #$pr" $EXIT_TAG
  version=$(read_version_at_ref "$sha")
  log "merge commit $sha carries $version"
  if gh api "repos/$REPO/git/ref/tags/v$version" >/dev/null 2>&1; then
    say RESUMING "v$version (tag already exists)"
    watch_for_tag "v$version"
  else
    # Only main's current version may be released: an older merged PR whose
    # version was superseded must never be published after the fact.
    local f='(0|[1-9][0-9]{0,8})' re
    re="^$f\.$f\.$f\$"
    [[ "$version" =~ $re ]] || fail "bad-version-at-merge-commit: '$version'" $EXIT_PREFLIGHT
    [ "$version" = "$MAIN_VERSION" ] || fail "resume-not-main-version: v$version, main is $MAIN_VERSION" $EXIT_PREFLIGHT
    log "tag v$version missing; finishing release"
    tag_and_watch "$version" "$pr"
  fi
}

# Locate the release.yml run for $1 and watch it.
watch_for_tag() {
  local tag="$1" run_id=""
  say WAITING_FOR_RUN "$tag"
  if [ "$DRY_RUN" = 0 ]; then
    for _ in $(seq 1 24); do
      run_id=$(find_run_for_tag "$tag")
      [ -n "$run_id" ] && break
      sleep 5
    done
    [ -n "$run_id" ] || fail "release-run-not-found-for-$tag" $EXIT_RELEASE
  else
    run_id="<dry-run>"
  fi
  say WATCHING_RUN "$run_id"
  watch_run "$run_id"
  say SUCCESS "$tag released to ghcr.io"
}

# ───── arg parsing ──────────────────────────────────────────────────

DRY_RUN=0
PR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; PR="${1:-}"; shift || true ;;
    -*) echo "unknown flag: $1" >&2; usage >&2; exit $EXIT_USAGE ;;
    *) PR="$1"; shift ;;
  esac
done
[ -n "$PR" ] || { usage >&2; exit $EXIT_USAGE; }

# ───── pre-flight ───────────────────────────────────────────────────

say CHECKING_PR "#$PR"
PR_JSON=$(gh -R "$REPO" pr view "$PR" \
  --json state,headRefOid,statusCheckRollup,mergeable,title,baseRefName)
STATE=$(jq -r .state <<<"$PR_JSON")
TITLE=$(jq -r .title <<<"$PR_JSON")
log "title: $TITLE"
log "state: $STATE"

MAIN_VERSION=$(read_version_at_ref main)
log "main version: $MAIN_VERSION"
require_main_base "$(jq -r .baseRefName <<<"$PR_JSON")"

# ───── auto-resume: PR already merged ──────────────────────────────

if [ "$STATE" = "MERGED" ]; then
  log "PR already merged"
  resume_merged "$PR"
  exit 0
fi

if [ "$STATE" != "OPEN" ]; then
  fail "pr-state-$STATE" $EXIT_PREFLIGHT
fi

MERGEABLE=$(jq -r .mergeable <<<"$PR_JSON")
log "mergeable: $MERGEABLE"
[ "$MERGEABLE" = "MERGEABLE" ] || fail "pr-not-mergeable-$MERGEABLE" $EXIT_PREFLIGHT

# Required-check enforcement.
say CHECKING_CHECKS
ROLLUP=$(jq -c '.statusCheckRollup' <<<"$PR_JSON")
for required in "${REQUIRED_CHECKS[@]}"; do
  conclusion=$(jq -r --arg n "$required" \
    '[.[] | select(.name == $n)] | last | (.conclusion // .status // "MISSING")' \
    <<<"$ROLLUP")
  case "$conclusion" in
    SUCCESS|NEUTRAL|SKIPPED)
      log "  ✓ $required ($conclusion)" ;;
    MISSING|null|"")
      fail "required-check-missing: $required" $EXIT_PREFLIGHT ;;
    *)
      fail "required-check-not-green: $required ($conclusion)" $EXIT_PREFLIGHT ;;
  esac
done

HEAD_SHA=$(jq -r .headRefOid <<<"$PR_JSON")
PR_VERSION=$(read_version_at_ref "$HEAD_SHA")
log "PR head version: $PR_VERSION"

# ───── no-bump path: merge only ─────────────────────────────────────

if [ "$PR_VERSION" = "$MAIN_VERSION" ]; then
  say MERGING_NO_RELEASE "#$PR"
  if [ "$DRY_RUN" = 0 ]; then
    gh -R "$REPO" pr merge "$PR" --squash --delete-branch --match-head-commit "$HEAD_SHA" \
      || fail "merge-failed" $EXIT_MERGE
  else
    log "[dry-run] would: gh pr merge $PR --squash --delete-branch --match-head-commit $HEAD_SHA"
  fi
  say SUCCESS "#$PR (no version bump, no release)"
  exit 0
fi

# ───── bump path: merge, tag, watch ────────────────────────────────

# Checked before the merge, so an unreadable or lower version never leaves a
# merged PR with a junk tag to clean up.
require_release_version "$PR_VERSION" "$MAIN_VERSION"
TAG="v$PR_VERSION"
say PLANNED_RELEASE "${MAIN_VERSION} -> ${PR_VERSION} as $TAG"

if gh api "repos/$REPO/git/ref/tags/$TAG" >/dev/null 2>&1; then
  fail "tag-already-exists: $TAG (refusing to merge into inconsistent state)" \
    $EXIT_PREFLIGHT
fi

say MERGING "#$PR"
if [ "$DRY_RUN" = 0 ]; then
  gh -R "$REPO" pr merge "$PR" --squash --delete-branch --match-head-commit "$HEAD_SHA" \
    || fail "merge-failed" $EXIT_MERGE
else
  log "[dry-run] would: gh pr merge $PR --squash --delete-branch --match-head-commit $HEAD_SHA"
fi

tag_and_watch "$PR_VERSION" "$PR"
