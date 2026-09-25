# ADR 0001 — Test the add-on under the current stable Supervisor and Core

- **Status:** Accepted (2026-09-25)
- **Context:** the owner's requirement of 2026-09-25 that add-ons are tested against the current general-release Home Assistant, and the RFC it produced (see References), decided as option A.
- **North star:** every release of this add-on has been installed, configured and used under the current stable Supervisor, with both the current stable Core and the oldest Core it declares, on both architectures users run, before it ships.

## Decision

Test the add-on in CI under a real Supervisor running the stable channel, using the official Home Assistant add-on devcontainer. Test it against both the current stable Core and the oldest Core the add-on declares it supports.

| # | Step | Owner | Status | Evidence |
| --- | --- | --- | --- | --- |
| 1 | Pilot: a manually triggered workflow running `ghcr.io/home-assistant/devcontainer` (apps variant, pinned by digest, bumped by Renovate) with `SUPERVISOR_CHANNEL=stable`. Install the add-on from `apps/local`, start it, set its options through the Supervisor API, and probe it through ingress. One job on `ubuntu-latest` (amd64), one on `ubuntu-24.04-arm` (aarch64). Record each job's runtime, and whether arm64 works at all | polygonal-zones | Open | |
| 2 | Each run reads `stable.json` and logs the expected and observed versions. The Supervisor must match stable in every leg. Core must match stable in the current-Core leg, and the declared floor in the floor leg (row 11). Any mismatch fails the run, so a silent fallback to dev or beta can't pass | polygonal-zones | Open | |
| 3 | Test the image that would ship. `config.yaml` names a published `image:`, so for an unreleased PR the Supervisor has to build locally, and it builds from `build.yaml`'s `build_from`, which still says 3.21 while releases use 3.24 from `base-images.yaml` (inferred, not yet observed). Either make `build.yaml` agree with `base-images.yaml` and enforce it in CI, or side-load the CI-built image under the tag the Supervisor expects. Row 1 establishes which works. Whichever route is taken, every run must prove, before any probe, that the running container was built from the commit under test (for example a commit-SHA label checked against the checkout). Otherwise the Supervisor can pull the published image and the run goes green on old code | polygonal-zones | Open | |
| 4 | Reuse the existing assertions: the smoke probes, then the Playwright draw-and-save through the ingress URL, which exercises #46's failure mode on the real path | polygonal-zones | Open | |
| 5 | Run it nightly and on demand, not yet required. After about two weeks, promote it if every failure had a known cause that wasn't flakiness. Promotion means both a required PR check and a job that `release.yml` runs before publishing, alongside tests, lint and build, because a tag-triggered release never sees a PR check. Promotion is the owner's call, recorded here | owner | Open | |
| 6 | Add an AppArmor compile check (`apparmor_parser -Q -K`, as a290 and r5 do). Enforcement stays in TESTING.md's manual HA OS layer until someone shows the devcontainer can enforce it | polygonal-zones | Open | |
| 7 | Tighten the standalone smoke: assert `uid=1001(app)` (CLAUDE.md says it does; `build.yml:161` only rejects uid 0), and fail on any traceback in the container log | polygonal-zones | Open | |
| 8 | Build and boot aarch64 on native `ubuntu-24.04-arm` instead of QEMU, and run Playwright on both arches | polygonal-zones | Open | |
| 9 | Update TESTING.md and EVALUATION.md, which still say only amd64 is booted, and describe the new layer | polygonal-zones | Open | |
| 10 | Declare the oldest supported HA in `config.yaml` (`homeassistant:`), which it doesn't do today. The value and the rule it must satisfy are set by the global `CLAUDE.md` ("Home Assistant: test against current stable"), which claude-config owns, and not by this ADR. Declare it only once row 11 has shown the add-on works on it. After that, the Supervisor won't offer the update to older installs, so the changelog must say so | polygonal-zones | Open | |
| 11 | Test that floor too: a second pilot leg with Core pinned to the declared minimum, under the current stable Supervisor, which users on an old Core still receive through auto-update. Row 1 establishes whether the devcontainer can pin Core | polygonal-zones | Open | |
| 12 | After row 10 lands, enforce the rule in CI: fail if `config.yaml`'s `homeassistant:` is missing, or breaks the global rule's bound, computed from current stable Core in `stable.json`. The floor is relative to a moving target, so without this check it can drift out of compliance with nobody noticing | polygonal-zones | Open | |
| 13 | Adopt the same approach in a290 and r5, including r5's Renovate rule for stable Core in a290. Accepting this ADR carries no authority into those repos: each needs its own session and the owner's approval there | a290, r5 | Blocked | awaits row 5 and a session in each repo |

Status is one of **Open**, **Done**, **Blocked**, **Dropped**. A **Done** row carries Evidence.

## Context

Users take Home Assistant updates through the normal path, so an add-on that works in isolation but not under the current Supervisor fails them on update. On 2026-09-25 this add-on's CI booted the container on both arches (aarch64 under QEMU) and ran Playwright on amd64, all standalone. Nothing installed it through a Supervisor, set its options through one, reached it through ingress, or confined it with AppArmor. EVALUATION.md already listed "No Supervisor-level boot gate". a290 and r5 don't boot their containers in CI at all.

## Alternatives

Copied from the RFC's options table as it stood when it was decided.

| Option | What it tests | Cost | Risk |
| --- | --- | --- | --- |
| **A. Official add-on devcontainer, stable channel** (chosen) | A real Supervisor and Core in `ghcr.io/home-assistant/devcontainer` (apps variant), with `SUPERVISOR_CHANNEL=stable` set explicitly (the default is `dev`, and the template uses `beta`). Installs the add-on from `apps/local`, sets options through the Supervisor API, and can open the ingress panel and run Playwright through Core | About 7 to 10 minutes per arch per run, based on one third-party repo. Free on public repos. One privileged job per arch; no KVM needed | AppArmor probably can't be enforced: the one working example had to turn it off. Discovering local add-ons is known to be fragile (supervisor#3976). Not yet shown to boot on `ubuntu-24.04-arm` |
| B. Supervisor container run directly | What the Supervisor repo's own CI does: install, start, back up and restore an add-on through `ha apps` | Similar to A, with less setup | AppArmor is never applied (no os-agent, so it falls back to unconfined). That CI runs the dev channel; stable is inferred from source, not run |
| C. HA OS in QEMU | The real OS, with real AppArmor | Highest: large images, slow boots, and getting a local add-on in is hard. There is no KVM on arm runners, so aarch64 would be fully emulated | No working CI example found. Likely slow and flaky |
| D. Cheap wins only | No Supervisor. Tighten what already runs | Low | Leaves the requirement unmet: Supervisor option handling, ingress and AppArmor stay manual-only |

## Consequences

**Accepted:** users on HA older than the declared floor stop receiving add-on updates. Under the owner's rule, the floor is never newer than the previous month's `.1`, so every install from that release onward keeps getting updates. CI gets slower and more complex. There are two privileged jobs of roughly 7 to 10 minutes each, plus a devcontainer digest for Renovate to keep current. AppArmor enforcement stays a manual check. Until row 5 promotes the check to required, a release can ship without the Supervisor-level test. The north star is reached at promotion, not before.

**Watch:**
- Discovering local add-ons is known to be fragile, and one other repo abandoned a similar job after 5 failures in 5 runs. Row 5's gate exists so a flaky check never becomes required.
- If the devcontainer can't boot on `ubuntu-24.04-arm`, the aarch64 half needs another route, which is a deviation to bring back here.
- The add-on and the companion integration should keep their floors in step. If either raises its floor, the other should follow, or users end up with an add-on that installs on an HA its integration won't support, or the reverse.

## Verification (2026-09-25)

- **Current stable** was read from `stable.json`: Supervisor 2026.09.2, Core 2026.9.3, OS 18.3. A missing channel or a wrong field would have returned nothing or a different shape.
- **Channel defaults** were read in the devcontainer source (`common/rootfs_supervisor/etc/supervisor_scripts/common`: `SUPERVISOR_CHANNEL` defaults to `dev`) and the Supervisor source (`validate.py:168` defaults to stable; `bootstrap.py:113-114`, where `SUPERVISOR_DEV` forces dev). This is why row 2 checks the running versions rather than trusting the setting.
- **CI today:** the "Build (aarch64)" job on #49 printed `OK /healthz`, `OK /zones.json shape` and `OK /save_zones round-trip`, which confirms CI boots aarch64 under QEMU. Playwright's `if: matrix.arch == 'amd64'` confirms the browser test is amd64-only.
- **Published aarch64 0.4.1 image:** booted natively on Apple Silicon (`uname -m` inside it gave `aarch64`, against an amd64 control that gave `x86_64`) and passed every smoke check and the browser test on both origins. Stopping it then made `/healthz` fail, so the checks could fail. The same stop also exited 137 and logged "Web service exited with status 256", which is unexplained and seen once.
- **Not established:** that A boots on `ubuntu-24.04-arm`; that AppArmor can be enforced under A; runtime and flakiness for this add-on; that the Supervisor builds a local add-on from `build.yaml`'s base (row 3).

## References

- RFC (unnumbered; this repo is not yet data-classified): https://claude.ai/code/artifact/6364efdb-f32e-4c4c-bd26-12ccd9a36111
- Stable channel: https://version.home-assistant.io/stable.json
- Working example of option A: https://github.com/brianbaggs35/ha-blink-clip-downloader/blob/main/.github/workflows/ha-integration.yaml
- TESTING.md (manual HA OS layer), docs/EVALUATION.md
