# Polygonal Zones

Create and manage polygonal zones inside Home Assistant. Draw shapes on a map, name them, and have the companion [Polygonal Zones integration](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones) consume them for location-based automations.

## Configuration

All options live under **Settings → Add-ons → Polygonal Zones → Configuration**. Defaults are sensible for a stock Home Assistant install — the only setting most people change is `zone_colour`.

| Option            | Type     | Default   | What it does                                                                                                              |
| ----------------- | -------- | --------- | ------------------------------------------------------------------------------------------------------------------------- |
| `zone_colour`     | string   | `purple`  | Colour used to render zones on the map (any CSS colour name or `#rrggbb`).                                                |
| `theme`           | list     | `auto`    | `auto` follows the OS `prefers-color-scheme`. Set `light` or `dark` to override.                                          |
| `allow_all_ips`   | bool     | `false`   | When `true`, accept HTTP requests from any IP, not just the Home Assistant ingress sidecar. See **LAN access** below.     |
| `save_token`      | password | *(empty)* | When set, `POST /save_zones` requires `X-Save-Token: <value>` for any non-ingress request. See **Securing /save_zones**.  |
| `trusted_proxies` | string   | *(empty)* | Comma-separated list of proxy IPs whose `X-Forwarded-For` header should be honoured. Leave empty unless you front the addon with your own reverse proxy. |
| `log_level`       | list     | `info`    | One of `debug`, `info`, `warning`, `error`, `critical`. Bump to `debug` when troubleshooting.                             |

### Network ports

The addon exposes its web interface on **TCP 8000** by default. Inside Home Assistant you reach it via the ingress UI (`Open Web UI` button) — that's the recommended path.

Direct LAN access on `http://<your-ha-host>:8000/` is also enabled by default for backups and the integration. If you don't need it, change the host port in **Configuration → Network** to `disabled` (sets it to null).

### LAN access (`allow_all_ips`)

By default, only the HA ingress IP (`172.30.32.2`) can talk to the addon. With `allow_all_ips: true`, any client on your network can:

- `GET /zones.json` — read the zone geometry (the integration uses this).
- `POST /save_zones` — overwrite the zones.

Enable it if your Home Assistant integration runs in a different container, or if you want to back up / restore zones via `curl`. Pair with `save_token` (below) to keep `POST /save_zones` protected.

### Securing `/save_zones` (`save_token`)

When `save_token` is set, the addon requires the header `X-Save-Token: <value>` on any `POST /save_zones` request that doesn't come from HA ingress. The Save button in the addon's UI keeps working unauthenticated because it goes through ingress.

Pick a long random string (the field is masked in the UI). To rotate, change the value and restart the addon — there is no migration needed.

The token is **never** logged. The `Loaded options:` line at startup prints it as `***`.

### Backing up / restoring zones with `curl`

The zones are kept in `/data/polygonal_zones/zones.json` inside the container, but you don't need a shell to read them. If you have `allow_all_ips: true`:

```sh
# Backup
curl http://<ha-host>:8000/zones.json > zones-backup.json

# Restore (without save_token)
curl -X POST -H 'Content-Type: application/json' \
  --data-binary @zones-backup.json \
  http://<ha-host>:8000/save_zones

# Restore (with save_token set)
curl -X POST -H 'Content-Type: application/json' \
  -H 'X-Save-Token: <yourtoken>' \
  --data-binary @zones-backup.json \
  http://<ha-host>:8000/save_zones
```

Recommended: leave `save_token` set, only enable `allow_all_ips` while you're actively backing up, then disable it again.

`GET /zones.json` returns an `ETag` header. Pass it back as `If-Match` on `POST /save_zones` to refuse the write if anything changed in between (the addon's own UI does this by default). Plain `curl` posts without `If-Match` keep their last-write-wins behaviour, so existing scripts are unaffected.

## Integration with the Polygonal Zones HA integration

The companion [Polygonal Zones integration](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones) reads `/zones.json` from this addon. If the integration runs anywhere other than the HA ingress sidecar (different container or external host), set `allow_all_ips: true` so its `GET /zones.json` requests are accepted.

> **Known limitation — private-IP URLs.** The integration hardens itself against SSRF by refusing to fetch URLs that resolve to a private (RFC-1918) address — that includes `192.168.x.x`, `10.x.x.x`, `172.16.0.0/12`, and local mDNS names that resolve to those. So a naive `zone_urls: http://<ha-host-lan-ip>:8000/zones.json` will be rejected by the integration **before** it ever reaches this addon, even when `allow_all_ips: true` is on.
>
> **Recommended workaround — private reverse proxy with TLS on a non-RFC-1918 hostname.** Put the addon behind a reverse proxy you control (nginx, Caddy, Traefik, HA's own NGINX Proxy Manager addon) that terminates TLS under a public-resolving hostname such as `zones.yourdomain.tld`. Point the integration at `https://zones.yourdomain.tld/zones.json`. The DNS name resolves publicly (so the integration's SSRF guard lets it through), but the listener is still on your LAN — no zones data leaves your network. Combine with `save_token` and basic-auth at the proxy if the hostname is reachable from the public internet.
>
> **Tracking upstream.** An opt-in relaxation on the integration side (e.g. `allow_private_urls: true`) would remove the need for any of this. Track progress in [issue #111](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones-addon/issues/111); once shipped, the plain `http://<ha-host-lan-ip>:8000/zones.json` path will work out of the box.
>
> **Last resort — public-CDN mirror (privacy warning).** Hosting `zones.json` on a public-facing server (GitHub Pages, S3, Cloudflare Pages, etc.) is occasionally suggested, but **do not do this without understanding the privacy cost**. Your polygon geometry encodes the precise shape and location of your home, workplace, school runs, and any other place you've drawn a zone. Publishing it to a public CDN means:
>
> - The data is **world-readable** — any visitor, crawler, scraper or search engine can fetch it.
> - Public CDNs **cache and replicate** content across their global edge networks. Once it's been served even once, you cannot assume you can fully revoke it.
> - Major providers (GitHub, AWS, Cloudflare) are subject to the US CLOUD Act, so the data can be disclosed to US authorities regardless of where you live.
> - Search engines **index** content hosted on common providers. Your home coordinates become discoverable by name/URL.
> - Deleting the file later does **not** remove archived copies (Wayback Machine, third-party scrapers).
>
> If you absolutely must go this route (e.g. you have no control over DNS and cannot stand up a reverse proxy), use a private / unlisted bucket with a strong pre-signed URL, rotate the URL regularly, and understand that any leak of the URL equates to a leak of the geometry.

## Usage

### Accessing the Web Interface

After installing and starting the add-on, you can access the web interface in two ways:

- **Info page**: press the **Open Web UI** button on the addon's info page.
- **Sidebar**: enable **Show in sidebar** in the addon configuration to add a permanent shortcut to the HA sidebar.

### Saving changes

**Important:** Any changes made to the zones need to be saved by pressing the **Save Button** located at the bottom of the sidebar. Unsaved changes will not be persisted between restarts.

### Zones File

The zones are stored as a GeoJSON `FeatureCollection` at `http(s)://[HOST]:[PORT]/zones.json` (default port 8000), and on disk at `/data/polygonal_zones/zones.json` inside the container. The companion [Polygonal Zones integration](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones) reads this file. See the **Backing up / restoring zones with curl** section for the recommended backup workflow.

### Features

#### Viewing zones

- A list of all the zones is displayed in the sidebar.
- The zones are also visualized on the map.

#### Adding zones

- Click the Draw Polygon Button (the button with a pentagon icon) located on the right side of the map.
- Click on the map to define the points of the polygon.
- Click on the first point again to complete the polygon.

#### Editing zones

- Click the Edit Button next to a zone's name in the sidebar.
- This will make the polygon editable on the map. Drag the points to modify the zone's shape.
- You can also rename the zone directly in the sidebar.
- After editing, press the Save Button next to the zone's name to save the changes.

#### Deleting zones

- Click the Delete Button in the toolbar.
- Select the zones you want to delete by clicking on them.
- Click the Clear All Button to delete all zones at once.
- Once you're satisfied with your selection, press the Save Button next to the delete button to confirm the deletion. Remember to also press the Save Button in the sidebar to permanently save these changes.
