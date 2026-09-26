# Testing

This repo has four layers of automated testing, plus a manual live-HA-OS layer for anything CI can't cover.

## Automated (CI)

| Layer | What it runs | Where |
|---|---|---|
| `Tests` | `pytest -v` (100% line coverage gated by `--cov-fail-under=100`) | [`.github/workflows/test.yml`](.github/workflows/test.yml) |
| `Lint addon` | `frenck/action-addon-linter`, an AppArmor profile compile (`apparmor_parser -Q -K`), `ruff`, a non-required check that the declared HA floor is within the global bound, and `shellcheck` of `scripts/` and `rootfs/` shell files | [`.github/workflows/lint.yml`](.github/workflows/lint.yml) |
| `Build addon` | Multi-arch Dockerfile build (per `polygonal_zones_editor/build.yaml`), each arch built and booted natively (amd64 on `ubuntu-latest`, aarch64 on `ubuntu-24.04-arm`, no QEMU), plus a Playwright headless page load on both arches | [`.github/workflows/build.yml`](.github/workflows/build.yml) |
| `Supervisor pilot` (nightly / on demand, not yet required) | Installs, configures and probes the add-on under a real stable-channel Supervisor and Core, in the official add-on devcontainer, on both arches | [`.github/workflows/supervisor.yml`](.github/workflows/supervisor.yml) |

The smoke step in `build.yml` boots the container on each arch's native runner with a stub `options.json` mount and probes:
- `/healthz` returning `ok`
- `/zones.json` returning a valid GeoJSON `FeatureCollection`
- `POST /save_zones` round-trips a minimal Polygon payload
- the web service runs as uid 1001 (not root) — regression guard for the s6 privilege-drop fallback
- no Python traceback in the container log
- the page loads clean in headless Chromium with no JS errors and renders at least one `<zone-entry>`, drawn and saved through the real Geoman toolbar on both a secure and a non-secure origin

`Tests`, `Lint`, and `Build` gate the release pipeline: a tag push to `v*` can't publish images until all three are green on the tagged SHA. `Supervisor pilot` does not gate release yet (ADR 0001 row 5) — a release can still ship without it.

## Supervisor pilot (nightly / on demand)

`.github/workflows/supervisor.yml` runs `scripts/supervisor-pilot.sh` in `ghcr.io/home-assistant/devcontainer` on `ubuntu-latest` (amd64) and natively on `ubuntu-24.04-arm` (aarch64). Each arch runs two legs: Core pinned to whatever `stable.json` currently names, and Core pinned to the floor declared in `config.yaml`'s `homeassistant:` (read via `scripts/ha-floor-check.sh --declared`). It installs the add-on the Supervisor way (from `apps/local`, built from the checkout under test, not the published image), sets its options through the Supervisor API, and probes it through ingress. It enforces AppArmor — this is what caught the profile's unix-socket-mediation break, fixed in 0.4.2 — on a newer parser and kernel than HA OS ships.

It runs nightly and on demand (`workflow_dispatch`), and on any PR that changes the pilot itself or the declared floor. It is not yet a required PR check and `release.yml` does not call it, so it cannot yet block a release; ADR 0001 row 5 tracks promoting it once a trial period shows no non-flaky failures.

## Manual — live HA OS

The Supervisor pilot now covers install, setting a *valid* option through the Supervisor API, ingress, and AppArmor enforcement — but on a devcontainer, not HA OS itself, and only the happy path. Some things still only show up under a real HA OS install, or need a case the pilot doesn't probe:

- `config.yaml` schema *rejection* (e.g. `zone_colour: match(...)` refusing `""` or `rgb(...)`) — the pilot only round-trips a valid value.
- `backup: hot` behaviour requires triggering a Supervisor snapshot.
- Provenance of the published images. `config.yaml` leaves `codenotary` unset (CAS is discontinued); the images carry Sigstore
  build-provenance attestations, which the Supervisor does not verify on install. Verify them yourself after a release:
  `gh attestation verify oci://ghcr.io/matthewhobbs/<arch>-addon-polygonal_zones:<version> --owner MatthewHobbs`.
- HA OS's actual kernel and AppArmor parser versions, which differ from the devcontainer's (see ADR 0001's 2026-09-26 amendment).
- Real hardware (RPi, HA Yellow/Green) rather than a GitHub Actions runner.

**Do these before merging any PR that touches `Dockerfile`, `rootfs/`, `config.yaml`, `build.yaml`, or `apparmor.txt`:**

### Setup

- [ ] Install the branch as an addon via the **Add Repository** flow pointing at your fork / branch URL.
- [ ] Confirm the addon starts cleanly: **Settings → Add-ons → Polygonal Zones → Log** — no tracebacks.
- [ ] The startup `Loaded options:` line should show `save_token: ***` (redacted) if a token is set.

### Core flows

- [ ] Open the Web UI via the **Open Web UI** button. Map tiles render (OSM in light mode, CARTO in dark).
- [ ] Draw a polygon, name it, click **Save**. Reload the page — the zone persists.
- [ ] Edit an existing zone's geometry, save. Reload — changes persist.
- [ ] Delete a zone via the toolbar's delete mode. Save. Reload — zone is gone.

### Config-surface

- [ ] In **Settings → Add-ons → Polygonal Zones → Configuration**, confirm option labels are in plain English (not raw YAML keys). Descriptions explain `save_token`, `allow_all_ips`, `trusted_proxies` trade-offs.
- [ ] Try setting `zone_colour: ""` — Supervisor should reject at save time with a schema error.
- [ ] Try `zone_colour: "rgb(255,0,0)"` — also rejected.
- [ ] `zone_colour: "#ff00aa"` — accepted, zones render in that colour.
- [ ] Set `theme: dark` and confirm the tile layer switches to the dark CARTO basemap.

### Backup / restore

- [ ] Create a snapshot via **Settings → System → Backups**. Addon should continue running during snapshot (`backup: hot`).
- [ ] Restore the snapshot. Zones survive intact.

### Security posture

- [ ] Response headers on an ingress `/zones.json` fetch (browser devtools):
  - `Content-Security-Policy` present
  - `X-Frame-Options: SAMEORIGIN`
  - `X-Content-Type-Options: nosniff`
- [ ] AppArmor check: `docker inspect addon_polygonal_zones | grep -i apparmor` → `apparmor=addon_polygonal_zones` (not `docker-default`).
- [ ] Non-root check: `docker exec addon_polygonal_zones id` → `uid=1001(app)`.
- [ ] `/data/options.json` perms: `docker exec addon_polygonal_zones ls -l /data/options.json` → `-rw-r----- app app` (0640, not world-readable).

### Save-token flow

Only relevant if you enable the LAN port (`Configuration → Network → set a host port for 8000/tcp`).

- [ ] With `save_token: "abc"` set, LAN `curl -X POST .../save_zones` **without** the header → `401`.
- [ ] Same with header `X-Save-Token: abc` → `200`.
- [ ] Trailing whitespace: `X-Save-Token: abc ` → `200` (strip-symmetric behaviour).
- [ ] 10 failed attempts in 60 seconds → `429 Too Many Requests` on the 11th. Wait 60s and try again with the correct token — succeeds.

### trusted_proxies validator

- [ ] Set `trusted_proxies: "*"` → restart → addon log contains `Refusing a wildcard trusted_proxies entry`.
- [ ] Set `trusted_proxies: "172.30.0.0/16"` → restart → log contains `CIDR covers the HA ingress IP`.
- [ ] Set `trusted_proxies: "proxy.local"` → restart → log contains `Refusing an unparseable trusted_proxies entry`.
- [ ] Set `trusted_proxies: "10.0.0.1, 192.168.1.0/24"` → restart → log shows `Honouring X-Forwarded-For from 2 configured proxy/proxies.`

### Upgrade path

- [ ] Install an older version (e.g. `0.2.14`). Draw and save some zones.
- [ ] Upgrade to the branch under test. Confirm existing zones are preserved and the addon starts clean.
- [ ] Rollback via the version picker: confirm zones survive the downgrade.

## When live verification is not required

Pure CI changes (workflow YAML edits), documentation, README, test-only changes, and tests for existing code paths don't need live verification — CI is sufficient.

## Rollback

See [`docs/RUNBOOK.md`](docs/RUNBOOK.md) for partial-release recovery and emergency rollback procedures.
