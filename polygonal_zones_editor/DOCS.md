# Polygonal Zones Editor

Home Assistant now calls these **apps**. Some versions call them **add-ons**; it is the same thing, and the menus below use **app**.

Draw polygonal zones inside Home Assistant and let the companion [Polygonal Zones integration](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones) use them in location automations.

## Set it up

You need both halves: this app, and the integration that tracks devices against the zones.

1. Install this app (see the [installation steps](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones-addon#installation)), then start it.
2. Install the [Polygonal Zones integration](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones) and restart Home Assistant if it does not appear in the list.
3. Open **Settings → Devices & services**. **Polygonal Zones** appears as a discovered entry. Choose **Confirm**.
4. Open the entry's **⋮** menu, choose **Reconfigure**, and tick the `device_tracker` entities you want compared against your zones.

That is the whole setup for app 0.7.0 and integration 1.16.0 or later. There is no port to map, no URL to type and no token to copy: the app tells Home Assistant where it is, and generates a token that only Home Assistant sees. Every save in the editor then asks the integration to re-read the zones, so an edit is live without a reload.

**Nothing appeared under Devices & services?** Follow [If the app is not discovered](#if-the-app-is-not-discovered-manual-setup). **It is set up but a tracker is wrong?** See [If it isn't working](#if-it-isnt-working) and the integration's [troubleshooting page](https://matthewhobbs.github.io/Homeassistant-polygonal-zones/troubleshooting/).

## If the app is not discovered (manual setup)

Only for the cases the automatic path cannot cover: an app older than 0.7.0, an integration older than 1.16.0, or an app Home Assistant cannot discover (a different machine, a bridged network). You are wiring three things by hand — a port, a token, and the URL — and they have to agree.

1. **Give the app a port.** **Settings → Apps → Polygonal Zones → Network**, map host port `8000`, restart the app.
2. **Set a token in the app.** **Configuration** tab, `save_token` to a long random value, restart.
3. **Add the integration.** **Settings → Devices & services → Add integration → Polygonal Zones**.
4. **Point it at the app.** Zones URL `http://<app-LAN-IP>:8000/zones.json`. Use the IP address, not a name: the integration never sends a token to a name, so the app answers `403`.
5. **Turn on the private-network allowance and paste the token.** Both fields are advanced, so enable **Advanced mode** in your user profile first. Turn on **Allow private-network URLs (LAN)** and put the app's `save_token` in **App save token**.
6. **Choose your trackers**, and tick the tracking notice.

An integration older than 1.15.0 cannot send a token at all, so it can only reach an app that has open LAN access — which this app stopped offering in 0.5.8. Put a TLS reverse proxy in front instead: see [a private reverse proxy](#alternative-a-private-reverse-proxy-with-tls-on-a-public-name).

The LAN port is plain HTTP: the token and your zones cross the network unencrypted.

## Settings

All options are under **Settings → Apps → Polygonal Zones → Configuration**. Most people never open this page; the groups below say when each one matters.

### The two you might change

Appearance only.

| Option        | Type   | Default  | What it does                                                                                                                                                             |
| ------------- | ------ | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `zone_colour` | string | `purple` | Colour used for zones on the map. A CSS colour name in letters, or a hex value such as `#800080`. `rgb()` and `hsl()` are refused.                                       |
| `theme`       | list   | `auto`   | `auto` follows the operating system's `prefers-color-scheme`. Set `light` or `dark` to override it. It sets the editor colours and, with **Map style** on Auto, the map. |

### When something is wrong

| Option      | Type | Default | What it does                                                                              |
| ----------- | ---- | ------- | ----------------------------------------------------------------------------------------- |
| `log_level` | list | `info`  | One of `debug`, `info`, `warning`, `error`, `critical`. Use `debug` when troubleshooting. |

### Only if Home Assistant cannot discover the app

Setting this yourself is the manual path; with automatic setup it stays empty and the app generates one. It must then match the integration's **App save token** exactly.

| Option       | Type     | Default | What it does                                                                                                                                                                                                                                                                    |
| ------------ | -------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `save_token` | password | empty   | Required for every request that does not come through Home Assistant (the sidebar or **Open Web UI**), as `X-Save-Token: <value>`. Leave it empty for automatic setup: the app generates one for Home Assistant. Set it yourself only for the LAN URL flow. See **LAN access**. |

### Only if you plot live positions on the map

`overlay_entities` is the list of devices to draw. The other two exist to make that list safe and readable.

| Option                    | Type               | Default | What it does                                                                                                                                                                                                                                                                                                                           |
| ------------------------- | ------------------ | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `overlay_entities`        | list of strings    | empty   | Entity ids to plot on the map, for example `device_tracker.my_car`. At most 25 are used. See **Plotting live positions on the map**.                                                                                                                                                                                                   |
| `tracker_refresh_seconds` | integer, 0 to 3600 | `60`    | Seconds between position updates while the editor is open. `0` loads them once. Values from 1 to 9 are raised to 10. Does nothing while `overlay_entities` is empty.                                                                                                                                                                   |
| `trusted_proxies`         | string             | empty   | Comma-separated IP addresses of reverse proxies whose `X-Forwarded-For` header should be believed. List each proxy's own address, not a range: every address listed is trusted about which client it forwards for, and that is what the log and the failed-attempt limit record. Leave it empty unless you run your own reverse proxy. |

### Kept only so old configurations still load

| Option          | Type | Default | What it does                                                                                                                                                                     |
| --------------- | ---- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `allow_all_ips` | bool | `false` | Grants nothing any more. It is kept so old configurations still load. When `true`, the app logs an error at startup telling you to set `save_token` instead. See **LAN access**. |

## If it isn't working

The app and the integration are two things that must agree, so the symptom usually names which one to look at. The integration's [troubleshooting page](https://matthewhobbs.github.io/Homeassistant-polygonal-zones/troubleshooting/) covers its side in more detail.

| What you see                                                     | What it means                                                                         | What to do                                                                                                          |
| ---------------------------------------------------------------- | ------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| Zones are saved, but the integration has no zones                | The integration was refused, or is pointed at something else                          | Check the app log: a refusal is a `403`. See [LAN access](#lan-access-save_token) for which token must match where. |
| Every tracker stays `away` while the car or phone sits in a zone | The integration has no zones loaded, or the tracker is not one of the chosen entities | Confirm zones reach the integration first; then **⋮ → Reconfigure** and re-tick the entities.                       |
| Many entities called "Polygonal Zones", numbered `_1`, `_2` …    | An old integration released one sensor per zone and left them behind                  | Update the integration; if the strays remain, delete them by hand once.                                             |
| The app's page shows "Couldn't load zones"                       | `zones.json` is not valid JSON. The file is served as-is, so nothing reports it       | Restore the file from a backup: see [Failure modes and recovery](#availability-and-failure-modes).                  |
| The sidebar entry or **Open Web UI** is missing                  | The app is not running or not started                                                 | Start it under **Settings → Apps**; the log says why it stopped.                                                    |

## Reference

Everything below is detail: ports, tokens, backups, failure modes, and the editor itself.

### Network ports

The web interface listens on port 8000 inside the container. Use the editor through Home Assistant: the **Open Web UI** button or the sidebar entry. That route (ingress) needs no setup.

Direct access on `http://<app-LAN-IP>:8000/` is off by default (`8000/tcp: null` in the app manifest). To turn it on, open **Settings → Apps → Polygonal Zones → Network**, assign a host port and restart the app. You need this for `curl` backups, for a reverse proxy, and for an integration older than 1.16.0 that fetches the zones over your network.

Home Assistant does not need the host port. Core reaches port 8000 in the container over the app network, which is the address discovery hands over. Every request that does not come through ingress needs `save_token`: see **LAN access**.

**The LAN port is plain HTTP.** The app does not terminate TLS, so the token, your zones and tracker positions cross your network unencrypted. Use it only on a network you trust, or put a TLS-terminating reverse proxy in front of it (see **Pointing the integration at the app**).

### LAN access (`save_token`)

Only requests that arrive through Home Assistant's ingress (TCP peer `172.30.32.2`) are served without a token. Every other request needs `X-Save-Token: <value>` matching `save_token`, whatever it asks for: `GET /zones.json`, `GET /trackers.json`, `GET /trackers/here_since`, `POST /save_zones`, `/config.json`, and the editor's own page and scripts. When `save_token` is empty, the app generates one for Home Assistant's discovery message, and nothing outside Home Assistant can reach it. `/healthz` is the one exception: it stays open so health checks work.

**`allow_all_ips` no longer grants anything.** Up to 0.5.7 it let any client on your network read and overwrite zones without a token. When it is on, the app logs an error at startup telling you to set `save_token` instead.

**Use the editor through Home Assistant, not the LAN port.** A browser cannot add a header to a page it navigates to, so opening `http://<ha-host>:8000/` directly gets `403`. Use the sidebar or **Open Web UI**.

**Ingress is for the editor, not for the integration.** Ingress is Home Assistant's browser-session proxy and needs a logged-in user, so a background job like the integration cannot use it. The integration fetches the zones over HTTP, so it needs the token like any other client. That holds on the same machine: Core reaches the app's internal address, but the request does not come from the ingress address and is refused without a token.

Every refusal looks the same from outside: `403` with `{"error": "not authorised"}`, so the reply never reveals whether a token is set. The real reason is in the app's own log. Ten failed attempts from one client within a minute earn `429` for the rest of that minute.

The companion integration sends the header itself from [v1.15.0](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones/releases/tag/v1.15.0), in one of two ways.

- **Automatic setup** (integration 1.16.0 or later): the token travels in the discovery message, so there is nothing to type.
- **Manual setup**: put the app's `save_token` in the integration's **App save token** field (`zone_source_token`).

In both cases the token goes only to a local app address, never to a public source. An older integration, or an empty field on a manual setup, gets `403` and loads no zones.

Pick a long random string. The field is masked in the UI. To rotate it, change the value and restart the app.

The token is never logged. The `Loaded options:` line at startup shows `save_token: ***` when the option is set, and `save_token: not set` when it is empty — but an empty option still gets a generated token for Home Assistant's discovery message, so `not set` does not mean no token is in force.

### Backing up and restoring zones with `curl`

Your zones live in `/data/polygonal_zones/zones.json` inside the container, but you do not need a shell to read them. Map a host port, set `save_token`, then:

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

A restore must use `Content-Type: application/json`, as above. Any other type is refused with `415`.

`GET /zones.json` returns an `ETag` header, which is a fingerprint of the file. Send it back as `If-Match` on `POST /save_zones` to refuse the write if the file changed in between. The editor does this by default. A plain `curl` post without `If-Match` wins, whatever came before.

`GET /zones.json` also honours `If-None-Match` and returns `Last-Modified`. The integration reads the file at startup and when you call `reload_zones` or `reload_all_zones`; it does not poll. A `304 Not Modified` reply means your cached copy is current. That helps those on-demand reads, and any script that asks for the file repeatedly:

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

`overlay_entities` plots live tracker positions over the map, so you can see whether a device falls inside a boundary, and by how much.

The list is empty by default. While it is empty, the app asks Home Assistant for nothing.

```yaml
overlay_entities:
  - device_tracker.my_car
  - device_tracker.the_van
```

Each device appears as a marker with a dashed ring showing its reported accuracy. The sidebar lists the zones it is inside and how far it sits from their edges.

Click a marker for its popup:

- the position error and battery level, when the tracker reports them;
- **Here since**, which is when it arrived where it is now. Movement within twice its position error, or 20 m if that is more, is ignored.

Working out **Here since** reads up to 24 hours of that tracker's history from Home Assistant when you open the popup, and again if it moves while open. Nothing is stored.

| Shows                                    | Meaning                                                                                |
| ---------------------------------------- | -------------------------------------------------------------------------------------- |
| `Here since checking…`                   | The lookup is running.                                                                 |
| a time, or a date and time               | When it arrived. A date appears when that was not today.                               |
| `Here since over 24 h`                   | It has not moved for the whole 24 hours, so the real time is earlier.                  |
| `Here since no history`                  | Home Assistant holds no history for the entity in that window.                         |
| `Here since no positions recorded`       | History exists, but no point in it carried coordinates.                                |
| `Here since history unavailable`         | The history read failed, or Home Assistant answered with something unusable.           |
| `Here since too many lookups, try again` | Too many popups were open at once. Closing some and reopening the popup retries it.    |
| `Here since unknown`                     | None of the above: the request timed out after 10 seconds, or the reply was malformed. |

The readout measures. It does not predict. It tells you a device is "3.8 m outside Annex (within its accuracy)". It does not say which zone the integration will report, because the integration makes that call with rules that are deliberately not copied here.

Each entity is fetched from Home Assistant and cut down to six fields: entity id, friendly name, state, coordinates, accuracy and battery level. Every other attribute is thrown away, so it never reaches the browser. Coordinates are rounded to four decimal places, about 11 m.

### How often positions refresh (`tracker_refresh_seconds`)

While the editor is open it re-reads the listed entities every `tracker_refresh_seconds` seconds (default 60) and moves the markers. Set it to `0` to load positions once, when the page opens.

Values from 1 to 9 are raised to 10, because each poll is one Supervisor call per listed entity for every open tab. The largest accepted value is 3600.

```yaml
overlay_entities:
  - device_tracker.my_car
tracker_refresh_seconds: 30
```

Polling pauses while the tab is hidden and catches up when you return. If Home Assistant cannot answer for an entity, its last position stays on the map. An entity that comes back without coordinates is removed. The option does nothing while `overlay_entities` is empty.

### Before you add people

Positions and arrival times are served by `GET /trackers.json` and `GET /trackers/here_since`, under the same rules as `/zones.json`: ingress, or `save_token` from anywhere else. Anyone holding the token can read them over your network. List only what you are content to expose on those terms.

Vehicles are the intended case. A household member's phone is a decision to make deliberately, and ideally with them.

At most 25 entities are fetched. Anything that is not a valid entity id is ignored, with a warning in the log.

### The `homeassistant_api` grant

Reading those states needs Home Assistant's state API, so the manifest (`config.yaml`) sets `homeassistant_api: true`. The grant applies when the app is installed, whatever you configure. Leaving `overlay_entities` empty does not withhold it; it only means the app uses none of it.

The grant is not read-only. It gives the app general access to the Home Assistant API. What the app does with it is narrow: `GET /states/<entity_id>` for each entity you list, and one history read when you open that entity's popup, again if it moves while open. With the list empty it does nothing.

## Integration with the Polygonal Zones HA integration

The companion [Polygonal Zones integration](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones) reads `/zones.json` from this app. It fetches the zones at startup, and again when you save here or call `reload_zones` or `reload_all_zones`. It never reaches the app through ingress, even on the same machine.

With automatic setup the integration uses the app's internal address and the token from discovery. With manual setup you map a host port and share `save_token`, as described in **LAN access**.

### Availability and failure modes

The app serves `/zones.json` from local disk. What happens when that file becomes unreadable at runtime (disk full, ownership drift after a Supervisor remount, filesystem damage) depends on who notices first.

- **The app** returns `503 Service Unavailable` with `{"error":"zones file unreadable"}` and logs the error with a traceback. The Docker `HEALTHCHECK` also fails, because `/healthz` checks that the file is readable, not merely that the process runs, so Docker marks the container unhealthy.
- **The Supervisor** restarts the app when the health check (`/healthz`, every 30 seconds) fails three times in a row, or when the web service stops. It only does this while **Watchdog** is on in the app's **Info** tab, so turn it on.
- **The integration** sees the `503` the next time it reads zones, at startup or on a reload. By default it keeps its last good zones, so automations carry on with the definitions it last fetched. If the app is gone entirely (process dead, port unreachable), the integration reports the entities as unavailable, and automations that guard on `zone.* != 'unavailable'` stop until the app returns.

A file that is readable but not valid JSON is a different case. The app never parses `zones.json` when serving it, so it hands the bytes over and the health check stays green. The editor then shows "Couldn't load zones".

**Recovery:**

1. Open **Settings → Apps → Polygonal Zones → Log**. An unreadable file shows as `Failed to read /data/polygonal_zones/zones.json`. A corrupt file leaves no line in the app log; the editor's "Couldn't load zones" is the sign.
2. Common causes: ownership drift on `/data`, usually fixed by a restart because `cont-init.d/00-fix-perms` re-chowns on boot; a full disk; or a damaged file after an unclean shutdown.
3. If the file is corrupt, restore `/data/polygonal_zones/zones.json` from a Home Assistant backup taken before the failure (**Settings → System → Backups**), or from your own `curl` copy. Until then the integration keeps its last good zones.

### Pointing the integration at the app

#### Recommended: automatic setup (integration v1.16.0 or later)

The app announces itself to Home Assistant at every start. Open **Settings → Devices & services** and **Polygonal Zones** appears as a discovered integration. Confirm it once and setup is done. There is no URL to type, no host port to map and no token to copy, because the app generates one that only Home Assistant sees.

Then choose your devices: open the entry's **⋮ → Reconfigure**, pick the `device_tracker` entities and confirm the tracking notice.

From then on, every save asks Home Assistant to re-read the zones, so an edit is live without a manual reload. The line under **Save** says what happened. With an older integration it says the reload needs v1.16.0 or later, and the `reload_zones` action still works.

#### The LAN URL path in detail

The steps are in [If the app is not discovered](#if-the-app-is-not-discovered-manual-setup). This is what each part is doing.

From integration [v1.12.0](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones/releases/tag/v1.12.0) onward, the integration has an advanced option called `allow_private_urls`. Turn it on and point `zone_urls` at the app's LAN address.

- In this app: map a host port and set `save_token: <value>`.
- In the integration: turn on `allow_private_urls` in the advanced section, and set `zone_urls: http://<ha-host-lan-ip>:8000/zones.json`. Use the IP address, not a name such as `homeassistant`, because the integration does not send the token to a name, so the app answers `403`.
- Integration v1.15.0 or later must also hold the same value in **App save token**, or it gets `403`.

The integration refuses addresses on private networks as a guard against server-side request forgery: a zone address reaching services inside your network. Loopback, link-local (cloud metadata), multicast and reserved ranges stay blocked. `allow_private_urls` unlocks only the usual home ranges, `192.168.x.x`, `10.x.x.x` and `172.16.x.x` to `172.31.x.x`.

The request is plain HTTP, so the token crosses your network unencrypted. See **Network ports**.

#### Alternative: a private reverse proxy with TLS on a public name

If you would rather not turn on `allow_private_urls`, or you run integration v1.11.x or earlier, put a reverse proxy you control in front of the app: nginx, Caddy, Traefik or Home Assistant's NGINX Proxy Manager app. Give it TLS under a name that resolves publicly, such as `zones.yourdomain.tld`, and point the integration at `https://zones.yourdomain.tld/zones.json`.

The name resolves publicly, so the integration's guard lets it through, while the proxy listens only on your network and no zone data leaves it. The integration never sends its token to a public name, so the proxy must add it. That token also unlocks saving zones and reading tracker positions, so forward only `GET /zones.json`. For nginx:

```
location = /zones.json {
    limit_except GET { deny all; }
    proxy_set_header X-Save-Token <value>;
    proxy_pass http://<ha-host>:8000;
}
```

Keep the proxy off the public internet. Anyone who reaches it can read your zones.

#### Last resort: a public CDN mirror (privacy warning)

Hosting `zones.json` on a public server (GitHub Pages, S3, Cloudflare Pages) is sometimes suggested. Do not do it without understanding the cost. Your zone outlines give away the exact shape and position of your home, workplace, school run and every other place you drew.

- The file is world-readable. Any visitor, crawler or search engine can fetch it.
- Public CDNs cache and copy content across their networks. Once served, you cannot assume you can pull it back.
- Major providers (GitHub, AWS, Cloudflare) fall under the US CLOUD Act, so the data can be disclosed to US authorities whatever country you live in.
- Search engines index content on common hosts. Your home coordinates become findable by name or address.
- Deleting the file later does not remove archived copies, such as the Wayback Machine and third-party scrapers.

If you must do it (no control over DNS, no reverse proxy, and an integration older than v1.12.0), use a private or unlisted bucket with a strong signed URL, rotate the URL regularly, and treat a leaked URL as leaked geometry.

## Usage

### Opening the editor

- **Info page**: choose **Open Web UI** on the app's page.
- **Sidebar**: turn on **Show in sidebar** in the app configuration for a permanent shortcut.

### Saving changes

Nothing is written until you press **Save** at the bottom of the sidebar.

An amber dot appears on **Save** while there are changes not yet written. It clears when the save succeeds.

A refused save shows the server's reason under the button.

#### If a save conflicts or times out

A conflict means another session saved first. Further saves stay refused until you reload, so the other session's zones are not overwritten.

After a conflict or a timeout, **Download unsaved zones** saves what is on screen as a file. Then either:

- reload the page and apply your changes again; or
- download, reload, then use **Replace zones from file** to put the downloaded zones back. This replaces every zone with the file's zones and discards the other session's changes.

### The zones file

Zones are stored as a GeoJSON `FeatureCollection` at `http(s)://[HOST]:[PORT]/zones.json` (port 8000 inside the container), and on disk at `/data/polygonal_zones/zones.json`. The companion integration reads this file. See **Backing up and restoring zones with curl** for backups, and **Privacy and data** for what Home Assistant backups keep.

Each zone has a stable `properties.id`, which the app fills in when it is missing. A position is `[longitude, latitude]`. A third number (altitude) is accepted and dropped on save. Four or more values are refused with `422`.

A save is refused unless every feature is a `Polygon` or `MultiPolygon` with closed rings and at most 1000 vertices, and names and ids are unique. The whole collection is capped at 500 zones and 10,000 vertices — the same numbers the integration enforces, so a file this app accepts is one the integration can load — and the request body at 512 KB.

### Features

#### Viewing zones

- Every zone is listed in the sidebar and drawn on the map.
- While the list loads it shows "Loading zones…". On a new install with no zones it shows "No zones yet. Draw one on the map to get started."
- If loading fails, the list shows "Couldn't load zones…" and the **Replace zones from file** button. Check the app log.

#### Map style

- Use the **Map style** dropdown at the top of the sidebar: **Street map** (OpenStreetMap), **Dark** (CARTO) or **Satellite** (Esri World Imagery).
- **Auto** follows the `theme` option when it is `light` or `dark`. Otherwise it follows your operating system or browser setting, picking Street map in light and Dark in dark.
- Your choice is kept in the browser.

#### Adding zones

The draw controls are at the top left of the map.

- Choose the polygon button, then click the map to place points. Click the first point again to finish.
- Or choose the rectangle button, then click two opposite corners. It is saved as a plain polygon.

#### Editing zones

- Choose the edit button next to a zone's name in the sidebar.
- Drag the points to change the shape. You can rename the zone in the sidebar too.
- Press **Done** next to the zone's name. That only confirms the change in the editor; press the sidebar **Save** to write it to disk.

#### Deleting zones

- Choose the trash button in the map toolbar to enter removal mode.
- Click a zone on the map to remove it.
- Press the sidebar **Save** to write the change to disk.

## Privacy and data

**Zones and settings are in every backup.** Your zones are at `/data/polygonal_zones/zones.json`. Everything in `/data` is in every Home Assistant backup that includes this app, and so is the app's `options.json`, which holds `save_token` and your `overlay_entities` list.

Deleting a zone in the editor does not remove it from earlier backups. If you are removing zones for privacy (after a move, after drawing over a sensitive area, or after a deletion request), remove old backups too, under **Settings → System → Backups → ⋮ → Remove**, and any copies you keep outside Home Assistant.

**The LAN port is not encrypted.** If you map a host port, the token, your zones and tracker positions travel over plain HTTP. See **Network ports**.

**Map tiles come from a third party.** Your browser fetches the map background from the provider of the style in use: OpenStreetMap, CARTO or Esri. Auto picks OpenStreetMap or CARTO by theme; Esri is used only if you choose Satellite. That provider sees your IP address, the area you are viewing (from the tiles you request) and your Home Assistant address as the referring site, such as a `*.ui.nabu.casa` hostname.

**Your browser remembers the last map view.** The editor stores where you last panned and zoomed in browser storage, under `pz:viewport`, so it opens there next time. Map style and sidebar choice are kept the same way (`pz:basemap`, `pz:sidebar`). None of it is sent to the app. Clearing site data for your Home Assistant address removes it.

## Is this the right tool? What the accuracy allows

Zones are compared against positions reported by phones, cars and trackers. The accuracy of the answer comes from those devices, not from how carefully you draw. A boundary placed to the centimetre is still only as good as the position being tested against it.

Real devices, as a sense of scale:

| Source                                | Typical reported accuracy              |
| ------------------------------------- | -------------------------------------- |
| Phone outdoors, good sky view         | 5–15 m                                 |
| Phone indoors                         | 15–65 m                                |
| Vehicle tracker with a proper antenna | 3–10 m                                 |
| Some vehicle APIs                     | reports `0`, meaning _no figure given_ |

A position is a circle, not a point. A device standing still drifts several metres between reports. The integration also widens each zone by the reported accuracy before testing, so a device can match a zone it is several metres outside.

**Good for:**

- property boundaries, paddocks, yards and car parks
- "is the car home or not", "is the van on site"
- any zone comfortably larger than the accuracy of the devices you test with. As a rule of thumb, 20 m across or more.

**Bad for:**

- rooms inside a building. A 4 m by 5 m room is smaller than the error circle of a phone standing in it. Neighbouring rooms will swap at random, and a device outside the building can match one: a car on the driveway has matched an indoor kitchen zone, because a 5 m ring reached it.
- anything needing pinpoint accuracy, or "which side of this line"
- telling apart places a few metres apart

**For room-level presence, use another tool.** The limit is the positioning, not the polygon. Wi-Fi access-point association, Bluetooth room sensors such as ESPresense, or mmWave occupancy sensors solve that problem. This app is for outdoor zones at the scale of a property.

The tracker overlay (described below) draws each device's accuracy ring over your zones, so you can judge whether a boundary means anything at the size you drew it.
