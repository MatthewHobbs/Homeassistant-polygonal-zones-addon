# Polygonal Zones Editor

Create and manage polygonal zones inside Home Assistant. Draw shapes on a map, name them, and have the companion [Polygonal Zones integration](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones) consume them for location-based automations.

> **Note — 32-bit hosts.** Home Assistant 2025.12 (released 2025-12-03) deprecated `armhf`, `armv7`, and `i386` as supported host architectures for add-ons. The last release of this add-on for those arches is **0.2.25** — from 0.2.26 onward, only `aarch64` and `amd64` images are published. If you're running on a 32-bit host (Raspberry Pi 0/1, 32-bit OS on a Pi 2/3, 32-bit Intel Atom, etc.), 0.2.25 is the last version you can run; to keep receiving updates, move your host to a 64-bit HA OS installation. Supervisor stops offering updates automatically once the architecture mismatch is detected, so there's no risk of accidentally pulling an incompatible image.

## What this add-on is good for — and what it isn't

Zones drawn here are compared against positions reported by phones, cars and
trackers. **The accuracy of the answer is set by those devices, not by how
carefully you draw.** A boundary placed to the centimetre is still only as good
as the fix being tested against it.

For a sense of scale, from real devices:

| Source | Typical reported accuracy |
| --- | --- |
| Phone outdoors, good sky view | 5–15 m |
| Phone indoors | 15–65 m |
| Vehicle tracker with a proper antenna | 3–10 m |
| Some vehicle APIs | reports `0`, meaning *no figure given* |

A fix is a circle, not a point. A device standing still will drift across
several metres between reports, and the companion integration inflates each
zone by the reported accuracy before testing containment — so a device can
match a zone whose edge it is several metres outside.

**This works well for:**

- property boundaries, paddocks, yards, car parks
- "is the car home or not", "is the van on site"
- any zone whose smallest dimension is comfortably larger than the accuracy of
  the devices you are testing against — as a rule of thumb, **20 m across or
  more**

**This works badly for:**

- rooms inside a building. A 4 m × 5 m room is smaller than the error circle of
  a phone standing still in it. Adjacent rooms will swap at random, and a
  device *outside the building* can match one: a car parked on a driveway has
  been observed matching an indoor kitchen zone, because a 5 m accuracy ring
  reached it.
- anything needing pinpoint or "which side of this line" precision
- distinguishing places a few metres apart

**If you want room-level presence indoors, this is the wrong tool** and no
amount of careful drawing will fix it — the limit is the positioning
technology, not the polygon. Wi-Fi access-point association, Bluetooth room
sensors (ESPresense and similar), or mmWave occupancy sensors solve that
problem properly. This add-on is for outdoor, property-scale zones.

The editor's optional tracker overlay (below) exists partly to make this
visible: it shows each device's accuracy ring against your zones, so you can
see whether a boundary is meaningful at the scale you have drawn it.

## Configuration

All options live under **Settings → Add-ons → Polygonal Zones → Configuration**. Defaults are sensible for a stock Home Assistant install — the only setting most people change is `zone_colour`.

| Option            | Type     | Default   | What it does                                                                                                              |
| ----------------- | -------- | --------- | ------------------------------------------------------------------------------------------------------------------------- |
| `zone_colour`     | string   | `purple`  | Colour used to render zones on the map (any CSS colour name or `#rrggbb`).                                                |
| `theme`           | list     | `auto`    | `auto` follows the OS `prefers-color-scheme`. Set `light` or `dark` to override.                                          |
| `allow_all_ips`   | bool     | `false`   | **No longer grants anything.** Kept so existing configurations still load; when `true`, the add-on logs an error at startup telling you to set `save_token` instead. See **LAN access** below. |
| `save_token`      | password | *(empty)* | Required for every request that does not come through Home Assistant (the sidebar or **Open Web UI**): it must carry `X-Save-Token: <value>`. Empty means nothing outside Home Assistant can reach the add-on. See **LAN access**. |
| `trusted_proxies` | string   | *(empty)* | Comma-separated list of the IP address of each reverse proxy whose `X-Forwarded-For` header should be honoured. List each proxy's own IP rather than a range: every address listed is believed about which client it forwards for, and that is what the add-on's log and failed-attempt limit record. Leave empty unless you front the add-on with your own reverse proxy. |
| `log_level`       | list     | `info`    | One of `debug`, `info`, `warning`, `error`, `critical`. Bump to `debug` when troubleshooting.                             |

### Network ports

The add-on's web interface runs on port 8000 inside the container. Use the editor through Home Assistant: the **Open Web UI** button or the sidebar entry. That route (ingress) needs no extra configuration.

Direct LAN access on `http://<your-ha-host>:8000/` is **disabled by default** (`8000/tcp: null` in the add-on manifest). To enable it, go to **Settings → Add-ons → Polygonal Zones → Network**, assign a host port, and restart the add-on. This is how the companion integration fetches `zone_urls` and how `curl` backups work, and every such request needs `save_token`: see **LAN access** below.

### LAN access (`save_token`)

Only requests that come through Home Assistant's ingress (TCP peer `172.30.32.2`) are served without a token. Every other request needs the header `X-Save-Token: <value>` matching `save_token`, whatever it asks for: `GET /zones.json`, `GET /trackers.json`, `POST /save_zones`, `/config.json` and the editor's own page and scripts. With `save_token` empty, nothing outside Home Assistant can reach the add-on. `/healthz` is the one exception: it stays open so health checks work.

**`allow_all_ips` no longer grants anything.** Up to 0.5.7 it let any client on your network read and overwrite the zones with no token. It is kept in the configuration so existing setups still load, and when it is on the add-on logs an error at startup telling you to set `save_token` instead.

**The editor is used from Home Assistant, not from the LAN port.** A browser cannot add a header to a page it navigates to, so opening `http://<ha-host>:8000/` directly gets `403`. Use the sidebar or **Open Web UI**.

**Ingress is for the editor, not for the integration's data fetch.** Ingress is Home Assistant's browser-session proxy and needs a logged-in user, so a background component like the integration cannot use it to poll a URL. The integration fetches `zone_urls` directly over HTTP, so it needs `save_token` like any other LAN client. That holds even when it runs inside Core on the same host: Core can reach the add-on's internal hostname, but that request does not come from the ingress address and is refused without the token, as the Supervisor pilot asserts.

Every rejection looks identical from the outside: `403` and `{"error": "not authorised"}` (deliberately: the response alone must not reveal whether a token is even configured). The precise reason is in the add-on's own log. Ten failed attempts from one client within a minute earn `429` for the rest of that minute.

The companion integration can send the header itself as of [PR #95](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones/pull/95) (merged 2026-09-27, not yet in a tagged release): see its `zone_source_token` option, bound to this add-on's own origin and never sent elsewhere or across a redirect. Until a release containing it is out, the integration cannot read zones from this add-on over the LAN.

Pick a long random string (the field is masked in the UI). To rotate, change the value and restart the add-on — there is no migration needed.

The token is **never** logged. The `Loaded options:` line at startup prints it as `***`.

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

`GET /zones.json` returns an `ETag` header (a fingerprint of the file's current contents). Pass it back as `If-Match` on `POST /save_zones` to refuse the write if anything changed in between (the add-on's own UI does this by default). Plain `curl` posts without `If-Match` keep their last-write-wins behaviour, so existing scripts are unaffected.

**Conditional-GET cache check.** Since 0.2.29, `GET /zones.json` also honours `If-None-Match` and returns a `Last-Modified` header. The companion integration is **not** a polling loop — it reads the zones file at startup and again whenever you trigger a `reload_zones` action. So the ETag / 304 benefit applies to those on-demand fetches (a `304 Not Modified` response — meaning "file unchanged, use your cached copy, no body to re-download" — avoids re-parsing the full GeoJSON on every `reload_zones` call) and to any external script that requests the file repeatedly. Example:

```sh
# First request
curl -D headers1.txt -H 'X-Save-Token: <yourtoken>' \
  http://<ha-host>:8000/zones.json > zones.json
etag=$(awk -F': ' '/^ETag: /{print $2}' headers1.txt | tr -d '\r')

# Subsequent requests — 304 means "no change, use the cached copy"
curl -I -H 'X-Save-Token: <yourtoken>' -H "If-None-Match: $etag" \
  http://<ha-host>:8000/zones.json
```

## Plotting live positions on the map (`overlay_entities`)

Drawing a zone accurately is guesswork if you cannot see what it is supposed to
classify. `overlay_entities` lets the editor plot live tracker positions over
the map, so you can see whether a device actually falls inside a boundary — and
by how much.

It is **empty by default**, and while it is empty this add-on asks Home
Assistant for nothing at all.

```yaml
overlay_entities:
  - device_tracker.my_car
  - device_tracker.the_van
```

When configured, each device appears on the map as a marker with a dashed ring
showing its reported GPS accuracy, and the sidebar lists which zones it is
inside and how far it sits from their edges. Clicking a marker shows its
position error, battery level and when it arrived where it is now. Working out
the arrival reads up to 24 hours of that tracker's history from Home Assistant
when you open its popup, and again if it moves while open; nothing is stored.

That readout **measures; it does not predict**. It will tell you a device is
"3.8 m outside Annex (within its accuracy)" — it will not tell you which zone
the integration is going to report, because that decision is made by the
integration using rules that are deliberately not duplicated here. Two
implementations of the same rule drift, and an editor that is confidently wrong
about a verdict is worse than one that offers none.

Each entity is fetched from Home Assistant and reduced to six fields —
entity id, friendly name, state, coordinates, GPS accuracy and battery level.
Every other attribute is discarded here, so it never reaches the browser.
Coordinates are rounded to four decimal places (about 11 m), which is finer
than any boundary you can place by hand and avoids putting sub-millimetre
positions into a response.

### How often positions refresh (`tracker_refresh_seconds`)

While the editor is open it re-reads the listed entities every
`tracker_refresh_seconds` seconds (default 60) and moves the markers in place,
without a reload. Set it to `0` to load positions once when the page opens.
Values from 1 to 9 are raised to 10: each poll is one Supervisor call per
listed entity for every open tab, and trackers rarely report faster than that
anyway. The largest accepted value is 3600.

```yaml
overlay_entities:
  - device_tracker.my_car
tracker_refresh_seconds: 30
```

Polling pauses while the browser tab is hidden and catches up when you return.
If Home Assistant cannot be asked about an entity on a poll, its last position
stays on the map rather than vanishing; an entity Home Assistant answers for
without coordinates is removed, because that is a real report of no position.
The option does nothing while `overlay_entities` is empty.

### Before you add people

The positions and arrival times are served from `GET /trackers.json` and
`GET /trackers/here_since`, protected by the same
rules as `/zones.json`: ingress, or `save_token` from anywhere else. Anyone
holding the token can read them over your LAN. List only what you are content
to expose on those terms. Vehicles are the intended case; a
household member's phone is a decision to make deliberately, and ideally with
them.

At most 25 entities are fetched, and anything that is not a valid entity id is
ignored with a warning in the log rather than silently rewritten.

### The `homeassistant_api` grant

Reading those states needs Home Assistant's state API, so the add-on's
manifest (`config.yaml`) sets `homeassistant_api: true`. The grant is part of
the manifest and applies when the add-on is installed, whatever you configure:
leaving `overlay_entities` empty does not withhold it, it only means the
add-on uses none of it.

The grant itself is not read-only: it gives the add-on Home Assistant API
access in general. What the add-on does with it is narrow: it only ever issues
`GET /states/<entity_id>` for each entity you list, a history read for one of
them when you open its popup, and nothing at all while the list is empty.

## Integration with the Polygonal Zones HA integration

The companion [Polygonal Zones integration](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones) reads `/zones.json` from this add-on. It fetches the zones file at **startup** and again whenever you call the `reload_zones` action — it does not poll on a schedule. It never reaches the add-on through ingress, even on the same host: it needs a mapped host port and `save_token`, as described in **LAN access** above.

### Availability and failure modes

The add-on serves `/zones.json` from local disk; the companion integration reads it on demand. What happens when the zones file becomes unreadable at runtime (disk full, ownership drift after a Supervisor remount, filesystem corruption) depends on which side notices first:

- **The add-on** returns `503 Service Unavailable` with `{"error":"zones file unreadable"}` and logs the OSError / traceback via `_LOGGER.exception`. The Docker `HEALTHCHECK` also fails, because `/healthz` checks that the zones file is readable rather than only that the process is alive, so Docker marks the container unhealthy.
- **The Supervisor** restarts the add-on when the container's health check (`/healthz`, every 30 seconds) fails three times in a row, or when the web service stops. It does this only while **Watchdog** is switched on in the add-on's **Info** tab, so switch it on.
- **The integration** sees the 503 the next time it tries to read zones (on startup or on a `reload_zones` call). Its default behaviour is to retain its last-known-good state — zone-based automations continue to fire based on the zone definitions it last successfully fetched, rather than silently losing geofencing coverage. If the add-on is down entirely (process gone, port unreachable), the integration surfaces an unavailable state; automations that guard on `zone.* != 'unavailable'` will stop firing until the add-on is back.

**Recovery:**
1. Pull the add-on log from **Settings → Add-ons → Polygonal Zones → Log**. Look for `Failed to read /data/polygonal_zones/zones.json` or a JSON parse error during startup.
2. Common root causes: ownership drift on `/data` (rare, usually resolved by a container restart — `cont-init.d/00-fix-perms` re-chowns on boot), disk full, or file corruption after an ungraceful host shutdown.
3. If the file is present but corrupted, the fastest path is to restore `/data/polygonal_zones/zones.json` from the latest HA snapshot that predates the failure (**Settings → System → Backups**). The integration's retained state means automations that were working before the outage keep working through the restore.

### Pointing the integration at the add-on

**Recommended (happy path) — LAN URL with `allow_private_urls: true`.** As of companion integration [v1.12.0](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones/releases/tag/v1.12.0), the integration has an opt-in boolean option `allow_private_urls` under its advanced-settings section. Turn it on, point `zone_urls` at the add-on's LAN URL, and you're done:

- In this add-on: map a host port and set `save_token: <value>`. The integration must send the same value as `zone_source_token` (PR #95, not yet in a release); a version that cannot send it gets `403`.
- In the integration: enable `allow_private_urls` in the advanced section, set `zone_urls: http://<ha-host-lan-ip>:8000/zones.json`.

The integration includes an SSRF (Server-Side Request Forgery) defence that prevents it from fetching URLs pointing at private network addresses — loopback, link-local (cloud-metadata), multicast, and reserved ranges are still blocked. `allow_private_urls` unlocks only RFC-1918 space (private home-network addresses such as `192.168.x.x`, `10.x.x.x`, `172.16–31.x.x`), which is the only space you need for a typical home-network install.

#### Alternative — private reverse proxy with TLS on a non-RFC-1918 hostname

If you'd rather not enable `allow_private_urls`, or you're running integration v1.11.x or earlier, you can still front the add-on with a reverse proxy you control (nginx, Caddy, Traefik, HA's own NGINX Proxy Manager add-on) that terminates TLS under a public-resolving hostname such as `zones.yourdomain.tld`. Point the integration at `https://zones.yourdomain.tld/zones.json`. The DNS name resolves publicly (so the integration's SSRF guard lets it through), but the listener is still on your LAN — no zones data leaves your network. Pair with `save_token` and basic-auth at the proxy if the hostname is reachable from the public internet.

#### Last resort — public-CDN mirror (privacy warning)

Hosting `zones.json` on a public-facing server (GitHub Pages, S3, Cloudflare Pages, etc.) is occasionally suggested, but **do not do this without understanding the privacy cost**. Your polygon geometry encodes the precise shape and location of your home, workplace, school runs, and any other place you have drawn a zone. Publishing it to a public CDN means:

- The data is **world-readable** — any visitor, crawler, scraper or search engine can fetch it.
- Public CDNs **cache and replicate** content across their global edge networks. Once it's been served even once, you cannot assume you can fully revoke it.
- Major providers (GitHub, AWS, Cloudflare) are subject to the US CLOUD Act, so the data can be disclosed to US authorities regardless of where you live.
- Search engines **index** content hosted on common providers. Your home coordinates become discoverable by name/URL.
- Deleting the file later does **not** remove archived copies (Wayback Machine, third-party scrapers).

If you absolutely must go this route (e.g. you have no control over DNS, cannot stand up a reverse proxy, and cannot upgrade the integration to v1.12.0+), use a private / unlisted bucket with a strong pre-signed URL, rotate the URL regularly, and understand that any leak of the URL equates to a leak of the geometry.

## Usage

### Accessing the web interface

After installing and starting the add-on, you can access the web interface in two ways:

- **Info page**: press the **Open Web UI** button on the add-on's info page.
- **Sidebar**: enable **Show in sidebar** in the add-on configuration to add a permanent shortcut to the HA sidebar.

### Saving changes

**Important:** Any changes made to the zones need to be saved by pressing the **Save** button located at the bottom of the sidebar. Unsaved changes will not be persisted between restarts.

Whenever you have changes that haven't been written to disk yet, a small amber dot appears on the **Save** button. It clears once the save succeeds — so if you can see the dot, you still have unsaved work.

### Zones file

The zones are stored as a GeoJSON `FeatureCollection` at `http(s)://[HOST]:[PORT]/zones.json` (port 8000 inside the container), and on disk at `/data/polygonal_zones/zones.json`. The companion [Polygonal Zones integration](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones) reads this file. See the **Backing up / restoring zones with curl** section for the recommended backup workflow, and **Privacy and data** below for what Home Assistant backups keep.

### Features

#### Viewing zones

- A list of all the zones is displayed in the sidebar.
- The zones are also visualized on the map.
- While the list is loading it shows **"Loading zones…"**. On a brand-new install with no zones yet, it shows **"No zones yet — draw one on the map to get started."** If it stays stuck on "Loading zones…", check the add-on log.

#### Map style

- Use the **Map style** dropdown at the top of the sidebar to change the background map: **Street map** (OpenStreetMap), **Dark** (CARTO), or **Satellite** (Esri World Imagery).
- **Auto** follows your Home Assistant light/dark theme.

#### Adding zones

- Click the Draw Polygon Button (the button with a pentagon icon) located on the right side of the map.
- Click on the map to define the points of the polygon.
- Click on the first point again to complete the polygon.

#### Editing zones

- Click the Edit Button next to a zone's name in the sidebar.
- This will make the polygon editable on the map. Drag the points to modify the zone's shape.
- You can also rename the zone directly in the sidebar.
- When you're finished, press the **Done** button next to the zone's name. This only confirms the change in the editor — you still need to press the sidebar **Save** to write it to disk.

#### Deleting zones

- Click the Delete (trash) button in the map toolbar.
- Select the zones you want to delete by clicking on them. (Use **Clear All** in the delete toolbar to mark every zone at once.)
- Press **Save** in the delete toolbar to confirm the removal, or **Cancel** to back out. Note this is the map toolbar's own Save — it only updates the editor.
- Then press the sidebar **Save** to write the change to disk.

## Privacy and data

**Zones and settings are in every backup.** Your zones are stored at `/data/polygonal_zones/zones.json`. Everything in `/data` is included in every Home Assistant backup that includes this add-on (a full backup, or a partial one with the add-on ticked), and so is the add-on's `options.json`, which holds your `save_token` and your `overlay_entities` list. That means **deleting a zone from the editor does not remove it from backups taken before the deletion** — the old geometry stays in those backup files until you delete the backups themselves. Your polygon geometry encodes precise home, workplace and school locations, so if you're removing zones for privacy reasons (after a move, after accidentally drawing over a sensitive area, or in response to a subject-access or deletion request), also remove old backups via **Settings → System → Backups → ⋮ → Remove**, and any copies you keep outside Home Assistant.

**Map tiles come from a third party.** Your browser fetches the map background directly from the provider of the map style you pick: OpenStreetMap, CARTO, or Esri for satellite imagery. Every tile request tells that provider which area you are looking at and your IP address, and sends your Home Assistant address (for example your `*.ui.nabu.casa` hostname) as the referring site.

**Your browser remembers the last map view.** The editor stores where you last panned and zoomed in your browser's localStorage, under `pz:viewport`, so it reopens there; your map style and sidebar choice are kept the same way (`pz:basemap`, `pz:sidebar`). Nothing of this is sent to the add-on. Clearing the site data for your Home Assistant address removes it.

**Tracker positions are passed through, not kept.** `GET /trackers.json` returns, for each entity in `overlay_entities`, only its entity id, friendly name, state, GPS accuracy, battery level, and coordinates rounded to four decimal places (about 11 m). `GET /trackers/here_since` returns only when one of them arrived where it is, worked out from up to 24 hours of its history. The add-on stores none of it: each request asks Home Assistant afresh. See **Plotting live positions on the map** above.

**Logs.** The add-on log records, among other things, the address of each request it refuses. How long logs are kept is controlled by the Supervisor, not by this add-on.

The companion integration has its own [privacy notes](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones/blob/main/docs/privacy.md).
