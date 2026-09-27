# ADR 0002: Clear the open backlog

- **Status:** Accepted (2026-09-27)
- **Context:** six findings were open in `BACKLOG.md`, one of them P1. I proposed how to clear them in RFC 0010 (see References) and chose its recommendation on each item that needed a decision.
- **North star:** the add-on does what its options say, the companion integration can read zones under every configuration the add-on recommends, and no finding in `BACKLOG.md` is left open without a plan row here.

## Decision

`save_token` stops gating reads of `/zones.json`, while `/trackers.json` keeps it. The three mechanical fixes go ahead without further decision. The zone overlay waits until the integration and I agree one set of containment fixtures.

| # | Step | Owner | Status | Evidence |
| --- | --- | --- | --- | --- |
| 1 | Scope the `/zones.json` read gate. A read is allowed through ingress, when `allow_all_ips` is on, or with a valid `X-Save-Token`. The token is accepted there but no longer required. `/trackers.json` keeps today's gate, so a set token still protects live positions. Tests include one that fails on today's `main`: a token set, `allow_all_ips` on, no header, `GET /zones.json` returns 200. Update the option description, `DOCS.md`, `CLAUDE.md`'s authorisation model and the changelog, with a version bump | this repo | Open | |
| 2 | Give each `/zones.json` rejection its own message (IP not allowed, token missing, token wrong) without revealing whether a token is configured. May ship in row 1's PR | this repo | Open | |
| 3 | Verify that releases built with `actions/attest-build-provenance` v4 produce attestations that verify | this repo | Done | 0.5.0 on both arches, see Verification |
| 4 | A scheduled, non-required check that runs the attestation step against a throwaway artefact, on the Supervisor pilot's pattern | this repo | Blocked | waits for ADR 0001 row 5, so the two nightly jobs do not change at once |
| 5 | Close the `release-merge.sh --dry-run` finding | this repo | Done | #61 fixed it on 2026-09-26; the entry is closed in this PR |
| 6 | One `draw_and_save` shared by `build.yml`'s standalone smoke and the Supervisor pilot. The pilot keeps its ingress-specific parts | this repo | Open | |
| 7 | Agree one containment fixture set with the companion integration, which both repos test their own code against in CI. No overlay build work starts before it is agreed. This ADR carries no authority into the integration repo | me, with Homeassistant-polygonal-zones | Open | |
| 8 | Point each open `BACKLOG.md` entry at its row here | this repo | Done | this PR |

Status is one of **Open**, **Done**, **Blocked**, **Dropped**. A **Done** row carries Evidence.

## Context

`save_token` is described as guarding `POST /save_zones`. Since 0.2.27 it also gates `GET /zones.json`: that release added a read gate that mirrors the save gate, so a token could unlock LAN reads with `allow_all_ips` off. The mirror also made a set token mandatory for reads when `allow_all_ips` is on. The integration can only be given a bare URL, so the configuration the add-on recommends when its port is exposed stops the integration reading zones.

RFC 0010 did not notice that `/trackers.json` uses the same read gate. It returns the live positions of the people named in `overlay_entities`. Relaxing the shared gate would have made those positions readable by anyone on the LAN whenever `allow_all_ips` is on, token or not. I decided that separately on 2026-09-27: only `/zones.json` changes.

The provenance finding said v4 was unproven until a release. By the time I decided, four releases had used v4. So I checked the latest one rather than dispatching a release by hand.

## Alternatives

Copied from RFC 0010 on 2026-09-27. This copy is the record; the doc can still be edited.

**`save_token` scope (RFC item 1).**

| Option | Cost | Risk |
| --- | --- | --- |
| A. Scope the token check to `POST /save_zones` only, matching the documented intent (chosen) | Low: one conditional, two tests | Low: `GET /zones.json` is still covered by the IP allowlist and by the port being unmapped by default |
| B. Leave the check as-is; fix only the description and give the integration a way to send a header | Medium: needs a change in the integration repo too, cross-repo dependency | Leaves the recommended add-on configuration broken until the integration ships that change |
| C. Add a separate, separately-documented read protection option distinct from `save_token` | Medium: new option, new docs, more tests | Medium: solves a threat model nobody has asked for yet, and adds surface for something already confusing |

**How far A reaches.** Not in the RFC; decided on 2026-09-27.

| Option | Effect |
| --- | --- |
| `/zones.json` only, token still accepted (chosen) | Fixes the integration. `/trackers.json` keeps requiring a set token. No privacy change |
| Both endpoints | `/trackers.json` also opens to the LAN under `allow_all_ips`, token or not |
| `/zones.json` only, token reads dropped | As the chosen option, but a token no longer unlocks `/zones.json` with `allow_all_ips` off, removing the 0.2.27 path |

**Provenance coverage (RFC item 2).** I chose A now and B after ADR 0001 row 5.

| Option | Cost | Risk |
| --- | --- | --- |
| A. Manually `workflow_dispatch` `release.yml` and verify the attestation with `gh attestation verify` before the next real release | Near zero, one-off | Only covers the next release, not the systemic gap |
| B. Add a scheduled, non-required workflow that runs the attestation step against a throwaway artifact, on the pattern already used by the Supervisor pilot (ADR 0001) | Medium: needs a disposable image and its cleanup from GHCR | Low: cannot block a merge or a release, since it never gates either |
| C. Restructure `release.yml` so attestation also runs as a reusable job from `build.yml`'s PR path in a dry-run mode | Highest: reshapes the workflow | Medium: attestation on a dry, untagged artifact may not behave like it does for a real tagged release, so a pass there could be false confidence |

**Containment maths for the zone overlay (RFC item 5).**

| Option | Cost | Risk |
| --- | --- | --- |
| A. Extract the containment rule into a shared set of test fixtures both repos assert against in CI, each keeping its own implementation (chosen) | Medium: fixtures to write and keep current in both repos | The two copies can still drift between releases if one repo's fixture run is skipped or its copy edited without the other |
| B. Have the editor call the integration's containment logic at runtime instead of reimplementing it | Higher: a new runtime dependency from the add-on on the integration, which does not exist today | Couples two independently versioned, independently released components; the integration is not known to expose a stable interface for this |
| C. Ship the editor's geometry module as the sole implementation and have the integration depend on it instead | Similar to B, inverted | Same coupling problem in the other direction, and the add-on is Docker-shipped, not something the integration can just import |

RFC items 3, 4 and 6 had one fix each and no options.

## Consequences

**Accepted:** with `allow_all_ips` on, anyone on the LAN can read `/zones.json` whether or not a token is set. Zones are shapes I drew, not anyone's position. The scheduled provenance check waits for the pilot's promotion, so a change to the attestation action between releases is still first exercised by a release.

**Watch:**
- `/zones.json` and `/trackers.json` will no longer share one read gate. A later change to either must not quietly re-merge them, or live positions lose the token.
- The overlay depends on the integration agreeing the fixtures. If it will not, this comes back here as a deviation.

## Verification (2026-09-27)

- **Read gate:** `authorise_read` in `app/main.py` serves both `/zones.json` and `/trackers.json`, and was added in 0.2.27 (09f2ebd). With a token set it returns `invalid_token` before it looks at `allow_all_ips`. That is the P1 finding, read from the code, not yet run.
- **Provenance:** `gh attestation verify` on `ghcr.io/matthewhobbs/{amd64,aarch64}-addon-polygonal_zones:0.5.0` with `--owner MatthewHobbs` exited 0 for both. Each attestation is SLSA provenance v1, signed by `release.yml` at `refs/tags/v0.5.0`, from commit dc56d79, which is the tag's commit. `release.yml` pins `attest-build-provenance` v4.2.2. A check that could fail: the same amd64 image verified against `--repo MatthewHobbs/Homeassistant-polygonal-zones` exited 1 with no attestation found.
- **Dry run:** #61 skips `wait_for_main_version` under `--dry-run`, and was checked against `--dry-run 60`.
- **Not established:** that the integration will adopt shared fixtures; row 1's behaviour, until its tests run.

## References

- RFC 0010: https://claude.ai/artifact/6VLwW8jjU2m6NWTeKJWYcn
- `BACKLOG.md` in this repo
- ADR 0001, row 5
