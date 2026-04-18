# Changelog

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
