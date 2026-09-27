# Backlog

Findings are logged with severity (P0 blocking / P1 this sprint / P2 later), the component that
owns the fix, and the evidence that produced them. Items whose fix lands in the companion
[integration](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones) are tracked in that
repo's `BACKLOG.md` and cross-referenced here where the two interact.

---

## `test_trackers_json_returns_only_opted_in_entities` asserts an order the pool doesn't guarantee (2026-09-27) — OPEN, P3, tests only

**Component:** `polygonal_zones_editor/tests/test_main.py`, `app/main.py` (`_OVERLAY_POOL`, `_gather`)

Found while running the full suite locally (macOS/arm64, Python 3.12) ahead of an unrelated PR
(ADR 0002 row 1) — **not caused by that PR**: reproduces identically on `main` at `3913984` with
no changes checked out. Fails deterministically (3/3) in a full `pytest` run, passes reliably
(5/5) run in isolation with `-k`.

The test asserts the **order `_fetch_state` was called** in (`asked`), reflecting which of
`_OVERLAY_POOL`'s worker threads happened to pick up which submitted future first.
`concurrent.futures.ThreadPoolExecutor` does not guarantee call order matches submission order,
only that both futures eventually run; `_OVERLAY_POOL` is a module-level pool shared across the
whole pytest process (by design — see its comment in `main.py`), so which worker is already
"warm" and grabs a task first depends on scheduling noise from whatever ran immediately before
in the same process. In isolation the pool's threads start fresh and happen to pick up in
submission order; under the full suite's load they don't reliably.

**What's established:** the race exists and is reproducible on this platform, on unmodified
`main`. **What's not established:** why it doesn't (or hasn't yet) reproduced on the remote
Linux CI runners — `gh run list` shows `pytest` green on `main` at this commit. That could be
timing luck, a scheduler difference, or something else; no cause is claimed here beyond what was
directly observed.

**Fix, not done here (out of scope for the PR that found it):** stop asserting call order for
concurrently-dispatched work — assert the *set* of entities asked for (already covered by a
separate assertion two lines below in the same test), or synchronise the fakes so order is
actually deterministic (e.g. a fake that blocks until both entities have been requested before
either returns).

---

## The editor mismeasures zones with holes (2026-09-27) — FIXED

**Fixed** (ADR 0002 row 12): `app/static/js/trackers.js`'s `pz_layer_rings` flattened every ring
(outer and holes alike) into one list, so a zone's area added its holes instead of subtracting
them, and a tracker inside a hole read as inside the zone. Replaced with `pz_layer_polygons`,
which groups each polygon's own outer ring with its own holes (a `MultiPolygon` keeps each part's
holes scoped to that part, never leaking into another part). Area now subtracts each polygon's
holes from its outer ring; a point counts as inside only when it is in the outer ring and outside
every hole. Verified live against a real running container (a donut-shaped zone: correct
hole-subtracted area, a point in the hole reads outside, a point in the ring reads inside) and
with `polygonal_zones_editor/tests/test_trackers.js` (`node --test`, wired into `just ci` and
`test.yml`) — the first automated JS test in this repo, since geometry.js/trackers.js were
already written to be Node-testable (`module.exports`) but nothing ran them before now.

---

## The draw-and-save browser check exists twice (2026-09-25) — FIXED

**Fixed** (ADR 0002 row 8): `scripts/draw_and_save.py` now holds the one `draw_and_save` (plus
`draw_rectangle_and_save`, added alongside it in row 11 rather than left to duplicate too).
`.github/workflows/build.yml`'s Playwright step and `scripts/supervisor_probe.py` both import it;
the pilot's ingress-specific parts (Core login, the ingress session, the option check) stayed in
`supervisor_probe.py`, and `scripts/supervisor-pilot.sh` mounts the new file alongside `probe.py`
so the import resolves inside the container. Verified live against a real running add-on, not
just read from the diff.

---

## `release-merge.sh --dry-run` always fails on a version-bump PR (2026-09-25) — FIXED

**Fixed 2026-09-26 by #61:** `wait_for_main_version` is skipped under `--dry-run`. **Regression check added** (ADR 0002 row 7): `scripts/test-release-merge.sh`, run by `just ci` and `test.yml`.

**Component:** `scripts/release-merge.sh`

On the bump path, `tag_and_watch` calls `wait_for_main_version` (line 121) whether or not it is a
dry run. That function (lines 107-115) polls `main`'s `config.yaml` for the new version. A dry run
never merges, so `main` still has the old version, and after 30 seconds the preview fails:

```
STATUS:FAILED post-merge-version-mismatch: main=0.4.0 expected=0.4.1
```

Seen on PR #48 (0.4.0 → 0.4.1). Every pre-merge check had already passed: PR state, required
checks, mergeability and the version bump. The real run then released v0.4.1 cleanly, because it
merges before this check. So `--dry-run` cannot preview a release, which is the case it exists
for. The real run is unaffected.

**Fix:** skip `wait_for_main_version` when `DRY_RUN=1` and log what it would wait for, as the merge
and tag steps already do. Cover it with a check that fails on today's script.

---

## The release path's action bumps are unverified by any PR check (2026-09-05) — OPEN, P2

**Plan:** ADR 0002 rows 4 and 5. Row 4 is done: the 0.5.0 attestations, made with v4.2.2, verify on both arches. The scheduled check (row 5) waits for ADR 0001 row 5.

**Component:** `.github/workflows/release.yml`

PR #30 bumped eight actions. Seven are exercised by `build.yml` / `lint.yml` / `test.yml` on every
pull request. One is not:

```
- uses: actions/attest-build-provenance@977bb373...  # v3
+ uses: actions/attest-build-provenance@4d101475...  # v4.2.2
```

That action appears **only** in `release.yml`, which triggers on `v*` tags and `workflow_dispatch`.
No pull-request check runs it. So #30 going green across all six required checks said nothing at
all about the riskiest line in it — and this is a **major** version jump, v3 → v4.

It matters more here than a normal action bump because provenance is load-bearing: `config.yaml`
documents Sigstore attestation as *the* image-integrity mechanism, with the `codenotary:` key
deliberately unset because CAS is discontinued.

```
gh attestation verify oci://ghcr.io/matthewhobbs/<arch>-addon-polygonal_zones:<version> \
  --owner MatthewHobbs
```

If v4 changed the attestation's shape or subject handling, the first symptom is a failed or
unverifiable **release** — the most expensive place to find out, and the one moment when rolling
back is hardest.

**Nothing is known to be broken.** This is a coverage gap, not a defect.

**Before the next release:** dispatch `release.yml` manually via `workflow_dispatch` and verify the
resulting attestation with the command above, rather than discovering the answer during a real tag.

**Longer term:** cover the attestation step against a throwaway artifact on a schedule, so release
tooling stops being the one code path that only production exercises. `docs/RUNBOOK.md` already
covers partial-release recovery; this is about not needing it.

---

## `save_token` gates reads as well as writes, contrary to its own description (2026-09-05) — FIXED 2026-09-27, P1

**Plan:** ADR 0002 row 1 done, row 2 merged upstream but not yet released. This was not a bug: 0.2.27 gated reads on purpose to protect zone geometry, and only the description lagged. The gate stays; the description is corrected in `translations/en.yaml` and `DOCS.md` (row 1). The integration's own `zone_source_token` option, bound to the add-on's origin, merged in that repo's PR #95 (2026-09-27) but not yet in a tagged release — until one ships, the integration can only read a token-protected add-on with `save_token` left empty. The **Fix** below, scoping the check to saves, was considered and rejected.

Confirmed under the Supervisor pilot (2026-09-27, `cmd_probe` in `scripts/supervisor-pilot.sh`): a same-host integration reaching the add-on does **not** go through ingress — ingress is a browser-session proxy a background component cannot use. Core can reach the add-on directly over the internal Supervisor network by its slug-derived hostname, with no LAN port and no ingress, but that request arrives from the Supervisor's internal gateway address, not the ingress sidecar, so it is blocked like any other non-ingress client unless `allow_all_ips` is on. See the new finding below.

**Component:** `app/main.py` (`IPAllowMiddleware` / auth layer) + `config.yaml` option description

The Supervisor option describes `save_token` as protecting one route:

> When set, POST /save_zones requires the header X-Save-Token:&lt;value&gt; on any non-ingress
> request. The addon's own Save button keeps working because it goes through ingress. Leave empty
> to disable. Never logged.

Observed behaviour is broader: with a token set and `allow_all_ips: true`, **`GET /zones.json`
also returns 401**, with the body `{"error":"missing or invalid X-Save-Token"}`.

```
GET /              200        <- LAN access confirmed working
GET /zones.json    401
GET /zones.json  + X-Save-Token: <token>   200 (3805 bytes)
```

**Why it matters beyond the wrong sentence:** the companion integration's config flow accepts bare
URLs only and cannot send a header (integration backlog, P1). So enabling `save_token` — which
`config.yaml`'s own comment recommends whenever the port is exposed — makes the add-on unreadable
by the integration it exists to serve. The documented, recommended configuration does not work.

This cost roughly an hour of diagnosis here, because the description sends you looking at write
protection while a read is failing, and because the 401 body names a *save* token on a GET.

**Fix:** scope the token check to mutating methods (`POST /save_zones`), matching the documented
intent. `GET /zones.json` is then protected by the IP allowlist and by the port being unmapped by
default, which is the posture `config.yaml` already describes. If read protection is genuinely
wanted, it needs to be a separate, separately-documented option — and the integration needs a way
to supply the credential before it is switched on by anyone.

**Tests:** a case asserting `GET /zones.json` succeeds with a token configured and no header sent,
and one asserting `POST /save_zones` still 401s in the same state.

---

## No dedicated trust for Core's own traffic on the internal Supervisor network (2026-09-27) — DECIDED: leave as is

**Component:** `app/const.py` (`ALLOWED_IPS`), `app/main.py` (`IPAllowMiddleware`)

Found while confirming ADR 0002 row 1 under the Supervisor pilot: Core reaches the add-on
directly over the internal `hassio` Docker network (by the add-on's slug-derived hostname, no
LAN port, no ingress) — but the request arrives from the Supervisor's bridge gateway address
(`172.30.32.1` in the pilot), not the ingress sidecar (`172.30.32.2`), so `ALLOWED_IPS`
does not recognise it. Today that path is indistinguishable from any other non-ingress client:
it needs `allow_all_ips: true` (or a token) exactly like a real LAN client would, even though
the traffic never left the host.

**Decided (owner, 2026-09-27):** leave it as is. `allow_all_ips` stays the only opt-in for
same-host integration traffic, exactly as ADR 0002 row 1 documents; no new internal-trust
option. That gateway address is a Supervisor implementation detail, not a stable public
contract, and trusting it would need its own scrutiny (does it ever change per install? per
Supervisor version? is it spoofable by another add-on on the same bridge?) for a gain
`save_token` already covers, since a configured token authorizes without `allow_all_ips` at
all. Not revisited unless something changes that trade-off.

---

## The zone editor cannot show why a zone is wrong (2026-09-05) — SHIPPED

**Shipped in 0.4.0:** `geometry.js`, the tracker overlay, zone areas, Leaflet-Geoman and `/trackers.json`. The editor is measure-only by rule and never states a match, so shared containment fixtures are not needed unless that changes: ADR 0002 row 9. **Shipped in 0.5.2** (ADR 0002 row 11): rectangle drawing, turned on and verified end to end; edit handles scoped to the selected zone, midpoint insertion and right-click vertex deletion were already working as Geoman's own defaults, verified live rather than assumed.

**Component:** `app/static/` frontend + a new read-only backend route

Drawing zones accurately today is guesswork: the editor renders polygons over imagery but shows
nothing about the entities those polygons are supposed to classify. Every diagnostic question —
*is the car inside?*, *by how much?*, *which zones overlap here?* — has to be answered outside the
tool, from HA's debug log.

Two live faults in this installation were invisible in the editor and obvious the moment position
was overlaid on the map:

- a vehicle sitting **1.3 m** inside the boundary, matching nothing, because its GPS source reports
  `gps_accuracy: 0` (integration backlog, P0)
- a vehicle **parked outside** matching an indoor `Kitchen` zone of 31 m², because a 5 m accuracy
  ring inflates a 4 m room enough to swallow the fix

Neither is discoverable from a polygon drawn on a photo.

**Scope agreed with the maintainer (2026-09-05):**

1. **`js/geometry.js`** — pure, dependency-free `ringAreaM2`, `pointInRing`, `distanceToRingM`.
   Unit-testable without a browser, and the natural home for the containment rule.
2. **Tracker overlay** — markers plus accuracy rings for selected entities, with a per-entity
   readout: which zones match, which matched *only* because accuracy inflated them, and metres to
   the nearest edge.
3. **Zone list gains area in m²**, and edit handles scope to the selected zone only (thirteen
   polygons' worth of vertex handles at once is unreadable).
4. **Leaflet-Draw → Leaflet-Geoman**, re-vendored under `vendor/`, for rectangle drawing, midpoint
   vertex insertion and right-click vertex deletion.

**Backend:** a new read-only route returning position/accuracy for a configured entity list. Needs
`homeassistant_api: true` in `config.yaml`, which widens the add-on's Supervisor privileges — call
that out in `DOCS.md`.

**Privacy — decided, not optional:** the overlay plots people's live positions in a UI that is
LAN-reachable whenever `allow_all_ips` is on, and which (per the item above) should be *unauthenticated*
for reads. So the entity list is **explicit opt-in**: a new option naming entity_ids, empty by
default, nothing plotted until named. Do not default to "every `device_tracker` with coordinates".

**The one real design risk:** if the editor ships its own containment maths it becomes a second
source of truth that can disagree with the integration. A prototype already diverged — it matched a
zone at 3.8 m against a 5 m accuracy ring where the integration did not, because the integration
tie-breaks on `distance_to_exterior` and the prototype sorted by area. Extract the rule once,
document it in both repos, and test both implementations against the same fixtures — or have the
editor call the integration rather than reimplement it.

A working prototype exists (single-file, Leaflet + Geoman, satellite tiles, live overlay and
clearance readout) and can be lifted from rather than rewritten.

---

## `zones.json` 401 body names the wrong credential (2026-09-05) — FIXED

**Fixed** (ADR 0002 row 3): every authorisation failure on `/zones.json`, `/trackers.json` and
`/save_zones` now gets the identical `403` / `{"error": "not authorised"}` / `Cache-Control:
no-store` response, whatever the reason — a rejected read no longer names a *save* token, or any
token at all. The precise reason (`not_allowed` / `token_missing` / `token_wrong`) goes to the
add-on log only, via `_AUTH_REJECTION_LOG_REASON` in `app/main.py`. Pinned by
`test_uniform_auth_rejection_across_all_three_endpoints`, covering all three endpoints with a
token set and with none, per the row's own ask.

**Component:** `app/main.py`

A rejected `GET /zones.json` returns `{"error":"missing or invalid X-Save-Token"}`. Even once the
scoping bug above is fixed, a *read* rejection reporting a **save** token is actively misleading —
it points the reader at the write path. Worth a distinct message per rejection reason (IP not
allowed / token missing / token wrong), none of which should leak whether a token is configured.

---
