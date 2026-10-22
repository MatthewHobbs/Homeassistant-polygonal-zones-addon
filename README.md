# Polygonal Zones Add-on

[![GitHub release](https://img.shields.io/github/v/release/MatthewHobbs/Homeassistant-polygonal-zones-addon?style=flat-square&logo=github)](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones-addon/releases)
[![License](https://img.shields.io/github/license/MatthewHobbs/Homeassistant-polygonal-zones-addon?style=flat-square)](./LICENSE)
[![Tests](https://img.shields.io/github/actions/workflow/status/MatthewHobbs/Homeassistant-polygonal-zones-addon/test.yml?branch=main&style=flat-square&label=tests&logo=github-actions&logoColor=white)](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones-addon/actions/workflows/test.yml)
[![Build](https://img.shields.io/github/actions/workflow/status/MatthewHobbs/Homeassistant-polygonal-zones-addon/build.yml?branch=main&style=flat-square&label=build&logo=github-actions&logoColor=white)](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones-addon/actions/workflows/build.yml)
[![Lint](https://img.shields.io/github/actions/workflow/status/MatthewHobbs/Homeassistant-polygonal-zones-addon/lint.yml?branch=main&style=flat-square&label=lint&logo=github-actions&logoColor=white)](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones-addon/actions/workflows/lint.yml)
[![Home Assistant Addon](https://img.shields.io/badge/Home%20Assistant-Addon-41BDF5?style=flat-square&logo=home-assistant&logoColor=white)](https://www.home-assistant.io/addons/)

![aarch64](https://img.shields.io/badge/aarch64-yes-green?style=flat-square)
![amd64](https://img.shields.io/badge/amd64-yes-green?style=flat-square)

> **Fork Notice**
>
> This repository is a fork of [MichelGerding/Homeassistant-polygonal-zones-addon](https://github.com/MichelGerding/Homeassistant-polygonal-zones-addon),
> which was deprecated by its original author. This fork is maintained to address current issues, including
> the OpenStreetMap tile referrer policy change that broke map rendering in the upstream project.

This repository contains the code for the Polygonal Zones Add-on.

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FMatthewHobbs%2FHomeassistant-polygonal-zones-addon.git)

## Add-ons

This repository contains the code for the following add-ons:

### [Polygonal Zones Editor](./polygonal_zones_editor)

_Draw polygonal zones on a map and serve them as GeoJSON for the companion Polygonal Zones integration to use in location-based automations._

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FMatthewHobbs%2FHomeassistant-polygonal-zones-addon.git)

## Releasing

For version-bump PRs, use the helper. It merges the PR, tags the merge commit, and watches the release workflow to completion. A merged version bump still reaches HA users before its images exist on GHCR, for about one release run.

The helper refuses to merge unless the PR's required checks have passed. The release workflow then refuses to publish unless:
- the tag is on `main`;
- its version is above every released tag, and matches `config.yaml`;
- `CHANGELOG.md` has an entry for it;
- the tests, lint and build checks are green on the tagged commit.

**From a shell:**

```sh
scripts/release-merge.sh <pr-number>             # for real
scripts/release-merge.sh --dry-run <pr-number>   # preview only
```

The script resumes if the PR is already merged (for example after a transient failure mid-watch). For PRs that don't bump the version, it squash-merges with no tag and no release.

### Manual fallback

To do it by hand, push a `vX.Y.Z` git tag whose version matches the current `polygonal_zones_editor/config.yaml`:

```sh
# Replace X.Y.Z with the version in polygonal_zones_editor/config.yaml
git tag vX.Y.Z
git push origin vX.Y.Z
```

[`.github/workflows/release.yml`](./.github/workflows/release.yml) takes over from there:
1. Checks the gates above.
2. Builds multi-arch images (the same matrix as the build workflow, from `polygonal_zones_editor/build.yaml`).
3. Pushes them to `ghcr.io/matthewhobbs/{arch}-addon-polygonal_zones:<version>`.
4. Creates a GitHub Release whose body is the matching section of [`polygonal_zones_editor/CHANGELOG.md`](./polygonal_zones_editor/CHANGELOG.md).

The workflow can also be run manually via **Actions → Release addon → Run workflow** (the `version` input must equal `config.yaml`'s version). A manual run is a dry run by default: it runs every gate and pushes nothing. It publishes only when run on the `vX.Y.Z` tag with **dry_run** unticked.
