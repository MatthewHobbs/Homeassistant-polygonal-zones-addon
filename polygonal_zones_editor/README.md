# Polygonal Zones Add-on

Create and manage polygonal zones inside Home Assistant. Draw shapes on a map, name them, and have the companion [Polygonal Zones integration](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones) consume them for location-based automations.

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FMatthewHobbs%2FHomeassistant-polygonal-zones-addon.git)

## Installation

1. Click the button above to add the repository to your Home Assistant Supervisor, **or** add `https://github.com/MatthewHobbs/Homeassistant-polygonal-zones-addon.git` manually under Supervisor → Add-on Store → ⋮ → Repositories.
2. Find "Polygonal Zones" in the store and click **Install**.

## Documentation

Full user documentation — configuration, LAN access, securing `/save_zones`, backing up and restoring zones, and how the companion integration consumes `/zones.json` — lives in [`DOCS.md`](./DOCS.md) and is rendered in the addon's **Documentation** tab in Home Assistant.

## Screenshots

- Viewing all zones: ![Screenshot while viewing all zones](../screenshots/screenshot-view.png)
- Editing a zone: ![Screenshot while editing a zone](../screenshots/screenshot-edit.png)
- Delete button and its options: ![Screenshot of delete button](../screenshots/screenshot-delete-button.png)

## Roadmap

Planned improvements (UI polish, multi-shape zones, multiple zone files, etc.) are tracked as [GitHub issues](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones-addon/issues). Recent releases are documented in the [CHANGELOG](./CHANGELOG.md).

## Contributing

Contributions are welcome. Open an issue or submit a pull request.
