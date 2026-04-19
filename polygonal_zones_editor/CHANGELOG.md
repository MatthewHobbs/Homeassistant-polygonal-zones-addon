# Changelog

## 0.2.31 — 2026-04-19

UX polish slice: two small editor fixes that addressed friction points flagged in the principal-engineer panel review.

### Changed

- **Save-error state persists until the next save attempt (#123).** Previously, a network blip or 4xx response flashed the Save button red for two seconds and then cleared — users who tapped Save on mobile and then looked away often missed it entirely, leaving them unsure whether their zones had actually saved. The error message now stays on screen until the user clicks Save again (the "next action"), which both dismisses it and reattempts the save. Success still auto-clears at 2s; 412 conflicts still stay until manually resolved. Matches the pattern 412 already used.

### Added

- **Map viewport persists across reloads (#130).** The first-load experience previously centred the map on Groningen (the original upstream author's home, hardcoded at `map.js:48`). Users outside the Netherlands had to scroll-and-zoom on every visit before they could draw any zone. The map now remembers `center` and `zoom` per user in `localStorage` under `pz:viewport` on `moveend` / `zoomend` (debounced 500 ms), and restores on load. Existing users with zones but no persisted viewport still get the auto-fit-to-zones behaviour on first post-upgrade load — the persisted viewport only wins once the user has actively panned or zoomed. Genuine first-time users with no zones and no persisted viewport still see Groningen as an obvious "not-my-area" signal that prompts a pan to their actual location.

### Release gate

Build-time Playwright smoke extended to regression-guard both items: a failing-save response leaves the status text and `.error` class visible (not auto-cleared), and a `moveend` write followed by reload restores the center/zoom.

## 0.2.30 — 2026-04-19

Durability slice (part 2): tighten save-time GeoJSON validation so geometrically invalid zones are rejected at the boundary rather than silently persisted where they produce undefined point-in-polygon behaviour downstream. Partner to 0.2.29's conditional-GET hardening.

### Changed — stricter `POST /save_zones` validation (#118)

`_is_valid_feature_collection` previously only checked the JSON shape (Feature, geometry type, coordinates is a list). It now also enforces:

- **Closed linear rings** per RFC 7946 §3.1.6 — each ring must have at least 4 positions and the first and last must be identical. Polygons drawn in the editor and MultiPolygon features emitted by Leaflet's `toGeoJSON()` are already closed; this catches hand-written `zones.json` files and bulk-imports that forgot to repeat the first vertex.
- **WGS84 coordinate ranges** — longitude must be in `[-180, 180]`, latitude in `[-90, 90]`, both finite (rejects `Infinity`, `NaN`) and numeric (rejects strings and booleans — `True` subclasses `int` in Python but would round to `(1, 0)`, nonsense for a zone).
- **Per-feature vertex cap** of 1000 positions aggregate across all rings in a Polygon / MultiPolygon. Mild algorithmic-DoS defence: a malicious payload could fit ~10k vertices in the 512KB body cap which Supervisor has to load into memory; 1000 is the upper bound for any legitimate home / workplace / school zone (typical zones are 4–30 vertices).
- **Unique zone names** — two features with the same `properties.name` would make HA automations ambiguous (`state_attr('zone.home', ...)` — which 'home'?). Names that are missing or `null` are still allowed (HA won't surface a nameless feature as a `zone.*` entity anyway).

Structural / type rejections now return a descriptive error body with an index-bearing detail pointer so a client that logs the response can pinpoint the offending feature or ring without the server echoing coordinate values back (avoiding any PII bounce):

```json
{"error": "invalid GeoJSON", "detail": "features[3].geometry.coordinates[0]: ring is not closed (first position must equal last)"}
```

Self-intersection and winding-order are **not** enforced — RFC 7946 mandates CCW exteriors but most downstream consumers (Shapely, Turf.js, HA's own zone engine) are tolerant. Adding those checks would require a geometry dependency for marginal benefit at this scale.

### Backwards compatibility

Anyone with a zones file drawn via the editor or emitted by Leaflet's `toGeoJSON()` is unaffected — those paths already produce closed, in-range, valid GeoJSON.

**Potentially affected:** users who previously bulk-loaded a hand-written or externally-generated `zones.json` with (a) non-closed rings, (b) rings with fewer than 4 positions, (c) coordinates outside WGS84 ranges, or (d) duplicate zone names. Their existing file still serves from `GET /zones.json` — the validator only runs on write — but the next `POST /save_zones` from the editor or via `curl` will 422 until the invalid zone is fixed. The error body identifies which feature and which specific violation.

## 0.2.29 — 2026-04-19

Durability slice: conditional GET on `/zones.json` so the companion integration's polling loop stops re-parsing a full `FeatureCollection` on every tick when nothing has changed.

### Added

- **Conditional GET on `/zones.json` via `If-None-Match`** (#119). The response already carried an `ETag`; now it also carries `Last-Modified` (RFC 7231 GMT date, derived from file mtime), and the handler short-circuits to `304 Not Modified` (no body) when the client's `If-None-Match` header matches the current ETag. The wildcard `If-None-Match: *` is honoured per RFC 7232, and so is a comma-separated list of candidate validators. `ETag`, `Last-Modified`, and `Cache-Control` are repeated on 304 so clients can refresh their cache state from a single response. Pairs naturally with a future SSE / WebSocket push channel ([#127](https://github.com/MatthewHobbs/homeassistant-polygonal-zones-addon/issues/127)) — cheap polling now, push later.
- **Polling-idiom example in `DOCS.md`** — a two-line `curl` snippet showing the recommended integration pattern (cache the ETag, resend as `If-None-Match` on subsequent polls, treat 304 as "no change").

### Backwards compatibility

Fully additive on the read path. Clients that don't send `If-None-Match` get the 200 response they always got (same body, same ETag, plus the new `Last-Modified` header). No breaking change for the integration, `curl` scripts, or the browser.

## 0.2.28 — 2026-04-19

First user-visible UX slice after the 0.2.27 correctness batch — a tile-layer picker (#31). Scaffolding landed in 0.2.24; this release wires the actual control.

### Added

- **Basemap picker** in the sidebar (#31). A native `<select>` labelled "Map style" lives between the sidebar header and the zone list. Four options:
  - `Auto (follows theme)` — follows the OS `prefers-color-scheme` (and the `theme` option in addon config if forced to light/dark). This is the default when you haven't made an explicit pick.
  - `Street map` — OpenStreetMap light tiles.
  - `Dark` — CARTO Dark tiles.
  - `Satellite` — Esri World Imagery. Free, permissive-ToS satellite tiles; no API key required. Attribution auto-displayed in the Leaflet control.
  The choice persists to `localStorage` under `pz:basemap` and survives reload. Picking `Auto` removes the stored key and re-engages the theme-follow behaviour. CSP was widened to permit `https://server.arcgisonline.com` as a new `img-src` origin for the satellite option.
- **Tile-error banner** on the map. If the active basemap fails to serve five consecutive tiles (threshold chosen to ignore a single flaky tile or momentary DNS blip while catching a dead provider within a couple of seconds of panning), a small non-blocking banner appears at the bottom-centre of the map reading *"Map tiles failed to load. Try a different map style."* The banner clears on the next successful tile and resets whenever the user switches basemaps. Styled with existing design tokens (`--popup-bg`, `--border-color`, `--text-color`) so light/dark parity is free.

### Release gate

- Build-time Playwright smoke asserts the picker element exists, contains the four expected options in order, and that switching to Satellite swaps the active tile layer. Locks the picker UX into the release gate so silent regressions (script-ordering, option-list drift) can't reach users.

## 0.2.27 — 2026-04-19

Installation and correctness — a batch of owned P0 fixes from this week's principal-engineer panel review. No new features; the next user-visible UX work is queued for 0.2.28.

### Security

- **Gated `GET /zones.json` behind `save_token`** when the option is configured (#113). Previously, setting `save_token` protected `POST /save_zones` but left `GET /zones.json` reachable by any LAN client once `allow_all_ips: true` was on — zone geometry (your home, workplace, school runs) was effectively less protected than the less-sensitive write action. Reads now share the full authz ladder of `/save_zones`: ingress always allowed (so the HA UI keeps working), `save_token` if set requires `X-Save-Token: <value>` (and unlocks LAN even when `allow_all_ips: false`), otherwise fall back to `allow_all_ips`. Strip-both-ends whitespace handling is symmetric with `/save_zones`. Failed reads share the existing 10-failures-in-60s rate-limit bucket with `/save_zones` so an attacker can't brute-force the token by rotating between GET and POST — this was verified end-to-end via a local Docker build of the image against four scenarios (allow_all_ips × save_token matrix).

### Frontend

- **`beforeunload` guard on unsaved edits** (#115). Drawing a polygon, deleting, renaming, finishing an edit, or bulk-loading now marks the editor as dirty. Navigating away (sidebar tap, back button, tab close) while dirty triggers the browser's standard "Leave site?" confirmation. The dirty flag clears on a successful save (200); a 412 conflict keeps the flag set because the edit still isn't flushed.
- **Removed two `console.log(layer)` / `console.log(geojson)` calls** in `map.js`'s `draw:created` handler (#115). These were leaking the full GeoJSON — including real home/work coordinates — into browser devtools on every polygon draw, where any console-capturing RUM or mid-screen-share inadvertently exposed them.

### Fixed (latent bugs surfaced during local-Docker verification of the above)

- **Drawn polygons now save.** `draw:created` was building `layer.feature = {properties: {...}}` without a `type` field. Leaflet's `toGeoJSON()` uses `layer.feature` as a template when present and only overrides `geometry`, so the emitted Feature was missing `"type":"Feature"` — which the server-side validator rejects with 422. This bug had existed since the handler was written but was never caught because no automated test exercised the full draw→save round-trip (the build-time smoke POSTs a pre-made valid payload). Build-time Playwright smoke now exercises the path as a regression guard.
- **`delete_load_btn` null-guard.** `helpers.js`'s `delete_load_btn()` called `querySelector('#load-btn').remove()` without null-checking. The load button only exists when the editor starts empty; if a user drew a new zone on a non-empty editor the handler threw a TypeError mid-draw. Added optional-chaining on both queries.

### Accessibility

- **Zone-entry rename input** (#116). Replaced the broken `<label for="zone-name-input">` + `<input id="zone-name-input">` pattern with an `aria-label="Zone name"` on the input. Per spec, the `for`/`id` attribute does not cross the shadow-DOM boundary, so the previous implementation never programmatically associated the label — and every zone-entry shared the same id, compounding the bug. Screen readers now announce the field correctly. (WCAG 2.2 SC 1.3.1.)
- **Leaflet-draw toolbar labels** (#116). After `map.addControl(drawControl)`, every `.leaflet-draw-toolbar a[title]` has its `title` mirrored into an `aria-label`. Leaflet-draw itself only emits `title=`, which NVDA and VoiceOver on iOS either ignore or inconsistently announce on interactive anchors. The Draw / Delete / Clear-all controls are now programmatically labelled. (WCAG 2.2 SC 4.1.2.)
- **Build-time smoke extended** to regress-guard both a11y fixes: the Playwright block now asserts the leaflet-draw polygon control carries an `aria-label`, and the first zone-entry's input carries an `aria-label` when in edit mode.

### Documentation

- **`DOCS.md` privacy rewrite** (#112, already on `main` from PR #133 — landed ahead of the 0.2.27 cut so Supervisor's metadata refresh picks it up independently of the version bump). The "private-IP URLs" section now names a **private reverse proxy with TLS on a non-RFC-1918 hostname** (nginx, Caddy, Traefik, or HA's NGINX Proxy Manager addon) as the recommended workaround. The public-CDN mirror path is demoted to a "last resort" with explicit enumeration of the privacy cost: world-readable, CDN replication, CLOUD Act reach, search indexing, and archived copies that survive deletion.

### Tracked upstream

- Issue [#111](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones-addon/issues/111) — the integration-side RFC-1918 opt-in. Until it ships upstream, the reverse-proxy workaround is the official path. Not a code change in this release; mentioned so the full "v1 correctness" story has a tracked home.

## 0.2.26 — 2026-04-19

### Removed

- Dropped `armhf`, `armv7`, and `i386` from the published architectures. Home Assistant 2025.12 (released 2025-12-03) deprecated 32-bit ARM and x86 as supported host architectures for addons; keeping them listed was generating `Architecture '<arch>' is deprecated and no longer supported` warnings on every `hassfest`/`@home-assistant/builder` lint run. `config.yaml`'s `arch:` list and `build.yaml`'s `build_from:` map are now `aarch64` + `amd64` only. The matrix generators in `.github/workflows/build.yml` and `.github/workflows/release.yml`, plus `.github/base-images.yaml`, follow suit — no dead base-image digests, no unused platform entries in the `arch_to_platform` dict. Users on 32-bit hosts running HA < 2025.12 will stop receiving updates at 0.2.25; they can pin to that version via the Supervisor version picker until they upgrade their host.

## 0.2.25 — 2026-04-18

### Added

- **Collapsible sidebar (#29).** A small chevron button on the inner edge of the sidebar now toggles the zone list in and out. In the open state the button sits at the sidebar's left edge pointing right (collapse); clicking it slides the sidebar off-screen and repositions the button to the map's right edge pointing left (expand). State persists to `localStorage` under `pz:sidebar` so the choice survives reloads. The map is told to `invalidateSize` after the CSS grid-column transition settles, so Leaflet recomputes tile bounds immediately — no stale gutter, no blank tiles. Respects `prefers-reduced-motion`: the transition is skipped and the map resize fires on the next tick instead of waiting for `transitionend`. ARIA attributes (`aria-expanded`, `aria-controls`, `aria-label`) update on every toggle for screen readers.

## 0.2.24 — 2026-04-19

### Security

- Reintroduced a custom AppArmor profile, this time built as a **deny-list rather than an allow-list**. The profile grants the same general access that `docker-default` grants (`file, signal, network, capability`) and then explicitly denies the specific paths and operations a map-drawing addon has no legitimate need for: reads of `/etc/shadow`/`/etc/gshadow`, writes to `/root`/`/home`/`/boot`/`/sys/kernel`/`/proc/sys/kernel`, and the `mount`/`umount`/`ptrace` syscalls. This is strictly better than `docker-default` alone — an exploit inside the addon sandbox can't read hashed passwords, escape to the host filesystem, tamper with kernel parameters, mount filesystems, or inject into other processes — while avoiding the allow-list pitfall that took the addon offline in 0.2.16–0.2.21 (where any path the Python runtime or s6-overlay needed that wasn't in the allow-list silently failed).

### Refactor

- Frontend prep work for the collapsible sidebar (#29), responsive drawer (#30), and tile-layer picker (#31). Two new modules added: `app/static/js/basemaps.js` (registry of Leaflet tile-layer definitions, seeded with the existing OSM and CARTO Dark entries — `map.js` now resolves its initial layer through the registry) and `app/static/js/ui-state.js` (single write-point for a new `data-sidebar="open|collapsed|drawer"` attribute on `.body`, with the matching `pz:map-resized` custom event fired after Leaflet's container is invalidated). Complements `app/static/js/storage.js` (thin localStorage wrapper with a silent in-memory fallback) which landed alongside the 0.2.22 release-merge.sh fix.
- `map.js` carries a new `userChoseTile` flag set from Leaflet's `baselayerchange` event. Once #31 ships a picker, the existing OS-theme-follow auto-swap will no longer override an explicit choice.
- `style.css` moves the existing `1fr / 300px` grid definition under `.body[data-sidebar="open"]` and scaffolds empty rules for the two future states. `index.html` swaps the dead `sidebar-open` class (no selector matched it anywhere) for `data-sidebar="open"`.
- No user-visible change from the refactor: nothing in this release sets the `data-sidebar` attribute to anything other than its `open` default, so the rendered page is byte-identical to 0.2.22 apart from the restored AppArmor profile.

## 0.2.22 — 2026-04-19

### Fixed

- **Hotfix: removed the custom AppArmor profile.** 0.2.21's `mrix` tweak addressed the `/init` read-permission gap visible during teardown, but the container still failed to bind port 8000 under the profile — Supervisor logged repeated `Ingress error: Cannot connect to host 172.30.33.1:8000` and the Web UI returned 502. Rather than iterate on the profile blind without a live Supervisor to test against, `polygonal_zones_editor/apparmor.txt` has been deleted. Supervisor falls back to the stock `docker-default` AppArmor profile, same as 0.2.14 and every earlier release. A hardened custom profile can return once it's been validated against a live HA OS instance under Supervisor — not against GHA's smoke container, which doesn't load addon profiles.

  Users on 0.2.16–0.2.21 who hit the 502 / restart loop recover automatically once 0.2.22 lands on ghcr.io, or can roll back to 0.2.14 in the meantime via the Supervisor version picker.

## 0.2.21 — 2026-04-19

### Fixed

- **Hotfix: AppArmor profile was blocking s6 teardown.** The profile shipped in 0.2.16 granted `ix` (execute + inherit) on `/init`, `/bin/**`, `/usr/bin/**`, `/run/{s6,s6-rc*,service}/**`, `/package/**`, `/command/**`, and `/usr/lib/bashio/**` without the `r` (read) permission that `/bin/sh` needs to open a script for interpretation. Addon teardown would log `can't open '/init': Permission denied` and exit the container with status 256, visible on live HA installs as "Web service exited with status 256; stopping container". Every executable allow-rule now includes `mr` (memory-map + read) so shutdown/restart paths work. Behaviour-wise the sandbox is unchanged — writes outside `/data`/`/tmp` are still denied, as are `ptrace`/`mount`/`net_admin`.

  Users who upgraded to 0.2.16–0.2.20 and saw the addon repeatedly restart should pick up this fix automatically once the 0.2.21 image appears on ghcr.io.

## 0.2.20 — 2026-04-19

- MultiPolygon zones now round-trip through the Save button intact. Previously `save_zones()` hand-assembled a GeoJSON `Polygon` from every layer, silently dropping every ring beyond the first on any zone loaded from a `MultiPolygon` feature (via bulk-load or a hand-edited zones.json). Save now uses Leaflet's `toGeoJSON()` so the layer's actual geometry type — Polygon or MultiPolygon — is preserved.
- The sidebar zone list now shows a small `(N shapes)` indicator when a zone is a MultiPolygon. Helps distinguish merged zones from single-shape zones at a glance.
- Playwright smoke test extended: the build-time round-trip now POSTs both a Polygon and a MultiPolygon, reloads the page, and asserts both render with the right shape-count indicator.

## 0.2.19 — 2026-04-19

### Security

- Added `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`, and a tailored `Content-Security-Policy` on every response. The CSP keeps OSM + CARTO tile hosts in `img-src`, allows the SRI-pinned Leaflet + Leaflet-Draw bundles via `unpkg.com` in `script-src`/`style-src`, restricts `connect-src` to same-origin, and permits ingress (`*.home-assistant.io`, `*.ui.nabu.casa`) to iframe the UI via `frame-ancestors` while blocking everything else — clickjacking + XSS defence-in-depth on the one exposed web surface.
- `trusted_proxies` now rejects CIDR supernets that contain the HA ingress IP (e.g. `172.30.0.0/16`, `172.0.0.0/8`, `128.0.0.0/1`) in addition to the previously-blocked wildcard and exact-ingress-IP entries. Unparseable entries (hostnames, garbage) are now refused with a typed error instead of being silently forwarded to uvicorn. Parsing uses Python's `ipaddress` module.
- `X-Save-Token` is now `.strip()`ed symmetrically with the stored token. Previously only the stored value was stripped; a trailing whitespace in the header silently failed an otherwise-correct token.

### API

- `POST /save_zones` 412 response no longer includes `current_etag: null` when the zones file is unreadable at conflict-check time. The field is omitted entirely in that case — clients can fall back to `GET /zones.json` to refetch. Backwards compatible for existing clients (the key was `null` before; now it's absent).

## 0.2.18 — 2026-04-19

- Option labels in the Supervisor UI are now human-readable. Shipped `translations/en.yaml` so `zone_colour`, `theme`, `allow_all_ips`, `save_token`, `trusted_proxies`, and `log_level` get proper names + descriptions instead of raw YAML keys. The security-relevant options (`allow_all_ips`, `save_token`, `trusted_proxies`) each get a one-paragraph description so the Configuration page explains the trade-off inline. Other locales can be added by dropping in e.g. `translations/de.yaml`; the HA addon linter picks them up automatically.
- `zone_colour` schema is now `match(^#[0-9a-fA-F]{3,8}$|^[a-zA-Z]+$)` instead of `str`. Supervisor rejects empty strings and `rgb()/hsl()` function notation at Configuration save time — values that would have silently broken the map render. Existing `purple`, `red`, `#800080`, etc. continue to work unchanged.

## 0.2.17 — 2026-04-19

### Reliability

- `/zones.json` no longer 500s with a bare traceback when the zones file is missing or unreadable. The handler returns a proper `503 Service Unavailable` with a JSON error body (`{"error": "zones file unreadable"}`) and a `_LOGGER.exception` line so operators can see the file path and errno.
- `/healthz` now verifies that the zones file is accessible instead of just reporting process liveness. The Docker HEALTHCHECK fails (and Supervisor restarts the container) when the zones file becomes unreadable, giving users a self-healing addon instead of a "running but broken" state.
- `atomic_write_json` now fsyncs the parent directory after the rename, so the new directory entry is durable on hard power-off. Without this, on some filesystems (overlayfs as used by HA OS, vfat, tmpfs) the rename could be lost to a power failure and users would reboot to an empty `zones.json`.
- `POST /save_zones` is now rate-limited: 10 authorisation failures from a given IP in a 60-second window cause subsequent requests from that IP to return `429 Too Many Requests` for the remainder of the window. Defends against LAN brute-force of `save_token`. Valid token requests and ingress requests never increment the counter.
- If the s6 init falls back to running the web service as root (missing `s6-setuidgid`, missing `app` user, or unreadable `options.json`), the addon now logs a prominent warning on every boot in both the service log (`bashio::log.error`) and the addon log (`Running as uid 0 (root)...`). Previously only a single warning line appeared in a log users rarely read.

### Tests

- New coverage: `/zones.json` returns parseable GeoJSON for a populated file (the actual integration contract); `/zones.json` returns 503 when the file is unreadable; `/healthz` returns 503 when the file is missing; `/save_zones` rate-limit kicks in after 10 failures and valid tokens still succeed before the budget is exhausted; `/zones.json` is still ingress-locked when `save_token` is set without `allow_all_ips`; `atomic_write_json` fsyncs the parent directory.

## 0.2.16 — 2026-04-19

### Security

- **Breaking: direct LAN access to the addon is now opt-in.** `config.yaml`'s default port mapping `8000/tcp: 8000` is now `8000/tcp: null`, so the Supervisor does not publish the port to the host network by default. Users who rely on `curl` backups or run the companion integration in another container must enable the port under **Settings → Add-ons → Polygonal Zones → Network** and choose a host port. Ingress (the `Open Web UI` button and the companion integration running under the same HA install) is unaffected.
- `trusted_proxies` now refuses wildcard and ingress-IP values (`*`, `0.0.0.0/0`, `::/0`, `172.30.32.2`). Previously these could be handed to uvicorn unchecked, which let any on-path client forge `X-Forwarded-For: 172.30.32.2` and bypass `save_token` on `POST /save_zones`. The parser drops these entries with a logged error and keeps the addon running with safe entries — a typo can no longer lock you out, but it also can no longer silently open a hole.
- `/data/options.json` is no longer `chmod o+r`'d after boot. The previous belt-and-braces made the file (which contains `save_token` when configured) world-readable inside the container. If `chown app:app` fails (read-only mount, unexpected FS), `services.d/web/run`'s existing fallback to root-run handles readability without a world-readable secret.
- Shipped a tailored AppArmor profile (`apparmor.txt`). The Supervisor loads it in place of the generic `docker-default`. The profile denies writes outside `/data` and `/tmp`, denies `ptrace` / `mount` / `net_admin`, and restricts the process to the paths it actually needs.

## 0.2.15 — 2026-04-19

- Home Assistant addon metadata brought into line with current conventions. `config.yaml` now declares `url` (link back to the addon folder) and the startup tier is `services` instead of `system` since the addon only serves data Core polls rather than being a core-boot dependency. The root `repository.yaml` is updated to point at this fork's URL and maintainer instead of the deprecated upstream. No behaviour change for users already running 0.2.14.

## 0.2.14 — 2026-04-19

- New `theme` option: `auto` (default — follows OS `prefers-color-scheme`), `light`, or `dark`. Set this when HA's theme and your OS theme disagree (e.g. HA on dark, laptop on light) and you want the editor to pick one. The override controls both the CSS palette and the tile layer.
- Internal: ZoneEntry no longer duplicates the dark-mode palette in its shadow DOM. CSS custom properties on `:root` cascade through shadow boundaries, so the document-scope theme defines the values once and the shadow root inherits them.
- `Loaded options:` continues to redact `save_token` (no behaviour change here, just confirming).

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
