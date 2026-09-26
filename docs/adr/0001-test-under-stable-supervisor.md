# ADR 0001: Test the add-on under the current stable Supervisor and Core

- **Status:** Accepted (2026-09-25)
- **Context:** my requirement of 2026-09-25 that add-ons are tested against the current general release of Home Assistant, and the RFC that followed (see References). I chose option A.
- **Amended (2026-09-26):** the pilot's first runs on GitHub found that the shipped AppArmor profile fails on modern AppArmor. I fixed that and released it as 0.4.2. The pilot enforces AppArmor after all. See the amendment below.
- **North star:** every release of this add-on has been installed, configured and used under the current stable Supervisor, with both the current stable Core and the oldest Core it declares, on both architectures my users run, before it ships.

## Decision

I test the add-on in CI under a real Supervisor on the stable channel, using the official Home Assistant add-on devcontainer. I test it against both the current stable Core and the oldest Core the add-on declares it supports.

| # | Step | Owner | Status | Evidence |
| --- | --- | --- | --- | --- |
| 1 | Pilot. A manually triggered workflow runs `ghcr.io/home-assistant/devcontainer` (the apps variant, pinned by digest and bumped by Renovate) with `SUPERVISOR_CHANNEL=stable`. It installs the add-on from `apps/local`, starts it, sets its options through the Supervisor API and probes it through ingress. One job runs on `ubuntu-latest` (amd64) and one on `ubuntu-24.04-arm` (aarch64). It records each job's runtime and whether arm64 works at all. Result: it boots on `ubuntu-24.04-arm`, and a job takes 133 to 188 seconds with a warm cache | this repo | Done | #52, run 36229742351 |
| 2 | Each run reads `stable.json` and logs the expected and observed versions. The Supervisor must match stable in every leg. Core must match stable in the current-Core leg and the declared floor in the floor leg (row 11). Any mismatch fails the run, so a silent fallback to dev or beta cannot pass | this repo | Done | #52, run 36229742351 |
| 3 | Test the image that would ship. `config.yaml` names a published `image:`, so for an unreleased change the Supervisor has to be given a locally built image. Either make `build.yaml` agree with `base-images.yaml` and enforce that in CI, or side-load the CI-built image under the tag the Supervisor expects. Row 1 establishes which works. Whichever route is taken, every run must prove, before any probe, that the running container was built from the commit under test, for example with a commit label checked against the checkout. Otherwise the Supervisor can pull the published image and the run goes green on old code | this repo | Done | #52, run 36229742351 |
| 4 | Reuse the existing assertions: the smoke probes, then the Playwright draw-and-save through the ingress URL, which exercises #46's failure mode on the real path | this repo | Done | #52, run 36229742351 |
| 5 | Run it nightly and on demand, not yet as a required check. After about two weeks, promote it if every failure had a known cause that was not flakiness. Promotion means both a required PR check and a job that `release.yml` runs before publishing, alongside tests, lint and build, because a tag-triggered release never sees a PR check. Promotion is my call, recorded here. Releases stay ungated during the trial (my decision, 2026-09-25) | me | Open | |
| 6 | Add an AppArmor compile check (`apparmor_parser -Q -K`, as a290 and r5 do). Enforcement stays in TESTING.md's manual HA OS layer until the devcontainer is shown to enforce it. Amended 2026-09-26: it can, and does. See the amendment | this repo | Done | #51 |
| 7 | Tighten the standalone smoke. Assert `uid=1001(app)`, which CLAUDE.md said it did while `build.yml` only rejected uid 0, and fail on any traceback in the container log | this repo | Done | #51 |
| 8 | Build and boot aarch64 on native `ubuntu-24.04-arm` instead of QEMU, and run Playwright on both arches | this repo | Done | #51 |
| 9 | Update TESTING.md and EVALUATION.md, which still said only amd64 was booted, and describe the new layer | this repo | Done | #56 |
| 10 | Declare the oldest supported HA in `config.yaml` (`homeassistant:`), which it did not do before. The value and the rule it must satisfy come from my global CLAUDE.md ("Home Assistant: test against current stable"), not from this ADR. Declare it only once row 11 has shown the add-on works on it. After that the Supervisor will not offer the update to older installs, so the changelog must say so. Declared as 2026.8.1 in 0.4.3. Stable Core was 2026.9.3 on 2026-09-26, so the bound was 2026.8.1 | this repo | Done | #55 |
| 11 | Test that floor too: a second pilot leg with Core pinned to the declared minimum, under the current stable Supervisor, which users on an old Core still receive through auto-update. Row 1 establishes whether the devcontainer can pin Core. Since row 10 the leg reads the version from `config.yaml`'s `homeassistant:` (`scripts/ha-floor-check.sh --declared`) rather than a default in the workflow, so the tested floor is the enforced one. The `floor_core` input only overrides it | this repo | Done | #52, run 36229742351; #55 |
| 12 | After row 10 lands, enforce the rule in CI. Fail if `config.yaml`'s `homeassistant:` is missing, or breaks the bound computed from current stable Core in `stable.json`. The floor is relative to a moving target, so without this check it can drift out of compliance with nobody noticing. It is not a required check, like the integration's equivalent (my decision, 2026-09-25), so an outage at `version.home-assistant.io` cannot block every merge. It is `scripts/ha-floor-check.sh`, run by lint.yml's `ha-floor` job and by `just ha-floor`, and skipped on tag refs so the same outage cannot block a release either | this repo | Done | #55 |
| 13 | Adopt the same approach in a290 and r5, including r5's Renovate rule for stable Core in a290. Accepting this ADR carries no authority into those repos. I approve the work in each of them separately | a290, r5 | Blocked | awaits row 5 and my approval in each repo |

Status is one of **Open**, **Done**, **Blocked**, **Dropped**. A **Done** row carries Evidence.

## Context

My users take Home Assistant updates through the normal path. An add-on that works in isolation but not under the current Supervisor fails them on update. On 2026-09-25 this add-on's CI booted the container on both arches (aarch64 under QEMU) and ran Playwright on amd64, all standalone. Nothing installed it through a Supervisor, set its options through one, reached it through ingress, or confined it with AppArmor. EVALUATION.md already listed "No Supervisor-level boot gate". a290 and r5 did not boot their containers in CI at all.

## Alternatives

Copied from the RFC's options table as it stood when I decided.

| Option | What it tests | Cost | Risk |
| --- | --- | --- | --- |
| **A. Official add-on devcontainer, stable channel** (chosen) | A real Supervisor and Core in `ghcr.io/home-assistant/devcontainer` (apps variant), with `SUPERVISOR_CHANNEL=stable` set explicitly (the default is `dev`, and the template uses `beta`). Installs the add-on from `apps/local`, sets options through the Supervisor API, and can open the ingress panel and run Playwright through Core | About 7 to 10 minutes per arch per run, based on one third-party repo. Free on public repos. One privileged job per arch. No KVM needed | AppArmor probably cannot be enforced: the one working example had to turn it off. Discovering local add-ons is known to be fragile (supervisor#3976). Not yet shown to boot on `ubuntu-24.04-arm` |
| B. Supervisor container run directly | What the Supervisor repo's own CI does: install, start, back up and restore an add-on through `ha apps` | Similar to A, with less setup | AppArmor is never applied (no os-agent, so it falls back to unconfined). That CI runs the dev channel. Stable is inferred from source, not run |
| C. HA OS in QEMU | The real OS, with real AppArmor | Highest: large images, slow boots, and getting a local add-on in is hard. There is no KVM on arm runners, so aarch64 would be fully emulated | No working CI example found. Likely slow and flaky |
| D. Cheap wins only | No Supervisor. Tighten what already runs | Low | Leaves the requirement unmet. Supervisor option handling, ingress and AppArmor stay manual-only |

## Consequences

**Accepted:** users on HA older than the declared floor stop receiving add-on updates. Under my rule the floor is never newer than the previous month's `.1`, so every install from that release onward keeps getting updates. CI gets slower and more complex: two privileged jobs of a few minutes each, plus a devcontainer digest for Renovate to keep current. Until row 5 promotes the check to required, a release can ship without the Supervisor-level test. The north star is reached at promotion, not before.

**Watch:**
- Discovering local add-ons is known to be fragile, and one other repo abandoned a similar job after five failures in five runs. Row 5's gate exists so a flaky check never becomes required.
- If the devcontainer cannot boot on `ubuntu-24.04-arm`, the aarch64 half needs another route. That is a deviation to bring back here.
- The add-on and the companion integration should keep their floors in step. If either raises its floor, the other should follow. Otherwise a user can have the add-on on an HA its integration does not support, or the reverse.

## Verification (2026-09-25)

- **Current stable** was read from `stable.json`: Supervisor 2026.09.2, Core 2026.9.3, OS 18.3. A missing channel or a wrong field would have returned nothing, or a different shape.
- **Channel defaults** were read in the devcontainer source (`common/rootfs_supervisor/etc/supervisor_scripts/common`: `SUPERVISOR_CHANNEL` defaults to `dev`) and in the Supervisor source (`validate.py:168` defaults to stable; `bootstrap.py:113-114`, where `SUPERVISOR_DEV` forces dev). This is why row 2 checks the running versions rather than trusting the setting.
- **CI at the time:** the "Build (aarch64)" job on #49 printed `OK /healthz`, `OK /zones.json shape` and `OK /save_zones round-trip`, which confirms CI booted aarch64 under QEMU. Playwright's `if: matrix.arch == 'amd64'` confirmed the browser test was amd64-only.
- **Published aarch64 0.4.1 image:** booted natively on Apple Silicon (`uname -m` inside it gave `aarch64`, against an amd64 control that gave `x86_64`) and passed every smoke check and the browser test on both origins. Stopping it then made `/healthz` fail, so the checks could fail. The same stop also exited 137 and logged "Web service exited with status 256", which is unexplained and was seen once.
- **Not established at the time:** that A boots on `ubuntu-24.04-arm`; that AppArmor can be enforced under A; runtime and flakiness for this add-on; that the Supervisor builds a local add-on from `build.yaml`'s base (row 3).

## Amendment (2026-09-26)

The pilot's first runs on GitHub (PR #52) failed on all four jobs at the add-on's start. The runner kernel's audit line was `apparmor="DENIED" operation="create" class="net" info="failed protocol match" profile="local_polygonal_zones" comm="s6-ipcserver-so" family="unix" sock_type="stream"`. Established: Linux 6.17 added unix-socket mediation to AppArmor (`security/apparmor/af_unix.c` is absent in 6.16 and present in 6.17). A policy compiled by a recent parser carries the new network encoding, and under it the profile's bare `network,` grants no unix access. HA OS 18.0 moved to kernel 6.18, but 18.3 still builds AppArmor parser 3.1.7, whose network code has no such encoding, so HA OS users take the kernel's legacy path and were not hit. That last step is inferred from source, not observed on an HA OS box.

I decided on 2026-09-26 to fix the profile and release it as 0.4.2 on its own, before the pilot merged. The first candidate, adding `unix,`, was run on the runners and changed nothing: the same denial on socket create. It is not in the fix. Pinning the policy ABI (`abi <abi/3.0>,`) turned all four jobs green (run 36227020887, 133 to 188 seconds per job). The parser then emits the encoding this profile was written against, and the kernel's legacy path honours `network,`. Verification: parsers 3.0.4 and 4.0.1 both compile the pinned profile and both reject a bogus `abi/9.9`. Consequence: the pilot enforces AppArmor on a newer parser and kernel than HA OS ships, so it catches this class of breakage before HA OS's next parser bump does. Row 6's "enforcement stays manual" is superseded for the pilot. The manual HA OS layer remains for what the devcontainer cannot reproduce.

## References

- RFC (unnumbered; this repo is not yet data-classified): https://claude.ai/code/artifact/6364efdb-f32e-4c4c-bd26-12ccd9a36111
- Stable channel: https://version.home-assistant.io/stable.json
- Working example of option A: https://github.com/brianbaggs35/ha-blink-clip-downloader/blob/main/.github/workflows/ha-integration.yaml
- TESTING.md (manual HA OS layer), docs/EVALUATION.md
