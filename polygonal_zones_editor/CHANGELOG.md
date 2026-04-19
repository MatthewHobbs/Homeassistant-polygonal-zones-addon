# Changelog

## 0.2.13 — 2026-04-19

- Dark mode. The editor now follows your OS / browser `prefers-color-scheme` setting: dark sidebar, dark CARTO tile layer, popup + draw-toolbar overrides so everything stays readable. Switching your OS theme at runtime swaps the tile layer live without a reload. Note: the `zone_colour` option is unchanged — pick a colour that's readable on both light and dark tiles (`purple` works fine on both).

## 0.2.12 — 2026-04-18

- Switched to pre-built images. `config.yaml` now declares `image: ghcr.io/matthewhobbs/{arch}-addon-polygonal_zones`, so the Home Assistant Supervisor pulls the published image instead of running a Docker build on your hardware. Updates are now fast on every architecture, especially ARM SBCs where local builds previously took several minutes. The Dockerfile and `build.yaml` still exist; they're used by CI and the release workflow to produce those images.

## 0.2.11 — 2026-04-18

- Concurrent edits no longer silently clobber each other. `GET /zones.json` now returns a strong `ETag` header (sha256 of the file). `POST /save_zones` honours an `If-Match` precondition: when the on-disk ETag doesn't match, the server returns `412 Precondition Failed` with the current ETag in both header and body, and the addon UI shows a clear "Conflict — reload to fetch the current version" notice instead of overwriting. Clients that don't send `If-Match` (older `curl` scripts, the integration) keep working with last-write-wins semantics. Successful saves now also include the new `ETag` in the response so clients can track without an extra GET.

## 0.2.10 — 2026-04-18

- New `save_token` addon option (password type, default empty). When set, `POST /save_zones` requires `X-Save-Token: <value>` for any non-ingress request, regardless of `allow_all_ips`. Ingress (the HA Save button) keeps working unauthenticated. Closes the LAN-write hole when `allow_all_ips: true`.
- The token is user-set (not auto-generated): you control rotation, we never log it. `Loaded options: ...` now redacts `save_token: ***` so it can't leak via the addon log either.
- Startup logs a clear warning when `allow_all_ips: true` is set without a `save_token` (the dangerous combo), and an info line when a token is configured (the safe combo).
- Token comparison is constant-time via `secrets.compare_digest`.

### Curl with save_token
```sh
curl -X POST -H 'Content-Type: application/json' \
  -H 'X-Save-Token: yourvalue' \
  --data-binary @zones-backup.json \
  http://<host>:8000/save_zones
```

## 0.2.9 — 2026-04-18

- Reproducible builds: the Docker image now installs Python deps from a generated `requirements-lock.txt` with `pip install --require-hashes`. Two builds of the same git SHA on different days now produce identical Python dependency graphs (down to file hashes), and a tampered package file would fail the install instead of being silently used. `requirements.txt` remains the high-level spec; regenerate the lock with `uv pip compile requirements.txt --generate-hashes -o requirements-lock.txt`.

## 0.2.8 — 2026-04-18

- **Bug fix**: zones failed to load with "Failed to load zones — check the log" after upgrading to 0.2.6. The /config.json refactor accidentally moved the `map` and `editableLayers` handles into a callback scope, so other functions (`render_zone_list`, `save_zones`, etc.) couldn't see them and threw `ReferenceError`. They are now declared at module scope and assigned once /config.json returns. The CI smoke test only boots the server — it doesn't load the page in a browser — so this slipped through.

## 0.2.7 — 2026-04-18

- Existing zones loaded from `zones.json` are now drawn in the configured `zone_colour` instead of Leaflet's default blue. Newly drawn zones already used the correct colour; this brings the persisted zones into line.
- New `log_level` addon option (`debug` / `info` / `warning` / `error` / `critical`). Default `info`. Bump verbosity for a debug session without rebuilding the image.
- `GET /zones.json` no longer parses + re-serialises the file on every request — it streams the file bytes through verbatim. Saves a JSON parse/serialise round-trip per request and preserves the file's exact byte representation.

## 0.2.6 — 2026-04-18

- The `zone_colour` option is now exposed as `GET /config.json` instead of being inlined into `index.html` via template substitution. `index.html` is now a fully static file served by Starlette's `StaticFiles`; the per-request file open and string-replace are gone, and the HTML is cacheable. The frontend fetches `/config.json` once on load and falls back to "green" if the request fails.

## 0.2.5 — 2026-04-18

- Accessibility: the Save button's success/failure state is now announced to screen readers via an ARIA live region. Previously the only feedback was a 2-second CSS colour change, invisible to assistive tech. Network errors during save are also surfaced (visually and to readers) instead of being silently swallowed.

## 0.2.4 — 2026-04-18

- Logging: root logger is now configured exactly once at startup via `basicConfig` instead of each module attaching its own handler. Prevents duplicate log lines if a future module calls `getLogger(__name__)` and logs at import time.
- Resilience: a malformed or non-object `/data/options.json` no longer boot-loops the addon. The file is logged and the addon starts with default options, so a corrupted write from the Supervisor or a manual edit can't lock you out.
- Supply-chain: pinned leaflet-draw with an SRI hash (browser refuses tampered bytes from the CDN).

## 0.2.3 — 2026-04-18

- The addon now runs as a non-root `app` user (uid 1001). An s6-overlay init script runs first as root to chown `/data/options.json` and the addon's data directory to `app`, then `s6-setuidgid` drops privileges before launching the web service. A CI runtime smoke test asserts uid != 0 and guards against regression.
- Defensive fallbacks: if `s6-setuidgid` is missing, the `app` user was never created, or the Supervisor-mounted `options.json` still isn't readable by `app`, the service falls back to running as root with a logged warning rather than failing to start. A build-time check also fails fast if the base image doesn't have s6-setuidgid so the image never ships broken.

## 0.2.2 — 2026-04-18

- Run as root again. The non-root `app` user introduced in 0.2.0 could not read the Supervisor-mounted `/data/options.json`, so 0.2.1 failed to start with `PermissionError`. Proper privilege drop needs an s6 init script that chowns `/data` first; tracked in #14.

## 0.2.1 — 2026-04-18

- Add `build.yaml` mapping each architecture to its Home Assistant base image. 0.2.0 failed to build under the Supervisor because `BUILD_FROM` was empty.
- Add this changelog file.

## 0.2.0 — 2026-04-18

### Security
- `POST /save_zones` now validates the body shape (must be a GeoJSON `FeatureCollection`), enforces a 512 KiB size cap, and returns explicit 4xx/500 status codes instead of writing arbitrary JSON to disk.
- `GET /zones.json` now requires the IP allowlist; the wildcard `Access-Control-Allow-Origin: *` header is gone, so browsers on other origins can no longer read your zone coordinates.
- `zone_colour` option is now JSON-escaped before being substituted into the page, so a malformed value cannot break out of the JS string literal.
- Frontend zone names are now inserted via DOM APIs and passed to Leaflet popups as DOM nodes, not interpolated into innerHTML or HTML strings — a zone name containing HTML no longer executes script.
- Bumped `starlette` past CVE-2025-62727 (FileResponse Range DoS, fixed in 0.49.1), CVE-2024-47874 and CVE-2025-54121 (multipart DoS).

### Reliability
- Dockerfile now uses the Home Assistant `BUILD_FROM` base, runs as a non-root user, and declares a `HEALTHCHECK`.
- Removed the build-time `COPY zones.json /data/...` line that wiped user zone data on every addon upgrade.
- Restored uvicorn's default error/access log handlers so HTTP-level errors are visible to operators.
- Added a `/healthz` endpoint for the container probe (exempt from the IP allowlist).

### Internals
- Replaced the handcrafted static-file router with `StaticFiles`. New static assets appear without a restart; path normalisation/traversal protection comes from the framework.
- Centralised the IP allowlist check in a single middleware.
- New `trusted_proxies` option: when set, uvicorn is started with `proxy_headers=True` and `forwarded_allow_ips=<list>`, so `request.client.host` is rewritten from `X-Forwarded-For` only when the immediate peer is one you explicitly trust. Default is empty (current behaviour preserved).
- Switched the OSM tile URL to HTTPS; restored pinch-zoom; added `try/catch` around `JSON.parse` of the initial fetch and bulk-load file.

### Tests
- Coverage added for the size cap, schema rejection, authz on `/zones.json`, `/healthz`, colour escaping, write-failure path, static-file delivery, path-traversal rejection, and the `trusted_proxies` parser. 42 tests passing.
