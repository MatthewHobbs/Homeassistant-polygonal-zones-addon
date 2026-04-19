# Testing

This repo has three layers of automated testing, plus a manual live-HA-OS layer for anything CI can't cover.

## Automated (CI)

| Layer | What it runs | Where |
|---|---|---|
| `Tests` | `pytest -v` (100% line coverage gated by `--cov-fail-under=100`) | [`.github/workflows/test.yml`](.github/workflows/test.yml) |
| `Lint addon` | `frenck/action-addon-linter` + `shellcheck` of `scripts/` and `rootfs/` shell files | [`.github/workflows/lint.yml`](.github/workflows/lint.yml) |
| `Build addon` | Multi-arch Dockerfile build (per `polygonal_zones_editor/build.yaml`) + amd64 smoke boot + Playwright headless page load | [`.github/workflows/build.yml`](.github/workflows/build.yml) |

The amd64 smoke step in `build.yml` boots the container with a stub `options.json` mount and probes:
- `/healthz` returning `ok`
- `/zones.json` returning a valid GeoJSON `FeatureCollection`
- `POST /save_zones` round-trips a minimal Polygon payload
- the web service runs as uid 1001 (not root) — regression guard for the s6 privilege-drop fallback
- the page loads clean in headless Chromium with no JS errors and renders at least one `<zone-entry>`

All three workflows gate the release pipeline. A tag push to `v*` can't publish images until `Tests`, `Lint`, and `Build` (incl. amd64 smoke boot + Playwright) are all green.

## Manual — live HA OS

Some regressions only show up under a real Supervisor. Reasons:

- `config.yaml` schema validation (e.g. `zone_colour: match(...)`) only fires when Supervisor applies the configuration.
- Ingress (`172.30.32.2`) isn't synthesisable from a plain docker run — you need Supervisor's networking.
- `backup: hot` behaviour requires triggering a Supervisor snapshot.
- AppArmor profile enforcement is off when the addon runs outside Supervisor.
- Codenotary signature verification is only performed by Supervisor on install.

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
