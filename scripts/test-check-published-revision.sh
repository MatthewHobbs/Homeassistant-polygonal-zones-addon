#!/usr/bin/env bash
# Regression check for scripts/check-published-revision.sh, the release.yml
# guard that stops a run from re-pushing <image>:<version> with different
# contents. docker is replaced by a stub (via $DOCKER), so every registry
# answer the guard has to tell apart is exercised without a registry.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="$REPO_ROOT/scripts/check-published-revision.sh"
STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR"' EXIT
fails=0

SHA=0123456789abcdef0123456789abcdef01234567
OTHER=fedcba9876543210fedcba9876543210fedcba98

# The stub answers `docker buildx imagetools inspect` the way the registry
# would for each case, chosen by STUB_MODE.
cat >"$STUB_DIR/docker" <<'STUB'
#!/usr/bin/env bash
label='"org.opencontainers.image.revision"'
case "$STUB_MODE" in
  missing)
    echo "ERROR: $4: not found" >&2; exit 1 ;;
  denied)
    echo "ERROR: failed to authorize: 403 Forbidden" >&2; exit 1 ;;
  no-credentials)
    echo "error getting credentials - err: exit status 1, out: credentials not found in native keychain" >&2; exit 1 ;;
  same)
    echo "{\"architecture\":\"amd64\",\"config\":{\"Labels\":{$label:\"$STUB_SHA\"}}}" ;;
  same-index)
    echo "{\"linux/arm64\":{\"config\":{\"Labels\":{$label:\"$STUB_SHA\"}}}}" ;;
  other)
    echo "{\"config\":{\"Labels\":{$label:\"$STUB_OTHER\"}}}" ;;
  mixed)
    echo "{\"linux/amd64\":{\"config\":{\"Labels\":{$label:\"$STUB_SHA\"}}},\"linux/arm64\":{\"config\":{\"Labels\":{$label:\"$STUB_OTHER\"}}}}" ;;
  unlabelled)
    echo '{"config":{"Labels":{"io.hass.type":"addon"}}}' ;;
  no-labels)
    echo '{"config":{}}' ;;
  *)
    echo "stub: unknown STUB_MODE '$STUB_MODE'" >&2; exit 99 ;;
esac
STUB
chmod +x "$STUB_DIR/docker"

check() {
  local name="$1" mode="$2" want_exit="$3" got_exit=0
  DOCKER="$STUB_DIR/docker" STUB_MODE="$mode" STUB_SHA="$SHA" STUB_OTHER="$OTHER" \
    "$TARGET" "ghcr.io/example/amd64-addon-polygonal_zones:1.2.3" "$SHA" >/dev/null 2>&1 \
    || got_exit=$?
  if [ "$got_exit" = "$want_exit" ]; then
    echo "ok - $name"
  else
    echo "not ok - $name (exit $got_exit, wanted $want_exit)"
    fails=$((fails + 1))
  fi
}

check "not yet published: proceed" missing 0
check "same revision (re-run of a partial publish): proceed" same 0
check "same revision behind an index: proceed" same-index 0
check "different revision: refuse" other 1
check "index whose platforms disagree: refuse" mixed 1
check "published without the revision label: refuse" unlabelled 1
check "published with no labels at all: refuse" no-labels 1
check "registry error other than not-found: refuse" denied 1
check "a 'not found' that is not the image: refuse" no-credentials 1

# No docker at all must refuse too, not read "command not found" as unpublished.
got_exit=0
DOCKER="$STUB_DIR/no-such-docker" "$TARGET" "ghcr.io/example/amd64-addon-polygonal_zones:1.2.3" "$SHA" >/dev/null 2>&1 || got_exit=$?
if [ "$got_exit" = 1 ]; then echo "ok - docker missing: refuse"; else echo "not ok - docker missing: refuse (exit $got_exit, wanted 1)"; fails=$((fails + 1)); fi

if [ "$fails" -gt 0 ]; then
  echo "FAILED: $fails check(s)" >&2
  exit 1
fi
echo "All check-published-revision.sh checks passed"
