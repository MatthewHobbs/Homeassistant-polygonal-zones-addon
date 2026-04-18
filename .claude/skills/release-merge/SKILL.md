---
name: release-merge
description: Squash-merge a PR and, if it bumps the addon version, tag the merge commit and watch the release workflow to completion. Wraps scripts/release-merge.sh for this repo. Use when the user says "release PR <N>", "merge and release <N>", or "/release-merge <N>". Do NOT use for ordinary merges that don't ship a release — for those, use `gh pr merge` directly.
disable-model-invocation: true
argument-hint: "<pr-number>"
allowed-tools:
  - Bash(bash scripts/release-merge.sh*)
  - Bash(gh pr view*)
  - Bash(gh pr list*)
---

# release-merge

Wraps `scripts/release-merge.sh`. The script does the irreversible work; this skill exists to gate it behind a single explicit user confirmation, summarise progress, and translate the script's `STATUS:` lines into a concise report.

## What it does

1. Verifies the PR is OPEN, MERGEABLE, and that every required CI check (5 multi-arch builds, lint, pytest) is `SUCCESS`. Refuses to merge on `IN_PROGRESS`/missing checks.
2. Squash-merges the PR.
3. If the PR bumped `polygonal_zones_editor/config.yaml`'s `version`, tags the merge commit `vX.Y.Z` and watches `.github/workflows/release.yml` to completion (multi-arch images push to `ghcr.io/matthewhobbs/{arch}-addon-polygonal_zones`, GitHub Release published).
4. If the PR is already MERGED (e.g. retry after a transient `gh run watch` failure), auto-resumes — creates the missing tag or jumps straight to watching the existing release run.
5. If the PR didn't bump the version, stops after the merge.

## Required arguments

`$1` — PR number. If unset, ask the user before doing anything else.

## Procedure

1. **Pre-flight preview.** Run `bash scripts/release-merge.sh --dry-run $1` and capture the output. The script prints `STATUS:CHECKING_PR`, `STATUS:CHECKING_CHECKS`, `STATUS:PLANNED_RELEASE old→new as vX.Y.Z` (or `STATUS:MERGING_NO_RELEASE` if no version bump), then exits 0 without mutating anything.

2. **If dry-run failed (non-zero exit).** Surface the `STATUS:FAILED <reason>` line to the user verbatim. Do not proceed. Stop.

3. **Confirmation.** Show a short summary derived from the dry-run output:
   - "Squash-merge PR #N: *<title>*"
   - if a release is planned: "Tag and release: vX.Y.Z (bumps from vA.B.C)"
   - if no release: "(no version bump → no release)"
   Then ask **"Proceed?"** and wait for the user's "yes" / "merge" / "go" before continuing. Do **not** auto-proceed.

4. **Run for real.** Once the user confirms, run `bash scripts/release-merge.sh $1` (no `--dry-run`).

5. **Report.** When the script exits, look at the final `STATUS:` line:
   - `STATUS:SUCCESS …` → confirm the PR/tag URL.
   - `STATUS:FAILED <reason>` → surface the reason and exit code; do NOT speculate about recovery beyond what the script's exit code documents.

## Exit code map (for the report)

| Exit | Meaning | What the user should do |
|---|---|---|
| 0 | success | nothing |
| 2 | usage error in the script call | re-invoke with a PR number |
| 3 | pre-flight failed (state, checks, mergeability) | fix the upstream cause |
| 4 | merge failed (unusual after pre-flight passed) | check GitHub UI |
| 5 | tag creation failed | retry — script auto-resumes if the PR is now MERGED |
| 6 | release workflow failed | inspect the workflow run; rerun the script (auto-resumes) |

## When NOT to use this

- Plain merges that don't bump the addon version. Use `gh pr merge` directly. (The script *will* handle them — it just merges and stops — but the skill's confirmation overhead is extra friction for the no-release case.)
- Manually triggering the release workflow without merging anything (e.g. re-publishing an existing tag's images). Use **Actions → Release addon → Run workflow** instead.

## Why this skill exists

The release workflow only fires when a `vX.Y.Z` tag is pushed. If a maintainer merges a version-bump PR but forgets to tag, HA Supervisor will see the new version, try to pull `ghcr.io/.../<arch>-addon-polygonal_zones:<new>`, fail (the image doesn't exist yet), and the user's update breaks. The script closes that race window by doing both in one operation; this skill closes the human-error path that was substituting for the race.
