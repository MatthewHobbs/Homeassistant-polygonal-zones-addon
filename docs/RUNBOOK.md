# Release runbook

Operational procedures for the release pipeline. For routine releases, use `scripts/release-merge.sh` — this doc covers the failure modes.

> Throughout this doc `vX.Y.Z` is a placeholder — substitute the version in `polygonal_zones_editor/config.yaml`. Don't copy the literal example versions during an incident.

## Normal release

1. Merge the version-bump PR.
2. Tag the merge commit: `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. `.github/workflows/release.yml` runs:
   - **version** — resolves the version from the tag, verifies it is `X.Y.Z`, matches the tag and `config.yaml`, and has a `CHANGELOG.md` entry.
   - **tests** — reusable call to `test.yml` (pytest + 100% line coverage gate).
   - **lint** — reusable call to `lint.yml` (`frenck/action-addon-linter` + shellcheck on `scripts/` and `rootfs/`).
   - **build** — reusable call to `build.yml` — multi-arch docker build + per-arch boot-smoke + Playwright smoke (`scripts/ui_smoke.py`: draw→save round-trip, a11y aria-labels, tile-picker assertions). Added in PR #108 so a tag can't publish an image that hasn't booted cleanly at least once. Its arch matrix is also the one **publish** uses.
   - **publish** — per arch: refuses to push if `<version>` is already on GHCR from a different commit (see **Publish refuses: version already published from another commit**), then builds and pushes `ghcr.io/matthewhobbs/<arch>-addon-polygonal_zones:<version>`, labelled `org.opencontainers.image.revision=<commit>`, and attaches a Sigstore build-provenance attestation (keyless, via GitHub OIDC — no secret required).
   - **release** — extracts the CHANGELOG section for the version and creates/updates the GitHub Release.
   - **notify-failure** — opens a GitHub issue if any upstream job of a real (tag, not dry-run) release failed.

Supervisor picks up the new version within minutes via its addon-update check.

**publish** and **release** run only when the run's ref is a `v*` tag and it is not a dry run. Every other run stops after the gates and pushes nothing. The workflow's default token is read-only; only **publish** (packages, OIDC, attestations), **release** (contents) and **notify-failure** (issues) are granted writes.

## Dry run

Rehearse a release without publishing: **Actions → Release addon → Run workflow**, pick a branch (or the tag), enter the `version` (must equal `config.yaml`'s), and leave **dry_run** ticked (the default). It runs **version**, **tests**, **lint** and **build** as a release would, then skips **publish** and **release**. One difference: on a branch, **lint** also runs the network-dependent `ha-floor` check, which a tag run skips, so a branch dry run can fail where the release would not. A failed dry run opens no issue; read the run page.

A dispatch publishes only when it is run on the `vX.Y.Z` tag itself with **dry_run** unticked; that is the manual equivalent of a tag push. Run on a branch, it never publishes, whatever **dry_run** says.

## Common failures

### Tests or lint fail after tag push

- Fix the bug on `main`.
- Bump the version (a new patch, e.g. `vX.Y.Z` → `vX.Y.(Z+1)`) and re-release. Do **not** move the existing tag.
- Leave the original tag pointing at the broken commit — its failed release run is the historical record.

### Partial-matrix publish failure

One or more arch `Publish (<arch>)` jobs fail mid-matrix (GHCR auth timeout, rate limit, transient network). `:latest` was removed in #68, so there's no drift there — but users on the failed arches silently stay on the previous version.

1. **Identify failed arches** from the `notify-failure` issue or the workflow run page.
2. **Retry failed jobs** via **Actions → Release addon → <run> → Re-run failed jobs**. This re-runs only the failed matrix legs, re-pushing their images under the same version tag. A re-run is the same commit, so the overwrite check passes even if the failed leg had already pushed its image.
3. If GHCR is the cause (rate limits, auth), wait a few minutes before retry.
4. If the retry succeeds, close the auto-opened failure issue.
5. If retry keeps failing, consider re-running the whole workflow (**Re-run all jobs**). It rebuilds every arch from the same commit and re-pushes under the same tag; the overwrite check allows that, because the revision label matches.

### Publish refuses: version already published from another commit

The `Refuse to overwrite a different build of this version` step in `Publish (<arch>)` failed. `scripts/check-published-revision.sh` found `<arch>-addon-polygonal_zones:<version>` already on GHCR with an `org.opencontainers.image.revision` label naming a different commit, or with no label at all (images published before the label was added), or it could not read the registry. Nothing was pushed by that leg.

- **Different commit or no label:** the tag was moved, or a version was re-used. Do not try to force it through: users may already have the published image. Cut a new patch version from the commit you meant to release.
- **Registry error** (the message shows something other than `not found`): treat it as transient and **Re-run failed jobs**. A brand-new arch whose GHCR package does not exist yet may also land here if GHCR answers with a permission error rather than `not found`; check the message before re-running.

### Published images are bad (runtime bug shipped)

**Option A — forward-fix (preferred).** Revert the bad commit on `main`, bump version, tag. Takes one release cycle (~5 minutes).

**Option B — restore the add-on's partial backup.** This exists only if the user updated with backups on: the backup toggle in the UI's update dialog, or from a Supervisor shell:

```sh
ha apps update <slug> --backup
```

`<slug>` is the add-on's installed slug, which ends in `_polygonal_zones` (a repository-specific prefix comes first); `ha apps` lists the installed add-ons with their slugs.

Restoring that backup (**Settings → System → Backups**) puts back the add-on as it was before the update. There is no way to install a chosen earlier version instead: the CLI's `update` takes only `--backup`, and its help says "It is currently not possible to upgrade/downgrade to a specific version."

You can't delete a published ghcr.io image tag via automation; users on the broken version stay on it until they update. Cut a forward-fix version and announce it.

### Users report "zones stopped working" / `/zones.json` returns 503

Symptom: HA automations no longer fire on zone entry/exit, or a user hitting the addon's Web UI sees a `zones file unreadable` response.

1. Ask for the addon log (**Settings → Add-ons → Polygonal Zones → Log**). Search for `Failed to read` or the `/zones.json` traceback; a JSON parse error on startup points at a corrupted file, an `OSError` points at permissions or disk.
2. Common root causes:
   - **Ownership drift on `/data`** — Supervisor occasionally re-mounts after a base-image update or host reboot and `/data/polygonal_zones/zones.json` ends up owned by `root` rather than `app` (uid 1001). `rootfs/etc/cont-init.d/00-fix-perms` re-chowns on container start, so an addon restart usually resolves it.
   - **Disk full on `/data`** — `atomic_write_json` fails cleanly (no partial write; the old file is still in place), but if the disk filled mid-save the write is aborted. Check `df -h /data` via the Supervisor shell. Free space, then re-save from the editor or restore from snapshot.
   - **File corruption** — rare outside ungraceful power loss. Usually diagnosable because the log shows a JSON parse error rather than a read error.
3. **Fastest recovery**: restore `zones.json` from the latest HA snapshot that predates the failure (**Settings → System → Backups**). The companion integration retains its last-known-good state during the outage, so automations keep firing and no geofencing is lost during the recovery window.

### `release-merge.sh` exit codes

| Exit | Meaning | What to do |
|---|---|---|
| 0 | success | nothing |
| 1 | unexpected failure (an unguarded command failed) | read the output, fix the cause, re-run; a merged PR resumes as for exit 5 |
| 2 | usage error: missing PR number or unknown flag | re-invoke with a PR number |
| 3 | pre-flight failed (PR state, checks, mergeability, version mismatch) | fix the cause and re-run |
| 4 | merge failed (unusual after pre-flight passed) | check the GitHub UI |
| 5 | tag creation failed, or the merged PR's merge commit could not be read (`merge-commit-unknown`) | re-run: for a merged PR the script resumes by tagging that PR's merge commit with the version it carries, whatever has merged to `main` since |
| 6 | release workflow failed | the script only watches the run, so re-running it alone reports the same failure. Fix the run first: re-run its failed jobs (**Partial-matrix publish failure** above), or cut a new patch for a test or lint failure; then re-run the script to watch. If the reason is `release-run-not-found` (`release-run-not-found-for-vX.Y.Z`), a plain re-run picks the run up |

Progress goes to stdout as `STATUS:<TOKEN>` lines, ending in `STATUS:SUCCESS …` on success. A failure prints `STATUS:FAILED <reason>` to stderr; usage errors and unexpected failures print no `STATUS:FAILED` line.

### Release run cancelled by concurrency

Note: `test.yml` and `lint.yml` use distinct concurrency-group prefixes (`tests-` and `lint-`). If you see `Canceling since a higher priority waiting request for <group> exists`, it's almost certainly because a reusable workflow lost its prefix or another workflow is claiming the same group. Fix by giving each reusable workflow a unique concurrency-group prefix.

### Build-provenance attestation step fails

1. Check the `Attest build provenance` step output in the `publish` job.
2. Most common cause: the `publish` job's `permissions:` block is missing `id-token: write` / `attestations: write` (the workflow-level default is read-only, so they must be granted on the job), or `steps.build.outputs.digest` is empty (the `docker/build-push-action` step must have `id: build` and `push: true`). The image push itself has already succeeded by this point, so the image is published but unattested.
3. **Safe regression** if you need to ship while attestation is broken: the image is usable without the attestation (the Supervisor does not gate installs on it). Re-run the failed `publish` job after fixing permissions, or attach the attestation out-of-band with `gh attestation` against the pushed digest.
4. Verify a published image: `gh attestation verify oci://ghcr.io/matthewhobbs/<arch>-addon-polygonal_zones:<version> --owner MatthewHobbs`.

### Tag pushed before version was bumped in config.yaml

The `version` job fails at the version-match check. The release run dies before any image is pushed.

1. Delete the bad tag: `git push --delete origin vX.Y.Z && git tag -d vX.Y.Z`.
2. Bump `config.yaml` on `main` (or in a PR), merge.
3. Re-tag: `git tag vX.Y.Z <merge-commit> && git push origin vX.Y.Z`.

### CHANGELOG entry missing for the tag

Same recovery as above — `version` job catches it and fails before `publish`. Add the CHANGELOG entry on `main`, then re-tag pointing at the commit that includes it.

## Never do this

- **Don't force-push a tag** to a different commit after any image has been pushed under it. `:latest` has been removed so there's no floating-tag drift risk, but re-publishing a different image under an existing `vX.Y.Z` tag breaks the reproducibility promise. `publish` refuses it for every arch already pushed under the old commit (see **Publish refuses** above). An arch that never got pushed has nothing to compare against and would publish the new commit, splitting one version across two commits, so cut a new patch version instead.
- **Don't amend the version-bump commit** after tagging. Create a new commit + new tag.
- **Don't merge non-version-bump PRs on top of an in-flight release.** They don't affect the release directly but they muddy the CHANGELOG if you need to cut a hotfix.

## Who to bug

- **Addon pipeline:** @MatthewHobbs (repo maintainer).
- **HA base images:** [home-assistant/docker-base](https://github.com/home-assistant/docker-base).
- **Image attestations:** [actions/attest-build-provenance](https://github.com/actions/attest-build-provenance) · verify with `gh attestation verify`.
