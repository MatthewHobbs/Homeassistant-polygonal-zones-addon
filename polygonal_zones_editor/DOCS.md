# Polygonal Zones Editor

Create and manage polygonal zones inside Home Assistant. Draw shapes on a map, name them, and have the companion [Polygonal Zones integration](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones) consume them for location-based automations.

> **32-bit hosts.** Home Assistant 2025.12 (released 2025-12-03) deprecated `armhf`, `armv7`, and `i386` as supported host architectures for add-ons. The last release of this add-on for those arches is 0.2.25. From 0.2.26 only `aarch64` and `amd64` images are published. If you run a 32-bit host (Raspberry Pi 0/1, a 32-bit OS on a Pi 2/3, 32-bit Intel Atom), 0.2.25 is the last version you can run. To keep receiving updates, move to a 64-bit HA OS installation. Supervisor stops offering updates once it detects the architecture mismatch.

## What this add-on is good for, and what it isn't

Zones drawn here are compared against positions reported by phones, cars and
trackers. **The accuracy of the answer is set by those devices, not by how
carefully you draw.** A boundary placed to the centimetre is still only as good
as the fix being tested against it.

For a sense of scale, from real devices:

| Source                                | Typical reported accuracy              |
| ------------------------------------- | -------------------------------------- |
| Phone outdoors, good sky view         | 5–15 m                                 |
| Phone indoors                         | 15–65 m                                |
| Vehicle tracker with a proper antenna | 3–10 m                                 |
| Some vehicle APIs                     | reports `0`, meaning _no figure given_ |

A fix is a circle, not a point. A device standing still will drift across
several metres between reports, and the companion integration inflates each
zone by the reported accuracy before testing containment, so a device can
match a zone whose edge it is several metres outside.

**This works well for:**

- property boundaries, paddocks, yards, car parks
- "is the car home or not", "is the van on site"
- any zone whose smallest dimension is comfortably larger than the accuracy of
  the devices you are testing against: as a rule of thumb, 20 m across or
  more

**This works badly for:**

- rooms inside a building. A 4 m × 5 m room is smaller than the error circle of
  a phone standing still in it. Adjacent rooms will swap at random, and a
  device _outside the building_ can match one: a car parked on a driveway has
  been observed matching an indoor kitchen zone, because a 5 m accuracy ring
  reached it.
- anything needing pinpoint or "which side of this line" precision
- distinguishing places a few metres apart

**For room-level presence indoors, use another tool.** The limit is the
positioning technology, not the polygon. Wi-Fi access-point association,
Bluetooth room sensors (ESPresense and similar), or mmWave occupancy sensors
solve that problem. This add-on is for outdoor, property-scale zones.

The editor's optional tracker overlay (below) shows each device's accuracy ring
against your zones, so you can see whether a boundary is meaningful at the
scale you have drawn it.

## Configuration

All options live under **Settings → Add-ons → Polygonal Zones → Configuration**. The only setting most people change is `zone_colour`.

| Option                    | Type               | Default  | What it does                                                                                                                                                                                                                                                                                                                                                               |
| ------------------------- | ------------------ | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `zone_colour`             | string             | `purple` | Colour used to render zones on the map. A CSS colour name (letters only) or a hex value such as `#800080`. `rgb()` and `hsl()` are refused.                                                                                                                                                                                                                                |
| `theme`                   | list               | `auto`   | `auto` follows the OS `prefers-color-scheme`. Set `light` or `dark` to override. It sets the editor palette and, with **Map style** on Auto, the map.                                                                                                                                                                                                                      |
| `allow_all_ips`           | bool               | `false`  | No longer grants anything. Kept so existing configurations still load; when `true`, the add-on logs an error at startup telling you to set `save_token` instead. See **LAN access**.                                                                                                                                                                                       |
| `save_token`              | password           | empty    | Required for every request that does not come through Home Assistant (the sidebar or **Open Web UI**): it must carry `X-Save-Token: <value>`. Leave it empty for automatic setup: the add-on generates one for Home Assistant. Set it yourself only for the LAN URL flow. See **LAN access**.                                                                              |
| `trusted_proxies`         | string             | empty    | Comma-separated list of the IP address of each reverse proxy whose `X-Forwarded-For` header should be honoured. List each proxy's own IP rather than a range: every address listed is believed about which client it forwards for, and that is what the add-on's log and failed-attempt limit record. Leave empty unless you front the add-on with your own reverse proxy. |
| `overlay_entities`        | list of strings    | empty    | Entity ids (for example `device_tracker.my_car`) to plot on the map. At most 25 are used. See **Plotting live positions on the map**.                                                                                                                                                                                                                                      |
| `tracker_refresh_seconds` | integer, 0 to 3600 | `60`     | Seconds between position updates while the editor is open. `0` loads them once. 1 to 9 are raised to 10. Does nothing while `overlay_entities` is empty.                                                                                                                                                                                                                   |
| `log_level`               | list               | `info`   | One of `debug`, `info`, `warning`, `error`, `critical`. Use `debug` when troubleshooting.                                                                                                                                                                                                                                                                                  |

### Network ports

The add-on's web interface runs on port 8000 inside the container. Use the editor through Home Assistant: the **Open Web UI** button or the sidebar entry. That route (ingress) needs no extra configuration.

Direct LAN access on `http://<your-ha-host>:8000/` is off by default (`8000/tcp: null` in the add-on manifest). To enable it, go to **Settings → Add-ons → Polygonal Zones → Network**, assign a host port, and restart the add-on. You need this for `curl` backups, for a reverse proxy, and for an integration older than 1.16.0 that fetches the zones over the LAN.

Home Assistant itself does not need the host port. Core reaches the container's port 8000 on the add-on network, which is the address discovery hands over. Every non-ingress request needs `save_token`: see **LAN access**.

**The LAN port is plain HTTP.** The add-on does not terminate TLS, so `save_token`, zones and tracker positions cross your network unencrypted. Use it only on a network you trust, or put a TLS-terminating reverse proxy in front of it (see **Pointing the integration at the add-on**).

### LAN access (`save_token`)

Only requests that come through Home Assistant's ingress (TCP peer `172.30.32.2`) are served without a token. Every other request needs the header `X-Save-Token: <value>` matching `save_token`, whatever it asks for: `GET /zones.json`, `GET /trackers.json`, `GET /trackers/here_since`, `POST /save_zones`, `/config.json` and the editor's own page and scripts. With `save_token` empty, the add-on generates one for Home Assistant's discovery message, and nothing outside Home Assistant can reach it. `/healthz` is the one exception: it stays open so health checks work.

**`allow_all_ips` no longer grants anything.** Up to 0.5.7 it let any client on your network read and overwrite the zones with no token. When it is on, the add-on logs an error at startup telling you to set `save_token` instead.

**The editor is used from Home Assistant, not from the LAN port.** A browser cannot add a header to a page it navigates to, so opening `http://<ha-host>:8000/` directly gets `403`. Use the sidebar or **Open Web UI**.

**Ingress is for the editor, not for the integration's data fetch.** Ingress is Home Assistant's browser-session proxy and needs a logged-in user, so a background component like the integration cannot use it to poll a URL. The integration fetches `zone_urls` directly over HTTP, so it needs `save_token` like any other LAN client. That holds even when it runs inside Core on the same host: Core can reach the add-on's internal hostname, but that request does not come from the ingress address and is refused without the token.

Every refusal looks identical from the outside: `403` and `{"error": "not authorised"}`, so the response does not reveal whether a token is set. The precise reason is in the add-on's own log. Ten failed attempts from one client within a minute earn `429` for the rest of that minute.

The companion integration sends the header itself from [v1.15.0](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones/releases/tag/v1.15.0), in one of two ways.

- **Automatic setup** (integration 1.16.0 and later): the token travels in the discovery message, so there is nothing to type.
- **Manual setup**: put this add-on's `save_token` in the integration's **Add-on save token** field (`zone_source_token`).

In both cases the token goes only to a local add-on address, never to a public source. An older integration, or an empty field on a manual setup, gets `403` and loads no zones.

Pick a long random string (the field is masked in the UI). To rotate, change the value and restart the add-on.

The token is never logged. The `Loaded options:` line at startup shows `save_token: ***` when a token is set and `save_token: not set` when it is empty.

### Backing up / restoring zones with `curl`

The zones are kept in `/data/polygonal_zones/zones.json` inside the container, but you don't need a shell to read them. With a host port mapped and `save_token` set:

```sh
# Backup
curl -H 'X-Save-Token: <yourtoken>' \
  http://<ha-host>:8000/zones.json > zones-backup.json

# Restore
curl -X POST -H 'Content-Type: application/json' \
  -H 'X-Save-Token: <yourtoken>' \
  --data-binary @zones-backup.json \
  http://<ha-host>:8000/save_zones
```

A restore must be sent as `Content-Type: application/json`, as above; any other content type is refused with `415`.

`GET /zones.json` returns an `ETag` header (a fingerprint of the file's current contents). Pass it back as `If-Match` on `POST /save_zones` to refuse the write if anything changed in between (the editor does this by default). Plain `curl` posts without `If-Match` are last-write-wins.

`GET /zones.json` also honours `If-None-Match` and returns a `Last-Modified` header. The companion integration reads the zones file at startup and again whenever you call the `reload_zones` action; it does not poll. A `304 Not Modified` answer means the file is unchanged and your cached copy is current. It helps those on-demand fetches and any script that requests the file repeatedly:

```sh
# First request
curl -D headers1.txt -H 'X-Save-Token: <yourtoken>' \
  http://<ha-host>:8000/zones.json > zones.json
etag=$(awk -F': ' 'tolower($1)=="etag"{print $2}' headers1.txt | tr -d '\r')

# Later requests: 304 means no change, use the cached copy
curl -I -H 'X-Save-Token: <yourtoken>' -H "If-None-Match: $etag" \
  http://<ha-host>:8000/zones.json
```

## Plotting live positions on the map (`overlay_entities`)

`overlay_entities` lets the editor plot live tracker positions over the map, so you can see whether a device falls inside a boundary, and by how much.

It is empty by default, and while it is empty this add-on asks Home Assistant for nothing at all.

```yaml
overlay_entities:
  - device_tracker.my_car
  - device_tracker.the_van
```

Each device appears on the map as a marker with a dashed ring showing its reported GPS accuracy. The sidebar lists which zones it is inside and how far it sits from their edges.

Click a marker for its popup:

- the position error and battery level, when the tracker reports them;
- **Here since**, when it arrived where it is now. Movement within twice its position error, or 20 m if that is more, is ignored.

Working out **Here since** reads up to 24 hours of that tracker's history from Home Assistant when you open the popup, and again if it moves while open. Nothing is stored. The line reads:

| Shows                      | Meaning                                                                                                             |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `Here since checking…`     | The lookup is running.                                                                                              |
| a time, or a date and time | When it arrived. The date is shown when that was not today.                                                         |
| `Here since over 24 h`     | It has not moved for the whole 24 hours, so the real time is earlier.                                               |
| `Here since unknown`       | There is no usable history, the lookup failed or timed out (10 seconds), or the add-on was busy with other lookups. |

The readout measures; it does not predict. It tells you a device is "3.8 m outside Annex (within its accuracy)". It does not tell you which zone the integration will report, because the integration makes that decision with rules that are deliberately not duplicated here.

Each entity is fetched from Home Assistant and reduced to six fields: entity id, friendly name, state, coordinates, GPS accuracy and battery level. Every other attribute is discarded, so it never reaches the browser. Coordinates are rounded to four decimal places (about 11 m).

### How often positions refresh (`tracker_refresh_seconds`)

While the editor is open it re-reads the listed entities every `tracker_refresh_seconds` seconds (default 60) and moves the markers in place. Set it to `0` to load positions once when the page opens. Values from 1 to 9 are raised to 10, because each poll is one Supervisor call per listed entity for every open tab. The largest accepted value is 3600.

```yaml
overlay_entities:
  - device_tracker.my_car
tracker_refresh_seconds: 30
```

Polling pauses while the browser tab is hidden and catches up when you return. If Home Assistant cannot be asked about an entity on a poll, its last position stays on the map. An entity Home Assistant answers for without coordinates is removed. The option does nothing while `overlay_entities` is empty.

### Before you add people

Positions and arrival times are served from `GET /trackers.json` and `GET /trackers/here_since`, under the same rules as `/zones.json`: ingress, or `save_token` from anywhere else. Anyone holding the token can read them over your LAN. List only what you are content to expose on those terms. Vehicles are the intended case. A household member's phone is a decision to make deliberately, and ideally with them.

At most 25 entities are fetched. Anything that is not a valid entity id is ignored with a warning in the log.

### The `homeassistant_api` grant

Reading those states needs Home Assistant's state API, so the add-on's manifest (`config.yaml`) sets `homeassistant_api: true`. The grant applies when the add-on is installed, whatever you configure. Leaving `overlay_entities` empty does not withhold it; it only means the add-on uses none of it.

The grant is not read-only: it gives the add-on Home Assistant API access in general. What the add-on does with it is narrow. It issues `GET /states/<entity_id>` for each entity you list, and a history read for one of them when you open its popup (again if it moves while open). With the list empty it does nothing.

## Integration with the Polygonal Zones HA integration

The companion [Polygonal Zones integration](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones) reads `/zones.json` from this add-on. It fetches the zones file at startup and again whenever you call the `reload_zones` action. It never reaches the add-on through ingress, even on the same host: it needs a mapped host port and `save_token`, as described in **LAN access**.

### Availability and failure modes

The add-on serves `/zones.json` from local disk. What happens when the zones file becomes unreadable at runtime (disk full, ownership drift after a Supervisor remount, filesystem corruption) depends on which side notices first:

- **The add-on** returns `503 Service Unavailable` with `{"error":"zones file unreadable"}` and logs the error with a traceback. The Docker `HEALTHCHECK` also fails, because `/healthz` checks that the zones file is readable rather than only that the process is alive, so Docker marks the container unhealthy.
- **The Supervisor** restarts the add-on when the container's health check (`/healthz`, every 30 seconds) fails three times in a row, or when the web service stops. It does this only while **Watchdog** is switched on in the add-on's **Info** tab, so switch it on.
- **The integration** sees the 503 the next time it tries to read zones (on startup or on a `reload_zones` call). By default it keeps its last-known-good state, so zone-based automations keep using the zone definitions it last fetched. If the add-on is down entirely (process gone, port unreachable), the integration surfaces an unavailable state, and automations that guard on `zone.* != 'unavailable'` stop firing until the add-on is back.

A file that is readable but not valid JSON is a different case. The add-on never parses `zones.json` when serving it, so it hands the bytes over as they are and the health check stays green. The editor then shows "Couldn't load zones".

**Recovery:**

1. Open **Settings → Add-ons → Polygonal Zones → Log**. An unreadable file shows as `Failed to read /data/polygonal_zones/zones.json`. A corrupt one leaves no add-on log line; the "Couldn't load zones" message in the editor is the sign.
2. Common causes: ownership drift on `/data` (usually resolved by a restart, since `cont-init.d/00-fix-perms` re-chowns on boot), disk full, or file corruption after an ungraceful host shutdown.
3. If the file is corrupt, restore `/data/polygonal_zones/zones.json` from the latest Home Assistant backup that predates the failure (**Settings → System → Backups**), or restore your own `curl` copy. Until then the integration keeps its last-known-good zones.

### Pointing the integration at the add-on

#### Recommended: automatic setup (integration v1.16.0 or later)

The add-on announces itself to Home Assistant at every start. Open **Settings → Devices &
services**, and **Polygonal Zones** appears as a discovered integration: confirm it once and setup
is done. There is no URL to type, no host port to map and no token to copy — the add-on generates
one that only Home Assistant ever sees.

From then on, every save asks Home Assistant to re-read the zones, so an edit appears without a
manual reload. The line under **Save** says what happened. With an older integration it says the
reload needs v1.16.0 or later, and calling the `reload_zones` action still works.

#### Alternative: LAN URL with `allow_private_urls: true`

Use this for an integration older than v1.16.0, or when the add-on runs somewhere Home Assistant
cannot discover. From companion integration [v1.12.0](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones/releases/tag/v1.12.0) the integration has an opt-in boolean option `allow_private_urls` in its advanced settings. Turn it on and point `zone_urls` at the add-on's LAN URL:

- In this add-on: map a host port and set `save_token: <value>`. The integration (v1.15.0 or later) must have the same value in its **Add-on save token** field; otherwise it gets `403`.
- In the integration: enable `allow_private_urls` in the advanced section, set `zone_urls: http://<ha-host-lan-ip>:8000/zones.json`. Use the IP address, not a hostname such as `homeassistant`: the integration does not send the token to it, so the add-on answers `403`.

The integration refuses URLs that point at private network addresses (an SSRF defence). Loopback, link-local (cloud-metadata), multicast and reserved ranges stay blocked. `allow_private_urls` unlocks only RFC 1918 space (`192.168.x.x`, `10.x.x.x`, `172.16–31.x.x`), which is all a typical home network needs. The request is plain HTTP, so the token crosses your LAN unencrypted (see **Network ports**).

#### Alternative: a private reverse proxy with TLS on a non-RFC-1918 hostname

If you'd rather not enable `allow_private_urls`, or you run integration v1.11.x or earlier, front the add-on with a reverse proxy you control (nginx, Caddy, Traefik, HA's NGINX Proxy Manager add-on) that terminates TLS under a public-resolving hostname such as `zones.yourdomain.tld`. Point the integration at `https://zones.yourdomain.tld/zones.json`. The DNS name resolves publicly, so the integration's SSRF guard lets it through, but the listener stays on your LAN and no zones data leaves your network. The integration never sends its token to a public hostname, so the proxy must add it. The token also unlocks saving zones and tracker positions, so forward only `GET /zones.json` (nginx: `location = /zones.json { limit_except GET { deny all; } proxy_set_header X-Save-Token <value>; proxy_pass http://<ha-host>:8000; }`) and nothing else. Keep the proxy off the public internet: anyone who reaches it can read your zones.

#### Last resort: a public-CDN mirror (privacy warning)

Hosting `zones.json` on a public-facing server (GitHub Pages, S3, Cloudflare Pages) is occasionally suggested. Do not do this without understanding the privacy cost. Your polygon geometry encodes the precise shape and location of your home, workplace, school runs, and any other place you have drawn a zone. Publishing it to a public CDN means:

- The data is world-readable: any visitor, crawler, scraper or search engine can fetch it.
- Public CDNs cache and replicate content across their global edge networks. Once it has been served, you cannot assume you can fully revoke it.
- Major providers (GitHub, AWS, Cloudflare) are subject to the US CLOUD Act, so the data can be disclosed to US authorities regardless of where you live.
- Search engines index content hosted on common providers. Your home coordinates become discoverable by name or URL.
- Deleting the file later does not remove archived copies (Wayback Machine, third-party scrapers).

If you must go this route (no control over DNS, no reverse proxy, and an integration older than v1.12.0), use a private or unlisted bucket with a strong pre-signed URL, rotate the URL regularly, and treat any leak of the URL as a leak of the geometry.

## Usage

### Accessing the web interface

- **Info page**: press **Open Web UI** on the add-on's info page.
- **Sidebar**: enable **Show in sidebar** in the add-on configuration for a permanent shortcut.

### Saving changes

Changes are not written until you press **Save** at the bottom of the sidebar.

An amber dot appears on **Save** while there are changes not yet written to disk. It clears once the save succeeds.

A refused save shows the server's reason under the button.

#### If a save conflicts or times out

A conflict means another session saved first. Saving stays refused until you reload, so the other session's zones are not overwritten. After a conflict or a timeout, **Download unsaved zones** saves the zones on screen as a file. Then either:

- reload the page and re-apply your changes; or
- download, reload, and use **Replace zones from file** to put the downloaded zones back. This replaces every zone with the file's zones and discards the other session's changes.

### Zones file

The zones are stored as a GeoJSON `FeatureCollection` at `http(s)://[HOST]:[PORT]/zones.json` (port 8000 inside the container), and on disk at `/data/polygonal_zones/zones.json`. The companion integration reads this file. See **Backing up / restoring zones with curl** for backups, and **Privacy and data** for what Home Assistant backups keep.

Each zone has a stable `properties.id`, filled in by the add-on when missing. A position is `[longitude, latitude]`. A third number (altitude) is accepted and dropped on save. Four or more values in a position are refused with `422`. A save is refused unless every feature is a Polygon or MultiPolygon with closed rings and at most 1000 vertices, and names and ids are unique.

### Features

#### Viewing zones

- All zones are listed in the sidebar and drawn on the map.
- While the list is loading it shows "Loading zones…". On a new install with no zones it shows "No zones yet. Draw one on the map to get started."
- If loading fails, the list shows "Couldn't load zones…" and the **Replace zones from file** button. Check the add-on log.

#### Map style

- Use the **Map style** dropdown at the top of the sidebar to change the background map: **Street map** (OpenStreetMap), **Dark** (CARTO), or **Satellite** (Esri World Imagery).
- **Auto** follows the `theme` option when it is `light` or `dark`, and otherwise your OS or browser light/dark setting. It picks Street map in light and Dark in dark.
- Your choice is kept in the browser.

#### Adding zones

The draw controls are at the top left of the map.

- Click the polygon button, then click the map to place points. Click the first point again to finish.
- Or click the rectangle button, then click two opposite corners. It is saved as a plain polygon.

#### Editing zones

- Click the edit button next to a zone's name in the sidebar.
- Drag the points to change the shape. You can also rename the zone in the sidebar.
- Press **Done** next to the zone's name when finished. That only confirms the change in the editor; press the sidebar **Save** to write it to disk.

#### Deleting zones

- Click the trash button in the map toolbar to enter removal mode.
- Click a zone on the map to remove it.
- Press the sidebar **Save** to write the change to disk.

## Privacy and data

**Zones and settings are in every backup.** Your zones are stored at `/data/polygonal_zones/zones.json`. Everything in `/data` is included in every Home Assistant backup that includes this add-on, and so is the add-on's `options.json`, which holds your `save_token` and your `overlay_entities` list. Deleting a zone from the editor does not remove it from backups taken before the deletion. If you are removing zones for privacy reasons (after a move, after drawing over a sensitive area, or in response to a subject-access or deletion request), also remove old backups via **Settings → System → Backups → ⋮ → Remove**, and any copies you keep outside Home Assistant.

**The LAN port is not encrypted.** If you map a host port, the token, zones and tracker positions travel over plain HTTP. See **Network ports**.

**Map tiles come from a third party.** Your browser fetches the map background directly from the provider of the map style in use: OpenStreetMap, CARTO or Esri. Auto picks OpenStreetMap or CARTO by theme; Esri is used only if you choose Satellite. That provider sees your IP address, which area you are viewing (the tiles you request) and your Home Assistant address (for example a `*.ui.nabu.casa` hostname) as the referring site.

**Your browser remembers the last map view.** The editor stores where you last panned and zoomed in your browser's localStorage, under `pz:viewport`, so it reopens there. Your map style and sidebar choice are kept the same way (`pz:basemap`, `pz:sidebar`). Nothing of this is sent to the add-on. Clearing the site data for your Home Assistant address removes it.

**Tracker positions are passed through, not kept.** `GET /trackers.json` returns, for each entity in `overlay_entities`, only its entity id, friendly name, state, GPS accuracy, battery level, and coordinates rounded to four decimal places (about 11 m). `GET /trackers/here_since` returns only when one of them arrived where it is, worked out from up to 24 hours of its history. The add-on stores none of it: each request asks Home Assistant afresh.

**Logs.** The add-on log records the address of each client whose request it refuses, and of each save that conflicts with another session's. At `debug` level it also records the entity id of any tracker lookup that fails. Values a client sends are shown escaped. The Supervisor controls how long logs are kept, not this add-on.

The companion integration has its own [privacy notes](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones/blob/main/docs/privacy.md).
