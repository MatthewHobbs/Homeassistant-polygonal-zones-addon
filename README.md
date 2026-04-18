# Polygonal zones addon.

> **Fork Notice**
>
> This repository is a fork of [MichelGerding/Homeassistant-polygonal-zones-addon](https://github.com/MichelGerding/Homeassistant-polygonal-zones-addon),
> which was deprecated by its original author. This fork is being maintained to address current issues — including
> the OpenStreetMap tile referrer policy change that broke map rendering in the upstream project.

This repository contains the code for the polygonal zones addon.

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FMatthewHobbs%2FHomeassistant-polygonal-zones-addon.git)

## Add-ons

This repository contains the code for the following add-ons:

### [Polygonal zones editor](./polygonal_zones_editor)

_This add-on allows you to create a polygonal zones from a list of entities._

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FMatthewHobbs%2FHomeassistant-polygonal-zones-addon.git)

## Releasing

Releases are cut by pushing a `vX.Y.Z` git tag whose version matches the current `polygonal_zones_editor/config.yaml`:

```sh
git tag v0.2.12
git push origin v0.2.12
```

[`.github/workflows/release.yml`](./.github/workflows/release.yml) takes over from there:
1. Verifies the tag matches `config.yaml`'s version.
2. Builds multi-arch images (using the same matrix as the regular build workflow, sourced from `polygonal_zones_editor/build.yaml`).
3. Pushes them to `ghcr.io/matthewhobbs/{arch}-addon-polygonal_zones:<version>` and `:latest`.
4. Creates a GitHub Release whose body is the matching section of [`polygonal_zones_editor/CHANGELOG.md`](./polygonal_zones_editor/CHANGELOG.md).

The workflow can also be run manually via **Actions → Release addon → Run workflow** (the `version` input must equal `config.yaml`'s version).
