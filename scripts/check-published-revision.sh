#!/usr/bin/env bash
# check-published-revision.sh <image:tag> <git-sha>
#
# Run by release.yml's publish job before it pushes. A version tag on GHCR
# must only ever hold one build of one commit:
#   - <image:tag> not published yet               -> exit 0, push it
#   - published, revision label == <git-sha>      -> exit 0, a re-run of a
#     partial publish (docs/RUNBOOK.md) may re-push the same commit
#   - published from another commit, or unlabelled -> exit 1, refuse
# Any registry error other than "not found" also refuses: a transient error
# must not be read as "nothing there yet" and turn into an overwrite.
#
# DOCKER overrides the docker binary, for scripts/test-check-published-revision.sh.
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "usage: $0 <image:tag> <git-sha>" >&2
  exit 2
fi
image="$1"
revision="$2"
docker_bin="${DOCKER:-docker}"
label="org.opencontainers.image.revision"

err_file="$(mktemp)"
trap 'rm -f "$err_file"' EXIT

if ! inspected="$("$docker_bin" buildx imagetools inspect "$image" --format '{{json .Image}}' 2>"$err_file")"; then
  if grep -qiE '(^|[^[:alnum:]])not found' "$err_file"; then
    echo "OK $image is not published yet"
    exit 0
  fi
  echo "::error::could not inspect $image, refusing to push over something unknown: $(cat "$err_file")"
  exit 1
fi

# .Image is one image config, or a platform-keyed map of them for an index;
# collect the label from every config either way.
revisions="$(jq -r --arg l "$label" \
  '[.. | objects | select(has("Labels")) | (.Labels // {})[$l] // ""] | unique | .[]' \
  <<<"$inspected")"

if [ -z "$revisions" ] || grep -qx '' <<<"$revisions"; then
  echo "::error::$image is already published without a $label label, so it cannot be shown to be this commit. Release a new patch version instead."
  exit 1
fi
if [ "$revisions" != "$revision" ]; then
  echo "::error::$image is already published from commit(s) $(tr '\n' ' ' <<<"$revisions")but this run is $revision. Refusing to overwrite it; release a new patch version instead."
  exit 1
fi
echo "OK $image is already published from $revision; re-pushing the same commit"
