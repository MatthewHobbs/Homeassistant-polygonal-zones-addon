# Evaluation — polygonal zones for granular HA location

_Principal-panel review, 2026-04-19._

## Goal

Assess how well this project delivers the job-to-be-done: *"Home Assistant uses polygonal zones to provide more granular location than the default circular zones."*

Default HA zones are circles (lat/lon + radius). This project ships a two-piece solution:

1. **Addon** (`polygonal_zones_editor/`, this repo) — a Leaflet web UI inside HA to draw polygons, served as GeoJSON at `GET /zones.json`.
2. **Companion integration** ([`MatthewHobbs/Homeassistant-polygonal-zones`](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones), separate repo) — fetches `zones.json` and exposes polygonal zones to HA automations.

## Verdict — CONCERNS

No blockers. Architecture and backend are sound enough to run, but the **default install path is broken** because the integration's SSRF guard rejects LAN URLs. The documented workarounds either require infra most users don't have or leak home geometry publicly. Separately, the **editor UI has data-loss and a11y gaps** that will hit mobile and screen-reader users first.

## Consensus findings

- **The integration's private-IP SSRF guard breaks the default LAN install path.** Documented workarounds (GitHub Pages / S3 / public reverse proxy) either require infra most users don't have or publish precise home geometry to unauth public URLs. *(chief-architect, product-manager, security-reviewer, sre-reliability)*
- **HTTP is the wrong seam between addon and integration when both live on the same Supervisor host.** A Supervisor-shared-filesystem drop (e.g. `/share/polygonal_zones/zones.json`) + an HA event on save eliminates the SSRF wart, the `save_token`, and polling latency in one move. *(chief-architect, lead-backend, sre-reliability)*
- **No stable per-zone identifier and no schema version.** Renames break automation bindings; forward compatibility has no handshake. *(chief-architect, lead-backend)*
- **Release pipeline never boots under real Supervisor before publish.** Already tracked as known-missing. Rollback UX for non-shell users is weak. *(product-manager, sre-reliability)*
- **Geometry and input validation are shallow** — no ring closure, coord ranges, vertex bounds, name uniqueness; enables silent wrong-location results and mild DoS. *(lead-backend, security-reviewer)*
- **Public-mirror workaround is a privacy-by-default failure.** Publishing `zones.json` to GitHub Pages / S3 permanently exposes precise home geometry to indexing, archiving, and US legal process; presented in DOCS.md without a privacy warning. *(security-reviewer, dpo)*
- **Editor loses unsaved work silently.** No `beforeunload` guard; HA shell navigation inside the ingress iframe discards in-progress edits with no prompt. *(lead-frontend)*
- **Editor is not screen-reader accessible.** Leaflet-draw toolbar icons lack `aria-label`; `<zone-entry>` shadow DOM breaks `for`/`id` label association. *(lead-frontend)*

## Conflicts

- **Security posture of default LAN exposure.** `product-manager` recommends defaulting `allow_all_ips: true` with an auto-generated `save_token` so the install path works out of the box. `security-reviewer` warns that plaintext HTTP serving home GPS geometry over the LAN is a HIGH-severity exposure and wants TLS + token gate on `GET` too. Both positions reconcile if the `/share` file-drop seam is adopted — the LAN HTTP server goes away for the common case.
- **Recommending the public-mirror workaround.** `product-manager` treats it as a documented fallback; `security-reviewer` wants it removed from docs as unsafe. Consensus next step: stop recommending it, fix upstream in the integration's SSRF guard.

## Per-specialist highlights

### chief-architect
- The HTTP seam inside one Supervisor box pays no rent. Replace with `/share/polygonal_zones/zones.json`.
- Fire an HA event (`polygonal_zones_updated`) on save so the integration stops polling.
- Write an ADR weighing an upstream contribution to HA core `zone` against the two-repo split.

### product-manager
- Target persona ≈ HA presence-automation power users, estimated 50–100k people globally.
- ~80% of installs will silently fail at the integration-URL step today. This is the adoption blocker.
- Consolidate the 0.2.x release churn (0.2.0 → 0.2.25 in rapid succession) before any HACS submission or forum positioning.

### security-reviewer
- **HIGH** — plaintext LAN HTTP + public-mirror workaround leak precise home geometry.
- **MEDIUM** — `GET /zones.json` is not token-gated even when `save_token` is set; `save_token` rate-limit keying collapses behind a trusted proxy.
- **LOW** — `frame-ancestors https://*.home-assistant.io` is the wrong origin; `'unsafe-inline'` in script-src weakens XSS containment.

### lead-backend
- ETag check has a TOCTOU window vs. disk; adequate for single-user today, wrong once the integration writes back.
- No stable zone ID — rename breaks every automation bound to the old name.
- No `If-None-Match` / `Last-Modified` support; every poll re-downloads and re-parses the whole file.
- `411 Length Required` is misreported as `413`.

### dpo
- **HIGH** — public-mirror workaround in DOCS.md is a GDPR Art. 25 (privacy-by-default) failure. Precise home polygon → permanently world-readable, indexed, retained on US-headquartered CDNs (CLOUD Act), no erasure path.
- **MEDIUM** — no documented retention / deletion for `zones.json`; HA snapshots silently carry the full history. UI-level delete does not purge backups.
- **LOW** — client IPs logged at `warning` on blocked requests; no retention policy. Application logs never contain polygon coordinates (already good).

### lead-frontend
- **HIGH** — no `beforeunload` / dirty-state guard. Tapping the HA sidebar while editing silently discards all unsaved zones. Worst on mobile, which is most sessions.
- **HIGH** — Leaflet-draw toolbar controls have no `aria-label`; `<zone-entry>` shadow DOM breaks label/input association. Editor is effectively unusable for screen-reader users (WCAG 4.1.2, 1.3.1).
- **MEDIUM** — `console.log` of `draw:created` GeoJSON leaks home coordinates to devtools. Network-error status auto-clears after 2s, hiding intermittent ingress failures. CDN-loaded Leaflet/leaflet-draw is render-blocking with no fallback. Default map centre is hard-coded to Groningen, NL.

### sre-reliability
- No Supervisor-level boot gate before release; CI tests amd64 under plain Docker only.
- Rollback requires Supervisor shell most home users don't have.
- aarch64 / armv7 are the dominant deployment targets but only amd64 is smoke-tested in CI.
- No staleness signal surfaced to the integration when the addon is down or degraded.

## Prioritized action list

### P0
1. **Fix the SSRF-blocked install path.** Open/track an opt-in `allow_private_urls` flag on the companion integration. Until it exists, the default install does not work for most users. *(product-manager, chief-architect, security-reviewer)*
2. **Stop recommending public mirrors (GitHub Pages / S3) in `DOCS.md`.** Home-geometry on unauth public URLs is the wrong happy path. *(security-reviewer)*
3. **Add stable per-zone `properties.id` (UUID) + `schema_version` at the FeatureCollection top level.** Unblocks rename-safe automations and future migration. *(lead-backend, chief-architect)*
4. **Gate `GET /zones.json` behind `save_token` when set (or when `allow_all_ips=true`).** Today writes are protected; reads of the sensitive asset are not. *(security-reviewer)*
5. **Add a Supervisor-level live boot gate before release** (self-hosted HA OS runner, or a manual sign-off step in the release template). *(sre-reliability)*
6. **Add a prominent privacy warning to the public-mirror workaround in DOCS.md.** Frame private reverse proxy as the primary path; public CDN as last resort with explicit warning. *(dpo)*
7. **Add a `beforeunload` dirty-state guard** in `map.js` and remove `console.log` of `draw:created` GeoJSON. *(lead-frontend)*
8. **Fix `<zone-entry>` label/input association** and add `aria-label` to leaflet-draw toolbar buttons. *(lead-frontend)*

### P1
9. **Prototype the `/share` file-drop seam** as an alternative to HTTP between addon and integration; fire an HA `polygonal_zones_updated` event on save. *(chief-architect, sre-reliability)*
10. **Tighten save-time validation:** closed rings, coordinate ranges, vertex/feature caps, name uniqueness. Return `422`. *(lead-backend, security-reviewer)*
11. **`If-None-Match` + `Last-Modified` on `GET /zones.json`** to cut polling cost and give a staleness signal. *(lead-backend, sre-reliability)*
12. **Document integration behaviour on 503 / timeout** and what the user observes in automations. *(sre-reliability)*
13. **Consolidate 0.2.x churn** into fewer milestone releases before any HACS / forum positioning. *(product-manager)*
14. **Self-host or `defer`-load Leaflet / leaflet-draw** to remove the unpkg CDN single point of failure. *(lead-frontend)*
15. **Replace the 2s error auto-clear** with a dismiss-on-next-action pattern so intermittent save failures don't disappear. *(lead-frontend)*
16. **Document HA snapshot coverage** — that `/data/polygonal_zones/zones.json` lives inside backups and UI-delete doesn't purge history. *(dpo)*

### P2
17. Migration importer from HA core circular zones → polygon approximations. *(chief-architect)*
18. QEMU arm64 boot probe in CI; GUI-based rollback path documented. *(sre-reliability)*
19. SSE / WebSocket push channel on the addon once stable zone IDs land. *(lead-backend)*
20. Fix `frame-ancestors`; remove `'unsafe-inline'` script-src. *(security-reviewer)*
21. Cut / defer tile-picker, responsive drawer, and AppArmor polish from v1 scope. *(product-manager)*
22. Persist map viewport (centre + zoom) per-user so first-run users outside NL don't land on Groningen. *(lead-frontend)*
23. Add a "Privacy and data" section to DOCS.md covering what the addon stores, what leaves the box, and controller responsibilities. *(dpo)*

## Out of scope

- Integration-repo source code (`MatthewHobbs/Homeassistant-polygonal-zones`) — only its documented contract was reviewed.
- Test coverage strategy (QA review not yet run).
- Visual / interaction design polish (product-designer not engaged).
