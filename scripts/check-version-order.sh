#!/usr/bin/env bash
# Refuse to release X.Y.Z ($1) unless it is above every other released tag.
# Tags come on stdin, one per line (`git tag -l 'v*'`). The same version is
# skipped, so re-running a release for its own tag still passes.
set -euo pipefail
version="$1"
re='^[0-9]+\.[0-9]+\.[0-9]+$'
[[ "$version" =~ $re ]] || { echo "::error::'$version' is not X.Y.Z"; exit 1; }

# 0 if $1 > $2, both X.Y.Z; compared as numbers, so 0.5.10 is above 0.5.9.
above() {
  local a b i
  IFS=. read -ra a <<<"$1"
  IFS=. read -ra b <<<"$2"
  for i in 0 1 2; do
    ((10#${a[i]} > 10#${b[i]})) && return 0
    ((10#${a[i]} < 10#${b[i]})) && return 1
  done
  return 1
}

while read -r tag; do
  released="${tag#v}"
  [[ "$released" =~ $re ]] && [ "$released" != "$version" ] || continue
  if ! above "$version" "$released"; then
    echo "::error::v$version is not above the released $tag"
    exit 1
  fi
done
