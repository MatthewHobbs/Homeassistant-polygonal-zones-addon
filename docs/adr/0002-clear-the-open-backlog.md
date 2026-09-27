# ADR 0002: Clear the open backlog

- **Status:** Accepted (2026-09-27)
- **Context:** six findings were open in `BACKLOG.md`, one of them P1. I proposed how to clear them in RFC 0010 (see References). Two of its premises turned out to be wrong, so on two items I decided against its recommendation.
- **North star:** the add-on does what its options say, the companion integration can read zones under every configuration the add-on recommends, and no finding in `BACKLOG.md` is left open without a plan row here.

## Decision

`save_token` keeps protecting reads of `/zones.json`, and its description is corrected to say so. The integration learns to send the token. The zone editor stays measure-only. The mechanical fixes go ahead.

| # | Step | Owner | Status | Evidence |
| --- | --- | --- | --- | --- |
| 1 | Correct `save_token`'s description in the add-on's options to say the token guards `GET /zones.json` and `GET /trackers.json` as well as `POST /save_zones`. `DOCS.md` already says it covers `/zones.json`; add `/trackers.json`. Establish under the Supervisor pilot how an integration on the same host actually reaches the add-on, since `DOCS.md` says it goes through ingress and that has not been tested, and correct the docs if it does not. Say plainly that until the integration can send the token (row 2), it can read over the LAN only with `save_token` empty, and that an empty token also leaves `POST /save_zones` open to the LAN under `allow_all_ips`. State the trade; do not recommend it. Say too that with a token set the whole editor works only through ingress: it loads zones, saves them and polls trackers without a token, so a direct LAN visit can do none of them. The gate is already pinned by `test_zones_json_requires_token_when_set_and_lan_request`. Changelog and version bump | this repo | Open | |
| 2 | The integration sends `X-Save-Token` when reading zones. The token can also save, so it is stored against the one add-on origin it belongs to and never sent to another, including across a redirect. This ADR carries no authority into that repo | Homeassistant-polygonal-zones | Blocked | needs work opened in that repo, under its own approval |
| 3 | Every authorisation failure on `/zones.json`, `/trackers.json` and `/save_zones` gets the same status, body and headers, so a client cannot tell whether a token is configured. The precise reason (IP not allowed, token missing, token wrong) goes to the add-on log only. A test covers all three endpoints with a token set and with none | this repo | Open | |
| 4 | Verify that releases built with `actions/attest-build-provenance` v4 produce attestations that verify | this repo | Done | 0.5.0 on both arches, see Verification |
| 5 | A scheduled, non-required check that runs the attestation step against a throwaway artefact, on the Supervisor pilot's pattern | this repo | Blocked | waits for ADR 0001 row 5, so the two nightly jobs do not change at once |
| 6 | Fix `release-merge.sh --dry-run` on a version bump | this repo | Done | #61, 2026-09-26 |
| 7 | A regression check for row 6 that fails when the dry run waits for `main`, as the finding asked. #61 was checked by hand only | this repo | Open | |
| 8 | One `draw_and_save` shared by `build.yml`'s standalone smoke and the Supervisor pilot. The pilot keeps its ingress-specific parts | this repo | Open | |
| 9 | The editor stays measure-only: it shows inside, distance to the edge and area, and never states which zone the integration would match. Shared containment fixtures with the integration are needed only if that ever changes | this repo | Done | this ADR; the rule is already stated in `app/static/js/geometry.js` |
| 10 | Point each open `BACKLOG.md` entry at its row here | this repo | Done | this PR |
| 11 | Check the rest of the editor scope agreed on 2026-09-05, and ship or drop each part with a reason: rectangle drawing, switched off in #36 with no reason given; edit handles scoped to the selected zone; midpoint vertex insertion; right-click vertex deletion | this repo | Open | |
| 12 | Measure zones with holes correctly: area subtracts inner rings, and a point counts as inside only when it is in an outer ring and not in one of its holes. Include a test with a hole that fails today. The validator accepts holes, though the editor cannot draw them | this repo | Open | |

Status is one of **Open**, **Done**, **Blocked**, **Dropped**. A **Done** row carries Evidence.

## Context

**`save_token` and reads.** The finding said the token gates `GET /zones.json` contrary to its description, and treated that as a bug. RFC 0010 followed it. It is not a bug. Release 0.2.27 gated reads on purpose: its changelog says zone geometry, meaning my home, workplace and school runs, had been less protected than writes. Only the description was never updated. I first chose the RFC's option A, to stop gating reads. On learning of 0.2.27 I chose B the same day. The finding's real harm is that the integration can only be given a bare URL, so it cannot read zones once a token is set. The cure belongs in the integration, not in weaker protection here. Under option A I had also limited the change to `/zones.json`, because `/trackers.json` shares the read gate and serves live positions. Under B that question does not arise.

**The zone overlay.** The finding and the RFC described the overlay as unbuilt. Most of it shipped in 0.4.0, on the day the finding was written: `geometry.js`, the tracker overlay, zone areas, Leaflet-Geoman and `/trackers.json`. Rectangle drawing did not: #36 switched it off. Row 11 covers what is left. `geometry.js` measures and deliberately does not reimplement the integration's matching rules, so the second source of truth the RFC worried about was designed out. I chose to record that as a rule rather than build shared fixtures.

**Provenance.** The finding said v4 was unproven until a release. Every release since 0.4.0 had used it, so I checked the latest one rather than dispatching a release by hand.

## Alternatives

**`save_token` and reads.** RFC 0010's options, copied on 2026-09-27; this copy is the record and the doc can still be edited. Then the options I weighed once I knew about 0.2.27.

| Option | Cost | Risk |
| --- | --- | --- |
| A. Scope the token check to `POST /save_zones` only, matching the documented intent | Low: one conditional, two tests | Low: `GET /zones.json` is still covered by the IP allowlist and by the port being unmapped by default |
| B. Leave the check as-is; fix only the description and give the integration a way to send a header (chosen) | Medium: needs a change in the integration repo too, cross-repo dependency | Leaves the recommended add-on configuration broken until the integration ships that change |
| C. Add a separate, separately-documented read protection option distinct from `save_token` | Medium: new option, new docs, more tests | Medium: solves a threat model nobody has asked for yet, and adds surface for something already confusing |

| Option, knowing 0.2.27 | Effect |
| --- | --- |
| Keep the gate, fix the description (chosen) | Keeps 0.2.27's protection. The integration needs a change before it can read with a token set |
| Relax `/zones.json` anyway | Fixes the integration today, and undoes 0.2.27: zone shapes readable on the LAN under `allow_all_ips`, token or not |
| Also accept the token as a query parameter | No integration change, but the token travels in URLs and can land in logs |

**Provenance coverage.** From RFC 0010; I chose A now and B after ADR 0001 row 5.

| Option | Cost | Risk |
| --- | --- | --- |
| A. Manually `workflow_dispatch` `release.yml` and verify the attestation with `gh attestation verify` before the next real release | Near zero, one-off | Only covers the next release, not the systemic gap |
| B. Add a scheduled, non-required workflow that runs the attestation step against a throwaway artifact, on the pattern already used by the Supervisor pilot (ADR 0001) | Medium: needs a disposable image and its cleanup from GHCR | Low: cannot block a merge or a release, since it never gates either |
| C. Restructure `release.yml` so attestation also runs as a reusable job from `build.yml`'s PR path in a dry-run mode | Highest: reshapes the workflow | Medium: attestation on a dry, untagged artifact may not behave like it does for a real tagged release, so a pass there could be false confidence |

**Containment maths.** RFC 0010 offered three options for an overlay it thought unbuilt: shared test fixtures, calling the integration at runtime, or the integration depending on the editor's code. I chose none, because the shipped editor is measure-only (row 9).

| Option, knowing the overlay shipped | Effect |
| --- | --- |
| Record measure-only as a rule (chosen) | No cross-repo work. Fixtures become necessary only if the editor ever states a match |
| Shared fixtures anyway | Both repos test the geometry primitives against one set. Work in two repos for a risk the design already avoids |

RFC items 3, 4 and 6 had one fix each and no options. Item 6 as written asked for a distinct message per reason that also hid whether a token is configured, which cannot both hold. Row 3 keeps the second, across all three endpoints, since each one's status would otherwise reveal the same thing.

## Consequences

**Accepted:** until row 2 lands in the integration's repo, someone running it over the LAN chooses between the integration reading zones and saves being protected. That is the position today; this ADR does not change it, and row 1 makes the docs say so. The scheduled provenance check waits for the pilot's promotion, so a change to the attestation action between releases is still first exercised by a release.

**Watch:**
- Row 2 has no owner in that repo yet. If it stalls, open saves become the norm for anyone using the integration over the LAN. If that looks likely, a separate read credential (RFC option C) is the fallback, and it comes back here.
- The editor's measure-only rule is a comment and this row. A change that makes the editor state a match needs shared fixtures first.

## Verification (2026-09-27)

- **Read gate:** `authorise_read` in `app/main.py` serves `/zones.json` and `/trackers.json`, and was added in 0.2.27 (09f2ebd). With a token set it returns `invalid_token` before it looks at `allow_all_ips`. The 0.2.27 changelog entry states the gate was deliberate. The option description in `translations/en.yaml` mentions only `POST /save_zones`, while `DOCS.md` already says the token covers `/zones.json` reads.
- **Overlay:** `app/static/js/geometry.js` has area, point-in-ring, edge distance and a `pz_measure` whose comment says it makes no claim about what the integration would match. `vendor/leaflet-geoman` is present. The 0.4.0 changelog lists the area readout, the Geoman swap, the tracker overlay and `overlay_entities`. `map.js` sets `drawRectangle: false`, added in #36 (3d15e37) with no reason given. Not checked: edit-handle scoping, midpoint insertion and right-click deletion.
- **Provenance:** `gh attestation verify` on `ghcr.io/matthewhobbs/{amd64,aarch64}-addon-polygonal_zones:0.5.0` with `--owner MatthewHobbs` exited 0 for both. Each attestation is SLSA provenance v1, signed by `release.yml` at `refs/tags/v0.5.0`, from commit dc56d79, which is the tag's commit. A check that could fail: the same amd64 image verified against `--repo MatthewHobbs/Homeassistant-polygonal-zones` exited 1 with no attestation found.
- **Releases on v4:** `release.yml` at each tag from v0.4.0 to v0.5.0 pins `attest-build-provenance` v4.2.2, introduced by #30 on 2026-09-05.
- **Dry run:** #61 skips `wait_for_main_version` under `--dry-run`, and was checked against `--dry-run 60`.
- **Holes:** `_validate_polygon_coordinates` accepts any number of rings per Polygon. `pz_layer_rings` in `trackers.js` flattens every ring, `pz_layer_area_m2` adds their areas, and `pz_measure_zone` reports inside if the point is in any ring. Read from the code, not run.
- **Editor requests:** `map.js` fetches `/zones.json` and posts `/save_zones`, and `trackers.js` fetches `/trackers.json`. None sends `X-Save-Token`, so with a token set a direct LAN visit is refused on all three.
- **Status as a signal:** all three endpoints answer 401 when a token is set and 403 when none is, whatever the body says.
- **Not established:** that an integration on the same host reaches the add-on through ingress, as `DOCS.md` says; only requests from 172.30.32.2 count as ingress, and the integration fetches a URL it is given. That the integration will take row 2; any automated check of #61's fix (row 7).

## References

- RFC 0010: https://claude.ai/artifact/6VLwW8jjU2m6NWTeKJWYcn
- `BACKLOG.md` and `polygonal_zones_editor/CHANGELOG.md` (0.2.27, 0.4.0) in this repo
- ADR 0001, row 5
