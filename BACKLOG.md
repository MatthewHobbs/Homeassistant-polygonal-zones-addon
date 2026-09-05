# Backlog

Findings are logged with severity (P0 blocking / P1 this sprint / P2 later), the component that
owns the fix, and the evidence that produced them. Items whose fix lands in the companion
[integration](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones) are tracked in that
repo's `BACKLOG.md` and cross-referenced here where the two interact.

---

## The release path's action bumps are unverified by any PR check (2026-09-05) — OPEN, P2

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

## `save_token` gates reads as well as writes, contrary to its own description (2026-09-05) — OPEN, P1

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

## The zone editor cannot show why a zone is wrong (2026-09-05) — OPEN, P2 (feature)

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

## `zones.json` 401 body names the wrong credential (2026-09-05) — OPEN, P3

**Component:** `app/main.py`

A rejected `GET /zones.json` returns `{"error":"missing or invalid X-Save-Token"}`. Even once the
scoping bug above is fixed, a *read* rejection reporting a **save** token is actively misleading —
it points the reader at the write path. Worth a distinct message per rejection reason (IP not
allowed / token missing / token wrong), none of which should leak whether a token is configured.

---
