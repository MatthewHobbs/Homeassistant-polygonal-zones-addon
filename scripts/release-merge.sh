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
#   3  pre-flight failure (PR state, checks, mergeability, version mismatch)
#   4  merge failed
#   5  tag creation failed
#   6  release workflow failed
#
# stdout uses STATUS:<TOKEN> lines so wrappers (e.g. the Claude Code skill)
# can summarise progress without screen-scraping freeform text.

set -euo pipefail

REPO="MatthewHobbs/Homeassistant-polygonal-zones-addon"
CONFIG="polygonal_zones_editor/config.yaml"

# Required CI checks. If any of these is missing from the PR's status rollup,
# or hasn't reached SUCCESS, the script refuses to merge. Update this list
# when adding/removing CI jobs.
REQUIRED_CHECKS=(
  "Build (amd64)"
  "Build (aarch64)"
  "Build (armhf)"
  "Build (armv7)"
  "Build (i386)"
  "lint"
  "pytest"
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

# Wait for main HEAD to differ from a known prior SHA (handles replication
# lag without a fixed sleep). Echoes the new SHA on success.
wait_for_main_head_change() {
  local prior="$1"
  local now
  for _ in $(seq 1 15); do
    now=$(gh api "repos/$REPO/commits/main" --jq .sha)
    if [ "$now" != "$prior" ]; then
      echo "$now"
      return 0
    fi
    sleep 2
  done
  return 1
}

# Wait for main's config.yaml version to equal $1.
wait_for_main_version() {
  local expected="$1" seen
  for _ in $(seq 1 15); do
    seen=$(read_version_at_ref main)
    [ "$seen" = "$expected" ] && return 0
    sleep 2
  done
  fail "post-merge-version-mismatch: main=$seen expected=$expected" $EXIT_PREFLIGHT
}

# Tag a SHA, find the resulting release run, and watch it.
tag_and_watch() {
  local version="$1" sha="$2" tag="v$1"

  wait_for_main_version "$version"

  say TAGGING "$tag at $sha"
  if [ "$DRY_RUN" = 0 ]; then
    if ! gh api -X POST "repos/$REPO/git/refs" \
        -f ref="refs/tags/$tag" -f sha="$sha" >/dev/null; then
      fail "tag-push-failed: $tag" $EXIT_TAG
    fi
  else
    log "[dry-run] would create tag $tag at $sha"
  fi

  watch_for_tag "$tag"
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

# ───── auto-resume: PR already merged ──────────────────────────────

if [ "$STATE" = "MERGED" ]; then
  TAG="v$MAIN_VERSION"
  log "PR already merged; main is at $MAIN_VERSION"
  if gh api "repos/$REPO/git/ref/tags/$TAG" >/dev/null 2>&1; then
    say RESUMING "$TAG (tag already exists)"
    watch_for_tag "$TAG"
    exit 0
  fi
  log "tag $TAG missing; finishing release"
  MERGE_SHA=$(gh api "repos/$REPO/commits/main" --jq .sha)
  tag_and_watch "$MAIN_VERSION" "$MERGE_SHA"
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
    gh -R "$REPO" pr merge "$PR" --squash --delete-branch \
      || fail "merge-failed" $EXIT_MERGE
  else
    log "[dry-run] would: gh pr merge $PR --squash --delete-branch"
  fi
  say SUCCESS "#$PR (no version bump, no release)"
  exit 0
fi

# ───── bump path: merge, tag, watch ────────────────────────────────

TAG="v$PR_VERSION"
say PLANNED_RELEASE "${MAIN_VERSION} -> ${PR_VERSION} as $TAG"

if gh api "repos/$REPO/git/ref/tags/$TAG" >/dev/null 2>&1; then
  fail "tag-already-exists: $TAG (refusing to merge into inconsistent state)" \
    $EXIT_PREFLIGHT
fi

PRE_MERGE_HEAD=$(gh api "repos/$REPO/commits/main" --jq .sha)

say MERGING "#$PR"
if [ "$DRY_RUN" = 0 ]; then
  gh -R "$REPO" pr merge "$PR" --squash --delete-branch \
    || fail "merge-failed" $EXIT_MERGE
  MERGE_SHA=$(wait_for_main_head_change "$PRE_MERGE_HEAD") \
    || fail "post-merge-head-propagation-timeout" $EXIT_PREFLIGHT
else
  log "[dry-run] would: gh pr merge $PR --squash --delete-branch"
  MERGE_SHA="<dry-run-merge-sha>"
fi

tag_and_watch "$PR_VERSION" "$MERGE_SHA"
