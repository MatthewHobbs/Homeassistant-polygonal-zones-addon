# Release runbook

Operational procedures for the release pipeline. For routine releases, use `scripts/release-merge.sh` or the `/release-merge` skill — this doc covers the failure modes.

## Normal release

1. Merge the version-bump PR.
2. Tag the merge commit: `git tag v0.2.19 && git push origin v0.2.19`.
3. `.github/workflows/release.yml` runs:
   - **matrix** — resolves the version from the tag, verifies it matches `config.yaml`, verifies a `CHANGELOG.md` entry exists, generates the arch matrix.
   - **tests** — reusable call to `test.yml` (pytest + 100% line coverage gate).
   - **lint** — reusable call to `lint.yml` (`frenck/action-addon-linter` + shellcheck on `scripts/` and `rootfs/`).
   - **build** — reusable call to `build.yml` — multi-arch docker build + amd64 boot-smoke + Playwright smoke (draw→save round-trip, a11y aria-labels, tile-picker assertions). Added in PR #108 so a tag can't publish an image that hasn't booted cleanly at least once.
   - **publish** — builds and pushes per-arch images to `ghcr.io/matthewhobbs/<arch>-addon-polygonal_zones:<version>`. Optionally notarizes via codenotary if `CAS_API_KEY` is set.
   - **release** — extracts the CHANGELOG section for the version and creates/updates the GitHub Release.
   - **notify-failure** — opens a GitHub issue if any upstream job failed.

Supervisor picks up the new version within minutes via its addon-update check.

## Common failures

### Tests or lint fail after tag push

- Fix the bug on `main`.
- Bump the version (`v0.2.19` → `v0.2.20`) and re-release. Do **not** move the existing tag.
- Leave the original tag pointing at the broken commit — its failed release run is the historical record.

### Partial-matrix publish failure

One or more arch `Publish (<arch>)` jobs fail mid-matrix (GHCR auth timeout, rate limit, transient network). `:latest` was removed in #68, so there's no drift there — but users on the failed arches silently stay on the previous version.

1. **Identify failed arches** from the `notify-failure` issue or the workflow run page.
2. **Retry failed jobs** via **Actions → Release addon → <run> → Re-run failed jobs**. This re-runs only the failed matrix legs, re-pushing their images under the same version tag.
3. If GHCR is the cause (rate limits, auth), wait a few minutes before retry.
4. If the retry succeeds, close the auto-opened failure issue.
5. If retry keeps failing, consider re-running the whole workflow (**Re-run all jobs**) — it's idempotent: images are content-addressable, tags get re-pushed.

### Published images are bad (runtime bug shipped)

**Option A — forward-fix (preferred).** Revert the bad commit on `main`, bump version, tag. Takes one release cycle (~5 minutes).

**Option B — immediate rollback.** Users can pin via **Supervisor → Add-ons → Polygonal Zones → ⋮ → Rebuild** after installing a previous version from the store. From a Supervisor shell:

```sh
ha addons update polygonal_zones --version 0.2.18
```

You can't delete a published ghcr.io image tag via automation; users on the broken version stay on it until they update. Cut a forward-fix version and announce it.

### Users report "zones stopped working" / `/zones.json` returns 503

Symptom: HA automations no longer fire on zone entry/exit, or a user hitting the addon's Web UI sees a `zones file unreadable` response.

1. Ask for the addon log (**Settings → Add-ons → Polygonal Zones → Log**). Search for `Failed to read` or the `/zones.json` traceback; a JSON parse error on startup points at a corrupted file, an `OSError` points at permissions or disk.
2. Common root causes:
   - **Ownership drift on `/data`** — Supervisor occasionally re-mounts after a base-image update or host reboot and `/data/polygonal_zones/zones.json` ends up owned by `root` rather than `app` (uid 1001). `rootfs/etc/cont-init.d/00-fix-perms` re-chowns on container start, so an addon restart usually resolves it.
   - **Disk full on `/data`** — `atomic_write_json` fails cleanly (no partial write; the old file is still in place), but if the disk filled mid-save the write is aborted. Check `df -h /data` via the Supervisor shell. Free space, then re-save from the editor or restore from snapshot.
   - **File corruption** — rare outside ungraceful power loss. Usually diagnosable because the log shows a JSON parse error rather than a read error.
3. **Fastest recovery**: restore `zones.json` from the latest HA snapshot that predates the failure (**Settings → System → Backups**). The companion integration retains its last-known-good state during the outage, so automations keep firing and no geofencing is lost during the recovery window.

### Release run cancelled by concurrency

See the session history: `test.yml` and `lint.yml` use distinct concurrency-group prefixes (`tests-` and `lint-`). If you see `Canceling since a higher priority waiting request for <group> exists`, it's almost certainly because a reusable workflow lost its prefix or another workflow is claiming the same group. Fix by giving each reusable workflow a unique concurrency-group prefix.

### CAS_API_KEY is set but signing fails

1. Check the addon log's `Notarize image with codenotary/cas` step output.
2. Most common: the CAS identity has expired or the API key has been revoked. Generate a fresh key at <https://cas.codenotary.com>, update the repo secret.
3. **Safe regression** if you need to ship while signing is broken: comment the `codenotary:` line in `config.yaml` so Supervisor doesn't require verification. Unset the secret. Release. Re-enable later.

### Tag pushed before version was bumped in config.yaml

The `matrix` job fails at the version-match check. The release run dies before any image is pushed.

1. Delete the bad tag: `git push --delete origin v0.2.19 && git tag -d v0.2.19`.
2. Bump `config.yaml` on `main` (or in a PR), merge.
3. Re-tag: `git tag v0.2.19 <merge-commit> && git push origin v0.2.19`.

### CHANGELOG entry missing for the tag

Same recovery as above — `matrix` job catches it and fails before `publish`. Add the CHANGELOG entry on `main`, then re-tag pointing at the commit that includes it.

## Never do this

- **Don't force-push a tag** to a different commit after any image has been pushed under it. `:latest` has been removed so there's no floating-tag drift risk, but re-publishing a different image under an existing `v0.2.19` tag breaks the reproducibility promise.
- **Don't amend the version-bump commit** after tagging. Create a new commit + new tag.
- **Don't merge non-version-bump PRs on top of an in-flight release.** They don't affect the release directly but they muddy the CHANGELOG if you need to cut a hotfix.

## Who to bug

- **Addon pipeline:** @MatthewHobbs (repo maintainer).
- **HA base images:** [home-assistant/docker-base](https://github.com/home-assistant/docker-base).
- **CAS / codenotary:** <https://support.codenotary.com>.
