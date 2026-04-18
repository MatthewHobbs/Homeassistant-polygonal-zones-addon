# Changelog

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
