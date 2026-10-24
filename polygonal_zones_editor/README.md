# Polygonal Zones App

[![GitHub release](https://img.shields.io/github/v/release/MatthewHobbs/Homeassistant-polygonal-zones-addon?style=flat-square&logo=github)](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones-addon/releases)
[![License](https://img.shields.io/github/license/MatthewHobbs/Homeassistant-polygonal-zones-addon?style=flat-square)](../LICENSE)
[![Tests](https://img.shields.io/github/actions/workflow/status/MatthewHobbs/Homeassistant-polygonal-zones-addon/test.yml?branch=main&style=flat-square&label=tests&logo=github-actions&logoColor=white)](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones-addon/actions/workflows/test.yml)
[![Build](https://img.shields.io/github/actions/workflow/status/MatthewHobbs/Homeassistant-polygonal-zones-addon/build.yml?branch=main&style=flat-square&label=build&logo=github-actions&logoColor=white)](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones-addon/actions/workflows/build.yml)

Draw polygonal zones inside Home Assistant, name them, and let the companion
[Polygonal Zones integration](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones) use them in location automations.

Home Assistant needs at least version 2026.8.1 to offer this app, and it must be a 64-bit installation (`aarch64` or `amd64`). 32-bit installations are not supported.

> **Is this the right tool for your zones?**
>
> How accurate a zone is depends on the device being tracked, not on how
> carefully you draw. A phone reports its position with an error of 5–15 m
> outdoors and 15–65 m indoors, and the integration widens each zone by that
> error before deciding whether a device is inside.
>
> **A good fit:** a garden, a paddock, a yard, a car park. Anything about 20 m
> across or more, where the question is simply "is it here or not".
>
> **A poor fit:** rooms inside a building, or anything that needs pinpoint
> accuracy. A 4 m room is smaller than the error of a phone standing still in
> it, so neighbouring rooms will swap at random and a car on the drive can
> count as being in the kitchen. Room-level presence needs Wi-Fi access points,
> Bluetooth room sensors or mmWave sensors instead. Careful drawing cannot fix
> a limit in the position.
>
> See [DOCS.md](DOCS.md) for the details.

> **This is a fork**
>
> It continues [MichelGerding/Homeassistant-polygonal-zones-addon](https://github.com/MichelGerding/Homeassistant-polygonal-zones-addon), which its author no longer maintains. This fork keeps it working, including the fix for a change at OpenStreetMap that stopped the map from drawing in the original.
>
> Companion integration, which tracks devices against your zones:
> [MatthewHobbs/Homeassistant-polygonal-zones](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones)
>
> Installation guide and documentation:
> [matthewhobbs.github.io/Homeassistant-polygonal-zones](https://matthewhobbs.github.io/Homeassistant-polygonal-zones)

[![Open your Home Assistant instance and show the add app repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FMatthewHobbs%2FHomeassistant-polygonal-zones-addon.git)

## Installation

1. Choose the button above to add this repository to Home Assistant. By hand: open
   **Settings → Apps**, choose **Install app**, open the **⋮** menu and choose
   **Repositories**, then paste
   `https://github.com/MatthewHobbs/Homeassistant-polygonal-zones-addon`.
2. In the app store, find **Polygonal Zones** and choose **Install**.

Version 0.7.0 and later offer themselves to the integration automatically once
installed and started.

## Documentation

Configuration, LAN access, backing up and restoring zones, and how the
integration reads `zones.json` are in [`DOCS.md`](./DOCS.md). Home Assistant
shows that page in the app's **Documentation** tab.

## Roadmap

Planned work is tracked in [GitHub issues](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones-addon/issues).
Each release is described in the [CHANGELOG](./CHANGELOG.md).

## Contributing

Issues and pull requests are welcome.
