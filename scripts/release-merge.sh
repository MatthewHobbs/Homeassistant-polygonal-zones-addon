#!/usr/bin/env bash
# release-merge.sh — squash-merge a PR and, if it bumped the addon version,
# immediately tag the merge commit so the release workflow publishes images
# before HA users see the update.
#
# Usage:
#   scripts/release-merge.sh <pr-number>
#
# Behaviour:
#   1. Verifies the PR is OPEN and all required checks are green.
#   2. Reads the version from polygonal_zones_editor/config.yaml on both the
#      PR head and main.
#   3. Squash-merges the PR (and deletes the branch).
#   4. If the PR bumped the version: creates a vX.Y.Z tag at the merge
#      commit and pushes it. The push triggers .github/workflows/release.yml,
#      which we then watch to completion.
#   5. If the PR didn't bump the version: stops after the merge.
#
# Requires: gh, jq, awk. Re-run safe: refuses to retag if vX.Y.Z exists.

set -euo pipefail

if [ "${1:-}" = "" ]; then
  echo "usage: $0 <pr-number>" >&2
  exit 2
fi

PR="$1"
REPO="MatthewHobbs/Homeassistant-polygonal-zones-addon"
CONFIG="polygonal_zones_editor/config.yaml"

read_version_at_ref() {
  # Print the version: "X.Y.Z" value from config.yaml at the given ref.
  gh -R "$REPO" api "repos/$REPO/contents/$CONFIG?ref=$1" --jq .content \
    | base64 -d \
    | awk -F'"' '/^version:/{print $2; exit}'
}

echo "→ Inspecting PR #$PR"
PR_JSON=$(gh -R "$REPO" pr view "$PR" --json state,headRefOid,statusCheckRollup,mergeable,title)
STATE=$(jq -r .state <<<"$PR_JSON")
[ "$STATE" = "OPEN" ] || { echo "PR #$PR is $STATE, not OPEN" >&2; exit 1; }

# Treat NEUTRAL/SKIPPED/null as fine; only SUCCESS-or-not is what matters for blockers.
FAILED=$(jq -r '.statusCheckRollup[]
  | select((.conclusion // .status) as $c | $c != "SUCCESS" and $c != "NEUTRAL" and $c != "SKIPPED" and $c != null)
  | .name + " (" + ((.conclusion // .status) | tostring) + ")"' <<<"$PR_JSON")
if [ -n "$FAILED" ]; then
  echo "Some checks aren't green:" >&2
  echo "$FAILED" >&2
  exit 1
fi

HEAD_SHA=$(jq -r .headRefOid <<<"$PR_JSON")
PR_VERSION=$(read_version_at_ref "$HEAD_SHA")
MAIN_VERSION=$(read_version_at_ref main)
echo "→ PR head version: $PR_VERSION"
echo "→ main    version: $MAIN_VERSION"

if [ "$PR_VERSION" = "$MAIN_VERSION" ]; then
  echo "→ No version bump; squash-merging only."
  gh -R "$REPO" pr merge "$PR" --squash --delete-branch
  echo "✓ Merged PR #$PR. No release."
  exit 0
fi

TAG="v$PR_VERSION"
if gh -R "$REPO" api "repos/$REPO/git/ref/tags/$TAG" >/dev/null 2>&1; then
  echo "Tag $TAG already exists; aborting before merge to avoid an inconsistent state." >&2
  exit 1
fi

echo "→ Squash-merging PR #$PR"
gh -R "$REPO" pr merge "$PR" --squash --delete-branch

# Allow the merge to propagate, then grab the merge commit on main.
sleep 3
MERGE_SHA=$(gh -R "$REPO" api "repos/$REPO/commits/main" --jq .sha)

# Sanity: main now has the bumped version.
POST_MERGE_VERSION=$(read_version_at_ref main)
if [ "$POST_MERGE_VERSION" != "$PR_VERSION" ]; then
  echo "main is at version $POST_MERGE_VERSION after merge, expected $PR_VERSION" >&2
  echo "Tag NOT created. Resolve manually." >&2
  exit 1
fi

echo "→ Creating tag $TAG at $MERGE_SHA"
gh -R "$REPO" api -X POST "repos/$REPO/git/refs" \
  -f ref="refs/tags/$TAG" \
  -f sha="$MERGE_SHA" >/dev/null

echo "→ Tag pushed; waiting for release workflow to start"
RUN_ID=""
for _ in $(seq 1 24); do
  RUN_ID=$(gh -R "$REPO" run list --workflow=release.yml --branch="$TAG" --limit=1 --json databaseId --jq '.[0].databaseId // empty')
  [ -n "$RUN_ID" ] && break
  sleep 5
done
if [ -z "$RUN_ID" ]; then
  echo "Release workflow run for $TAG didn't appear within 2 minutes." >&2
  echo "Check Actions UI manually." >&2
  exit 1
fi

echo "→ Watching release run $RUN_ID (this can take a few minutes)"
gh -R "$REPO" run watch "$RUN_ID" --exit-status --interval 10

echo "✓ $TAG released. Images on GHCR; HA users can now update to $PR_VERSION."
