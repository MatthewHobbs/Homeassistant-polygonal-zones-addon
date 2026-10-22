# Testing

This repo has four layers of automated testing, plus a manual live-HA-OS layer for anything CI can't cover.

## Automated (CI)

| Layer | What it runs | Where |
|---|---|---|
| `Tests` | `pytest -v` (100% line and branch coverage, gated by `--cov-fail-under=100`), the shell script tests in `scripts/`, and the node tests in `polygonal_zones_editor/tests/` | [`.github/workflows/test.yml`](.github/workflows/test.yml) |
| `Lint addon` | `frenck/action-addon-linter`, an AppArmor profile compile (`apparmor_parser -Q -K`), `ruff`, a non-required check that the declared HA floor is within its bound, and `shellcheck` of `scripts/` and `rootfs/` shell files | [`.github/workflows/lint.yml`](.github/workflows/lint.yml) |
| `Build addon` | Multi-arch Dockerfile build (per `polygonal_zones_editor/build.yaml`), each arch built and booted natively (amd64 on `ubuntu-latest`, aarch64 on `ubuntu-24.04-arm`, no QEMU), plus a Playwright headless page load on both arches (`scripts/ui_smoke.py`) | [`.github/workflows/build.yml`](.github/workflows/build.yml) |
| `Supervisor pilot` (every PR, nightly, on demand; required) | Installs, configures and probes the add-on under a real stable-channel Supervisor and Core, in the official add-on devcontainer | [`.github/workflows/supervisor.yml`](.github/workflows/supervisor.yml) |

The smoke step in `build.yml` boots the container on each arch's native runner with a stub `options.json` mount and probes:
- `/healthz` returning `ok`
- `/zones.json` without `X-Save-Token` returning `403`
- `/zones.json` returning a valid GeoJSON `FeatureCollection`
- `POST /save_zones` round-trips `SMOKE_ZONES` (a Polygon and a two-part MultiPolygon, from `scripts/draw_and_save.py`)
- the server gave every saved zone a unique id
- the web service (the `python main.py` process) runs as uid 1001 (`app`), not root
- nothing under `/app` is owned by, or writable by, the web user
- no Python traceback in the container log
- the page loads clean in headless Chromium with no JS errors and renders at least one `<zone-entry>`, drawn and saved through the real Geoman toolbar on both a secure and a non-secure origin

`Tests`, `Lint`, and `Build` gate the release pipeline: a tag push to `v*` can't publish images until all three are green on the tagged SHA. `Supervisor pilot` gates the merge instead: it is a required PR check, and the tag is cut from the merged PR's content.

## Supervisor pilot

`.github/workflows/supervisor.yml` runs `scripts/supervisor-pilot.sh` in `ghcr.io/home-assistant/devcontainer` on `ubuntu-latest` (amd64) and natively on `ubuntu-24.04-arm` (aarch64). Each arch runs two legs: Core pinned to whatever `stable.json` currently names, and Core pinned to the floor declared in `config.yaml`'s `homeassistant:` (read via `scripts/ha-floor-check.sh --declared`). The pilot installs the add-on the Supervisor way (from `apps/local`, built from the checkout under test, not the published image), sets its options through the Supervisor API, and probes it through ingress. It enforces AppArmor, on a newer parser and kernel than HA OS ships.

Every PR, every Friday and every manual run uses the full matrix (both arches, both legs). The other nightly runs cover amd64 against stable Core only. The `Supervisor pilot` job passes only when every leg passes.

## Manual: live HA OS

The Supervisor pilot covers install, setting a *valid* option through the Supervisor API, ingress, AppArmor enforcement, and the watchdog restarting the add-on when its web process dies. It runs on a devcontainer, not HA OS itself, and only the happy path. Some things still only show up under a real HA OS install, or need a case the pilot doesn't probe:

- `config.yaml` schema *rejection* (e.g. `zone_colour: match(...)` refusing `""` or `rgb(...)`): the pilot only round-trips a valid value.
- Backing up and restoring the add-on through the Supervisor.
- Provenance of the published images. `config.yaml` leaves `codenotary` unset (CAS is discontinued); the images carry Sigstore
  build-provenance attestations, which the Supervisor does not verify on install. Verify them yourself after a release:
  `gh attestation verify oci://ghcr.io/matthewhobbs/<arch>-addon-polygonal_zones:<version> --owner MatthewHobbs --source-ref refs/tags/v<version> --signer-workflow MatthewHobbs/Homeassistant-polygonal-zones-addon/.github/workflows/release.yml` (the source ref and workflow pin the attestation to the tag's own release run, not a dry run).
- HA OS's actual kernel and AppArmor parser versions, which differ from the devcontainer's.
- Real hardware (RPi, HA Yellow/Green) rather than a GitHub Actions runner.

**Do these before merging any PR that touches `Dockerfile`, `rootfs/`, `config.yaml`, `build.yaml`, or `apparmor.txt`:**

### Setup

- [ ] Install the branch as an addon via the **Add Repository** flow pointing at your fork / branch URL.
- [ ] Confirm the addon starts cleanly: **Settings → Add-ons → Polygonal Zones → Log**, no tracebacks.
- [ ] The startup `Loaded options:` line shows `save_token: ***` if a token is set and `save_token: not set` if not. `overlay_entities` and `trusted_proxies` show a count of entries, not the entries.

### Core flows

- [ ] Open the Web UI via the **Open Web UI** button. Map tiles render (OSM in light mode, CARTO in dark).
- [ ] Draw a polygon, name it, click **Save**. Reload the page: the zone persists.
- [ ] Edit an existing zone's geometry, save. Reload: changes persist.
- [ ] Delete a zone with the toolbar's delete mode (click the zone). Save. Reload: the zone is gone.

### Config-surface

- [ ] In **Settings → Add-ons → Polygonal Zones → Configuration**, confirm option labels are in plain English (not raw YAML keys). Descriptions explain the `save_token`, `trusted_proxies` and `overlay_entities` trade-offs, and say `allow_all_ips` no longer grants anything.
- [ ] Try setting `zone_colour: ""`. Supervisor should reject it at save time with a schema error.
- [ ] Try `zone_colour: "rgb(255,0,0)"`. Also rejected.
- [ ] `zone_colour: "#ff00aa"` is accepted, and zones render in that colour.
- [ ] Set `theme: dark` and confirm the tile layer switches to the dark CARTO basemap.

### Backup / restore

- [ ] Create a backup via **Settings → System → Backups**, with the add-on included.
- [ ] Restore the backup. Zones survive intact.

### Security posture

- [ ] Response headers on an ingress `/zones.json` fetch (browser devtools):
  - `Content-Security-Policy` present
  - `X-Frame-Options: SAMEORIGIN`
  - `X-Content-Type-Options: nosniff`

The container's name carries a repository-specific prefix. Find it with `docker ps --filter name=polygonal_zones` and use it as `<container>` below.

- [ ] AppArmor check: `docker inspect <container> | grep -i apparmor` shows the add-on's own profile, named after its slug (ending `_polygonal_zones`), not `docker-default`.
- [ ] Non-root check. s6 starts as root and drops to `app`, so `docker exec <container> id` shows root. Find the web process with `docker exec <container> pgrep -f 'python main.py'`, then `docker exec <container> awk '/^Uid:/{print $2}' /proc/<pid>/status` prints `1001`.

### Watchdog

- [ ] With **Watchdog** on in the add-on's **Info** tab, `docker exec <container> pkill -f 'python main.py'` restarts the add-on. The unhealthy path is not tested.

### Save-token flow

Only relevant if you enable the LAN port (`Configuration → Network → set a host port for 8000/tcp`).

- [ ] With `allow_all_ips: true`, LAN `curl .../zones.json` **without** the header returns `403`.
- [ ] Opening `http://<ha-host>:8000/` in a LAN browser returns `403`.
- [ ] With `save_token: "abc"` set, LAN `curl -X POST .../save_zones` **without** the header returns `403`.
- [ ] Same with the header and a valid body returns `200`. A token alone is not enough: without `Content-Type: application/json` the answer is `415`, and a missing `Content-Length` or an oversized body gets `413`.

  ```sh
  curl -X POST -H 'Content-Type: application/json' -H 'X-Save-Token: abc' \
    --data-binary '{"type":"FeatureCollection","features":[]}' \
    http://<ha-host>:8000/save_zones
  ```

- [ ] Trailing whitespace: `X-Save-Token: abc ` returns `200`.
- [ ] 10 failed attempts in 60 seconds return `429 Too Many Requests` on the 11th. Wait 60s and try again with the correct token: it succeeds.

### trusted_proxies validator

- [ ] Set `trusted_proxies: "*"`, restart: the addon log contains `Refusing a trusted_proxies entry that means every address`.
- [ ] Set `trusted_proxies: "172.30.0.0/16"`, restart: the log contains `CIDR covers the HA ingress IP`.
- [ ] Set `trusted_proxies: "proxy.local"`, restart: the log contains `Refusing an unparseable trusted_proxies entry`.
- [ ] Set `trusted_proxies: "10.0.0.1, 192.168.1.0/24"`, restart: the log shows `Honouring X-Forwarded-For from 2 configured proxy/proxies.`

### Upgrade path

- [ ] Restore a partial backup of the add-on taken on an earlier release (**Settings → System → Backups**); a chosen version cannot be installed any other way. Draw and save some zones.
- [ ] Upgrade to the branch under test. Confirm existing zones are preserved and the addon starts clean.
- [ ] Rollback by restoring the add-on's pre-update partial backup (`ha apps update <slug> --backup`, then restore it; the slug ends in `_polygonal_zones`; `ha apps` lists it): confirm zones survive.

## When live verification is not required

Pure CI changes (workflow YAML edits), documentation, README, test-only changes, and tests for existing code paths don't need live verification. CI is sufficient.
