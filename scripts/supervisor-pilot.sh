#!/usr/bin/env bash
# Install, configure and probe the add-on under a real stable-channel
# Supervisor and Core, inside the official add-on devcontainer (ADR 0001).
#
# One script for CI (.github/workflows/supervisor.yml) and for a local run:
#
#   scripts/supervisor-pilot.sh all          # every phase, then tear down
#   CORE_VERSION=2026.7.1 scripts/supervisor-pilot.sh all
#
# Phases can also be run one at a time against a running devcontainer
# (build, up, versions, sideload, install, provenance, probe,
# diagnostics, down). Needs docker with privileged containers, git and jq.
#
# Environment:
#   CORE_VERSION   "stable" (default) or an exact Core version to pin.
#   PILOT_NAME     devcontainer name, and prefix of its volumes (pz-supervisor).
#   PILOT_WORKDIR  scratch for the image tar and state (a mktemp dir). Set it
#                  when running phases one at a time, so they share state.
#   PILOT_KEEP=1   `all` leaves the devcontainer running for inspection.
#   PILOT_DISABLE_APPARMOR=1  install the add-on with apparmor: false.
#   PILOT_IMAGE_SOURCE  "checkout" (default) side-loads the image built here;
#                  "published" leaves config.yaml's ghcr image alone, which is
#                  only useful to show the provenance check failing.
#   PILOT_EXPECT_SUPERVISOR / PILOT_EXPECT_CORE  override what stable.json
#                  says; only for showing the version check can fail.

set -euo pipefail

# Renovate keeps these current (customManagers in .github/renovate.json5),
# matching the quoted "name:tag@digest"; the tag must exist upstream.
DEVCONTAINER_IMAGE="ghcr.io/home-assistant/devcontainer:6-apps@sha256:4e2d6efd9ac472c27f5cc522672ea9bbfdf35a897266ff4b1ac1f21ee611a4a9"
PLAYWRIGHT_IMAGE="mcr.microsoft.com/playwright/python:v1.63.0-noble@sha256:72bd171a9ffc2b4b59532aaa6210e21014d07093120dc25528870c0b840da1f0"
REGISTRY_IMAGE="docker.io/library/registry:3@sha256:852b3e4d378c426dda6b318fe9d9bfe8e92a0eccb9926671ec3d3ea17a196696"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ADDON_DIR="polygonal_zones_editor"
SLUG="local_polygonal_zones"
ADDON_CONTAINER="app_${SLUG}"
NAME="${PILOT_NAME:-pz-supervisor}"
CORE_VERSION="${CORE_VERSION:-stable}"
IMAGE_SOURCE="${PILOT_IMAGE_SOURCE:-checkout}"
WORKDIR="${PILOT_WORKDIR:-}"
if [[ -z "$WORKDIR" ]]; then
  WORKDIR="$(mktemp -d)"
fi
mkdir -p "$WORKDIR"
# The registry lives on the devcontainer's loopback: Docker allows plain HTTP
# to 127.0.0.1 without any daemon configuration, and nothing leaves the job.
LOCAL_REGISTRY="127.0.0.1:5000"
REGISTRY_TAG="pz-pilot-registry:local"
# A distinctive colour, so /config.json can only report it if the option set
# through the Supervisor reached the app.
PROBE_COLOUR="#1a2b3c"
PROVENANCE_LABEL="io.github.matthewhobbs.polygonal-zones.pilot-build"

log() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*"; }
fail() {
  printf '%s FAIL %s\n' "$(date -u +%H:%M:%S)" "$*" >&2
  exit 1
}

dc() { docker exec "$NAME" "$@"; }
ha_cli() { docker exec "$NAME" ha "$@"; }

# `ha --raw-json` exits 0 even when the result is an error, so only the
# result field can say whether the call worked.
ha_ok() {
  local out
  out="$(ha_cli "$@" --raw-json)" || true
  echo "$out"
  jq -e '.result == "ok"' <<<"$out" >/dev/null
}

# POST to the Supervisor REST API from inside hassio_cli, the one container
# that already holds a SUPERVISOR_TOKEN. The body goes in on stdin, never
# into the command string.
supervisor_post() {
  local path="$1" body="$2"
  printf '%s' "$body" | docker exec -i "$NAME" docker exec -i hassio_cli sh -c "
    curl -sS --fail-with-body --max-time 60 -X POST 'http://supervisor$path' \
      -H \"Authorization: Bearer \$SUPERVISOR_TOKEN\" \
      -H 'Content-Type: application/json' --data-binary @-"
}

# poll DESCRIPTION TIMEOUT_S INTERVAL_S COMMAND... (the exit code decides)
poll() {
  local description="$1" timeout_s="$2" interval_s="$3" start
  shift 3
  start=$SECONDS
  until "$@" >/dev/null 2>&1; do
    if ((SECONDS - start >= timeout_s)); then
      log "TIMEOUT after ${timeout_s}s: $description" >&2
      return 1
    fi
    sleep "$interval_s"
  done
  log "OK (${description}, $((SECONDS - start))s)"
}

docker_arch() {
  case "$(docker version --format '{{.Server.Arch}}')" in
    amd64 | x86_64) echo amd64 ;;
    arm64 | aarch64) echo aarch64 ;;
    *) fail "unsupported docker server arch" ;;
  esac
}

addon_version() {
  sed -n 's/^version: *"\{0,1\}\([^"]*\)"\{0,1\} *$/\1/p' "$REPO_ROOT/$ADDON_DIR/config.yaml"
}

# The revision under test: HEAD, marked dirty if the add-on has local edits,
# so an uncommitted change can never pass as the committed one.
checkout_revision() {
  local rev
  rev="$(git -C "$REPO_ROOT" rev-parse HEAD)"
  if [[ -n "$(git -C "$REPO_ROOT" status --porcelain -- "$ADDON_DIR")" ]]; then
    rev="${rev}-dirty"
  fi
  echo "$rev"
}

# --- build: the image that would ship, built the way release.yml builds it --
cmd_build() {
  local arch platform base version tag nonce
  arch="$(docker_arch)"
  platform="linux/$([[ $arch == aarch64 ]] && echo arm64 || echo amd64)"
  base="$(awk -v a="${arch}:" '$1 == a { print $2 }' "$REPO_ROOT/.github/base-images.yaml")"
  [[ "$base" == *@sha256:* ]] || fail "no digest-pinned base for $arch in .github/base-images.yaml"
  version="$(addon_version)"
  [[ -n "$version" ]] || fail "could not read version: from config.yaml"
  tag="${LOCAL_REGISTRY}/${arch}-addon-polygonal_zones:${version}"
  # Revision plus a per-build nonce: a published image built from this very
  # commit still cannot carry it.
  nonce="${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}-$(date +%s)-$RANDOM"
  local provenance
  provenance="$(checkout_revision)/${nonce}"

  log "Building $tag for $platform from $base"
  docker buildx build \
    --platform "$platform" \
    --build-arg "BUILD_FROM=$base" \
    --label "${PROVENANCE_LABEL}=${provenance}" \
    --tag "$tag" \
    --load \
    "$REPO_ROOT/$ADDON_DIR"

  docker pull --quiet "$REGISTRY_IMAGE" >/dev/null
  docker tag "$REGISTRY_IMAGE" "$REGISTRY_TAG"
  docker save "$tag" "$REGISTRY_TAG" -o "$WORKDIR/images.tar"
  {
    echo "PILOT_TAG=$tag"
    echo "PILOT_PROVENANCE=$provenance"
  } >"$WORKDIR/build.env"
  log "OK built $tag, provenance label ${provenance}"
}

# --- up: devcontainer, add-on copy, Supervisor, Core -------------------------
cmd_up() {
  if docker container inspect "$NAME" >/dev/null 2>&1; then
    fail "container $NAME already exists; run '$0 down' first"
  fi
  log "Starting devcontainer $DEVCONTAINER_IMAGE"
  # SUPERVISOR_CHANNEL explicitly: the devcontainer defaults to dev and its
  # template to beta. --privileged: it runs systemd and its own dockerd.
  docker run -d --name "$NAME" --privileged \
    -e SUPERVISOR_CHANNEL=stable \
    -v "${NAME}-docker:/var/lib/docker" \
    -v "${NAME}-containerd:/var/lib/containerd" \
    -v "${NAME}-mnt:/mnt/supervisor" \
    --tmpfs /tmp \
    "$DEVCONTAINER_IMAGE" >/dev/null

  # supervisor_run has no readiness check of its own and dies under set -e
  # if dockerd is not up yet.
  poll "inner Docker daemon ready" 120 2 dc docker info

  cmd_copy_addon

  dc bash -c 'curl -sS --fail --max-time 30 https://version.home-assistant.io/stable.json' \
    >"$WORKDIR/stable.json" || fail "could not fetch stable.json"
  log "stable.json: $(jq -c '{supervisor, homeassistant}' "$WORKDIR/stable.json")"
  seed_core_version

  log "Starting Supervisor (channel stable)"
  docker exec -d "$NAME" bash -c 'supervisor_run > /var/log/supervisor_run.log 2>&1'
  poll "hassio_supervisor container running" 180 3 \
    bash -c "docker exec '$NAME' docker inspect -f '{{.State.Running}}' hassio_supervisor | grep -qx true"
  cmd_wait_core
}

# Where Core really answers. 2026.9.3 under the Supervisor serves port 80 and
# 307s 8123 there; 2026.7.1 serves only 8123 (both observed).
core_url() {
  local url
  for url in http://127.0.0.1:8123 http://127.0.0.1; do
    if dc curl -sf "$url/api/onboarding" | jq -e 'type == "array"' >/dev/null 2>&1; then
      echo "$url"
      return
    fi
  done
  return 1
}

core_ready() {
  ha_cli core info --raw-json | jq -e '.data.version != "landingpage"' && core_url
}

# The landing page that precedes Core answers /api/onboarding with a 302,
# which curl -f counts as success, so readiness needs Core's JSON step list
# and a Core version that is not "landingpage".
cmd_wait_core() {
  poll "Home Assistant Core (not the landing page) answering" 1200 5 core_ready
}

# Only git-tracked files, as a user's clone has them, into apps/local.
cmd_copy_addon() {
  local dest="/mnt/supervisor/apps/local"
  dc mkdir -p "$dest"
  git -C "$REPO_ROOT" ls-files -z -- "$ADDON_DIR" |
    (cd "$REPO_ROOT" && tar --null -T - -cf -) |
    docker exec -i "$NAME" tar -xf - -C "$dest"
  if [[ "$IMAGE_SOURCE" == checkout ]]; then
    # Supervisor always pulls when config.yaml names an image, and never
    # uses a local one (docker/interface.py install -> pull_image), so the
    # checkout's image is served from a registry and named here. Only the
    # copy is changed; {arch} stays so the Supervisor still resolves it.
    dc sed -i "s|^image: .*|image: \"${LOCAL_REGISTRY}/{arch}-addon-polygonal_zones\"|" \
      "$dest/$ADDON_DIR/config.yaml"
  fi
  if [[ "${PILOT_DISABLE_APPARMOR:-0}" == 1 ]]; then
    dc bash -c "printf '\napparmor: false\n' >> '$dest/$ADDON_DIR/config.yaml'"
    log "WARNING AppArmor disabled for the add-on copy (PILOT_DISABLE_APPARMOR=1)"
  fi
  log "Add-on copy: $(dc grep -E '^(version|image|apparmor):' "$dest/$ADDON_DIR/config.yaml" | tr '\n' ' ')"
}

# --- versions: running Supervisor/Core against stable.json (ADR row 2) -------
cmd_versions() {
  local want_sup want_core machine got_sup got_core channel sup_img core_img rc=0
  [[ -s "$WORKDIR/stable.json" ]] || fail "no stable.json in $WORKDIR (run 'up' first)"
  machine="$(ha_cli info --raw-json | jq -er .data.machine)" || fail "could not read machine"
  want_sup="${PILOT_EXPECT_SUPERVISOR:-$(jq -er .supervisor "$WORKDIR/stable.json")}" ||
    fail "stable.json has no supervisor version"
  if [[ "$CORE_VERSION" == stable ]]; then
    want_core="${PILOT_EXPECT_CORE:-$(jq -er --arg m "$machine" '.homeassistant[$m] // .homeassistant.default' "$WORKDIR/stable.json")}" ||
      fail "stable.json has no Core version for $machine"
  else
    want_core="${PILOT_EXPECT_CORE:-$CORE_VERSION}"
  fi
  got_sup="$(ha_cli supervisor info --raw-json | jq -er .data.version)" || fail "could not read Supervisor version"
  channel="$(ha_cli supervisor info --raw-json | jq -er .data.channel)" || fail "could not read channel"
  got_core="$(ha_cli core info --raw-json | jq -er .data.version)" || fail "could not read Core version"
  # The API and the image actually running are checked separately.
  sup_img="$(dc docker inspect -f '{{.Config.Image}}' hassio_supervisor)"
  core_img="$(dc docker inspect -f '{{.Config.Image}}' homeassistant)"

  log "machine=$machine channel=$channel core_leg=$CORE_VERSION"
  log "Supervisor expected=$want_sup observed=$got_sup image=$sup_img"
  log "Core       expected=$want_core observed=$got_core image=$core_img"
  [[ "$channel" == stable ]] || { log "FAIL channel is $channel, not stable"; rc=1; }
  [[ "$got_sup" == "$want_sup" && "$sup_img" == *":$want_sup" ]] ||
    { log "FAIL Supervisor does not match $want_sup"; rc=1; }
  [[ "$got_core" == "$want_core" && "$core_img" == *":$want_core" ]] ||
    { log "FAIL Core does not match $want_core"; rc=1; }
  ((rc == 0)) || fail "version check"
  log "OK versions match"
}

# --- the floor leg (ADR row 11) ----------------------------------------------
# `ha core update --version` cannot pin a floor: it starts the older Core on
# .storage the newer one already wrote, which it refuses
# (UnsupportedStorageVersionError), and the Supervisor rolls back to stable
# while the CLI still reports ok. Instead the Supervisor is told the version
# before its first start: with no Core container but a stored version, it
# reinstalls that version directly (homeassistant/core.py load/_reinstall),
# so the newer Core never runs. Relies on homeassistant.json's layout, which
# is internal; the version check afterwards is what catches it changing.
seed_core_version() {
  [[ "$CORE_VERSION" == stable ]] && return
  log "Seeding Core $CORE_VERSION before the Supervisor's first start"
  jq -n --arg v "$CORE_VERSION" '{version: $v}' |
    docker exec -i "$NAME" tee /mnt/supervisor/homeassistant.json >/dev/null
}

# --- sideload: serve the checkout's image to the inner daemon ----------------
cmd_sideload() {
  [[ "$IMAGE_SOURCE" == checkout ]] || { log "image source $IMAGE_SOURCE: not side-loading"; return; }
  # shellcheck source=/dev/null
  source "$WORKDIR/build.env"
  # /var/tmp: /tmp is a tmpfs that docker cp does not reach.
  docker cp "$WORKDIR/images.tar" "$NAME:/var/tmp/images.tar"
  dc docker load -i /var/tmp/images.tar
  dc rm -f /var/tmp/images.tar
  # After supervisor_run, which removes every container on the inner daemon.
  if ! dc docker inspect pz-pilot-registry >/dev/null 2>&1; then
    dc docker run -d --restart=always --name pz-pilot-registry \
      -p "${LOCAL_REGISTRY}:5000" "$REGISTRY_TAG" >/dev/null
  fi
  poll "loopback registry ready" 60 2 dc curl -sf "http://${LOCAL_REGISTRY}/v2/"
  dc docker push --quiet "$PILOT_TAG"
  # Only the registry may supply it, so drop the loaded copy.
  dc docker rmi "$PILOT_TAG" >/dev/null
  log "OK $PILOT_TAG served from the devcontainer's loopback registry"
}

# --- install: discover, install, configure, start ----------------------------
discover_addon() {
  ha_cli store reload >/dev/null 2>&1 || true
  # The API still says "addons" (Supervisor 2026.09.2); allow for the rename.
  ha_cli store apps --raw-json |
    jq -e --arg s "$SLUG" '(.data.apps // .data.addons)[] | select(.slug == $s)'
}

cmd_install() {
  # Local add-on discovery is known to be fragile (supervisor#3976).
  poll "$SLUG discovered in the local store" 180 5 discover_addon
  log "Installing $SLUG"
  ha_ok store apps install "$SLUG" || fail "install of $SLUG failed"
  log "Setting options through the Supervisor API"
  # The Supervisor validates the whole set, so change two keys of the current one.
  local options
  options="$(ha_cli apps info "$SLUG" --raw-json |
    jq -ce --arg c "$PROBE_COLOUR" '{options: (.data.options + {zone_colour: $c, log_level: "debug"})}')" ||
    fail "could not read the add-on's current options"
  supervisor_post "/addons/$SLUG/options" "$options" || fail "Supervisor rejected the options"
  log "Starting $SLUG"
  ha_ok apps start "$SLUG" || fail "start of $SLUG failed"
  poll "Supervisor reports $SLUG started" 180 3 \
    bash -c "docker exec '$NAME' ha apps info '$SLUG' --raw-json | jq -e '.data.state == \"started\"'"
  ha_cli apps info "$SLUG" --raw-json | jq -c '.data | {version, state, ingress, ingress_url, options}'
}

# --- provenance: the running container came from this checkout (ADR row 3) ---
cmd_provenance() {
  local got image want_rev
  [[ -s "$WORKDIR/build.env" ]] || fail "no build.env in $WORKDIR (run 'build' first)"
  # shellcheck source=/dev/null
  source "$WORKDIR/build.env"
  want_rev="$(checkout_revision)"
  got="$(dc docker inspect -f "{{index .Config.Labels \"$PROVENANCE_LABEL\"}}" "$ADDON_CONTAINER")" ||
    fail "no running container $ADDON_CONTAINER"
  image="$(dc docker inspect -f '{{.Config.Image}}' "$ADDON_CONTAINER")"
  log "Add-on container image=$image"
  log "Provenance expected=$PILOT_PROVENANCE observed=${got:-<none>} checkout=$want_rev"
  [[ "$got" == "$PILOT_PROVENANCE" ]] ||
    fail "the running add-on was not built by this run from the checkout under test"
  [[ "${got%%/*}" == "$want_rev" ]] || fail "the build's revision is not the checkout's ($want_rev)"
  [[ "$image" == "$PILOT_TAG" ]] || fail "the add-on runs $image, not $PILOT_TAG"
  log "OK the running add-on is this run's build of $want_rev"
}

# --- probe: smoke probes and Playwright through Core's ingress (ADR row 4) ----
cmd_probe() {
  local ingress pid uid
  ingress="$(ha_cli apps info "$SLUG" --raw-json | jq -er .data.ingress_url)" || fail "no ingress_url"

  # The same guard as build.yml's smoke, on the Supervisor-run container:
  # exactly uid 1001, and that uid must be `app`.
  pid="$(dc docker exec "$ADDON_CONTAINER" pgrep -f 'python main.py' | head -n1 || true)"
  [[ -n "$pid" ]] || fail "could not find python main.py in $ADDON_CONTAINER"
  # shellcheck disable=SC2016 # $2 is awk's, not the shell's
  uid="$(dc docker exec "$ADDON_CONTAINER" awk '/^Uid:/{print $2}' "/proc/$pid/status")"
  # shellcheck disable=SC2016 # $3 and $1 are awk's
  user="$(dc docker exec "$ADDON_CONTAINER" awk -F: -v u="$uid" '$3 == u {print $1}' /etc/passwd)"
  [[ "$uid" == 1001 && "$user" == app ]] ||
    fail "web service runs as uid=${uid:-?}(${user:-?}) under the Supervisor, expected uid=1001(app)"
  log "OK web service runs as uid=$uid($user) under the Supervisor"

  local base
  base="$(core_url)" || fail "Core is not answering on 8123 or 80"
  log "Probing through Core ingress at $base$ingress"
  # Shares the devcontainer's network namespace, so Core is on its loopback
  # here and in CI alike, with no published ports.
  docker run --rm --network "container:$NAME" \
    -v "$REPO_ROOT/scripts/supervisor_probe.py:/probe/probe.py:ro" \
    -v "$REPO_ROOT/scripts/supervisor-probe-requirements.txt:/probe/requirements.txt:ro" \
    "$PLAYWRIGHT_IMAGE" \
    bash -c 'pip install --quiet --no-cache-dir --break-system-packages --require-hashes \
        -r /probe/requirements.txt && exec python3 /probe/probe.py "$@"' probe \
    --base "$base" --ingress "$ingress" --expect-colour "$PROBE_COLOUR"
}

cmd_diagnostics() {
  local out="${1:-$WORKDIR/diagnostics}"
  mkdir -p "$out"
  dc cat /var/log/supervisor_run.log >"$out/supervisor_run.log" 2>&1 || true
  dc docker ps -a >"$out/inner-docker-ps.txt" 2>&1 || true
  ha_cli supervisor logs >"$out/supervisor.log" 2>&1 || true
  ha_cli core logs >"$out/core.log" 2>&1 || true
  ha_cli apps logs "$SLUG" >"$out/addon.log" 2>&1 || true
  ha_cli resolution info --raw-json >"$out/resolution.json" 2>&1 || true
  log "Diagnostics in $out"
}

cmd_down() {
  docker rm -f -v "$NAME" >/dev/null 2>&1 || true
  docker volume rm "${NAME}-docker" "${NAME}-containerd" "${NAME}-mnt" >/dev/null 2>&1 || true
  docker image rm "$REGISTRY_TAG" >/dev/null 2>&1 || true
  log "Removed $NAME and its volumes"
}

cmd_all() {
  local t0=$SECONDS t phase timings=""
  export PILOT_WORKDIR="$WORKDIR"
  [[ "${PILOT_KEEP:-0}" == 1 ]] || trap 'cmd_down' EXIT
  # Each phase in its own process: a function called on the left of || runs
  # with set -e disabled, which would let a failed step pass silently.
  for phase in build up versions sideload install provenance probe; do
    t=$SECONDS
    log "=== $phase"
    if ! bash "${BASH_SOURCE[0]}" "$phase"; then
      bash "${BASH_SOURCE[0]}" diagnostics || true
      fail "phase $phase (timings so far: ${timings})"
    fi
    timings+="$phase=$((SECONDS - t))s "
  done
  log "Timings: ${timings}total=$((SECONDS - t0))s"
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    echo "Supervisor pilot ($(docker_arch), Core leg ${CORE_VERSION}): ${timings}total=$((SECONDS - t0))s" >>"$GITHUB_STEP_SUMMARY"
  fi
}

case "${1:-}" in
  build | up | wait-core | versions | sideload | install | provenance | probe | diagnostics | down | all | copy-addon)
    cmd="${1//-/_}"
    shift
    "cmd_$cmd" "$@"
    ;;
  *)
    echo "usage: $0 {all|build|up|wait-core|versions|sideload|install|provenance|probe|diagnostics|down}" >&2
    exit 2
    ;;
esac
