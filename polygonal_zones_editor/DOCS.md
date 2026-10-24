# Polygonal Zones Editor

Home Assistant now calls these **apps**. Some versions of Home Assistant call them **add-ons**; it is the same thing, and the instructions below say **app**.

Draw zones — areas on a map — for your home, a yard, a car park, and have Home Assistant know when a phone or a car is inside one. Then an automation can do something when it arrives: open the gate, turn on the lights, tell you the car is home.

This is one of two pieces you need:

- **this app**, which draws the zones and remembers them;
- the **Polygonal Zones integration**, which watches your phones and cars and works out which zone each one is in.

Install both from their GitHub pages, and they find each other. You will not need an address, a port or a password.

## Before you start

- Home Assistant **2026.8.1** or newer.
- This app, and the [Polygonal Zones integration](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones).
- Something that reports where it is: a phone running the Home Assistant app, a car with a data connection, or a GPS tracker.

**On a 32-bit system** (a very old Raspberry Pi, or a 32-bit operating system), version 0.2.25 was the last one published. Move to 64-bit Home Assistant to keep receiving updates.

## Install the app

1. In Home Assistant, open **Settings → Apps**, choose **Install app**, then the **⋮** menu in the top right.
2. Choose **Repositories**, paste
   `https://github.com/MatthewHobbs/Homeassistant-polygonal-zones-addon` and add it.
3. Find **Polygonal Zones** in the store, choose **Install**, then **Start**.
4. Turn on **Watchdog** on the app's page. It restarts the app if it stops.

## Set it up

1. Install the [Polygonal Zones integration](https://github.com/MatthewHobbs/Homeassistant-polygonal-zones). If it does not appear in the list of integrations, restart Home Assistant.
2. Open **Settings → Devices & services** and wait a minute. A **Polygonal Zones** card appears under **Discovered**.
3. Choose **Configure** on it (some versions say **Confirm**).
4. Tick the phones, cars or trackers you want watched, and accept the notice about tracking.
5. Choose **Finish**.

That is the whole setup. Nothing to copy, nothing to type.

## Check that it works

1. Open the app: choose **Open Web UI** on its page, or the **Polygonal Zones** entry in the sidebar if you turned that on.
2. Choose the polygon button at the top left of the map, then click the map to place corners. Click the first corner again to close the shape.
3. Give it a name you will recognise — `Home`, `Work`, `Nan's` — and choose **Save** at the bottom of the panel.
4. Now find the device you ticked: **Settings → Devices & services → Devices**. Its position line should read `Home` while it is inside your shape, and `away` when it leaves.

If it already says `away` while it is standing inside the shape, look below.

## If it does not work

| What you see                                          | What to do                                                                                                                                                                        |
| ----------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| No **Polygonal Zones** card to set up                 | Check the app is installed **and started**, and the integration is installed. Restart Home Assistant. If it still does not appear, see [setting up by hand](#setting-up-by-hand). |
| Set up, but the device always says `away`             | Check you ticked that device in the setup. Check the shape was saved: it should be in the list in the app. A device that has not moved or reported for hours may take a while.    |
| A device says `away` although it is clearly inside    | The shape may be too small to be reliable, or the phone may be reporting a rough position. See [is this the right tool](#is-this-the-right-tool).                                 |
| Saving says something went wrong                      | The app and the integration disagree about the app's password. That only happens after setting up by hand; repeat step 2 of [setting up by hand](#setting-up-by-hand).            |
| The editor says it could not load your zones          | The file holding your zones is damaged. Restore it from a Home Assistant backup — see [if the zones file is damaged](#if-the-zones-file-is-damaged).                              |
| Many extra devices called Polygonal Zones 1, 2, 3, 4… | An old version of the integration left them behind. Update the integration, then delete the leftovers once by hand.                                                               |
| The editor does not appear in the sidebar             | Turn on **Show in sidebar** in the app's configuration, or use **Open Web UI** on the app's page.                                                                                 |

The integration's own [troubleshooting page](https://matthewhobbs.github.io/Homeassistant-polygonal-zones/troubleshooting/) covers the tracking side in more detail.

## Drawing and editing zones

- **Draw**: the polygon button at the top left, then click each corner; click the first corner again to finish. The rectangle button takes two opposite corners instead.
- **Rename**: click the name in the list on the left and type a new one.
- **Reshape**: choose the edit button next to the name, drag the corners, then choose **Done**.
- **Delete**: choose the bin button in the map toolbar, then click the shape.
- **Save**: nothing is kept until you choose **Save** at the bottom of the panel. An amber dot shows there are changes waiting.
- **Start again**: if you make a mess, **Replace zones from file** loads zones you saved earlier.

Changes take effect as soon as you save: the integration is told to re-read them, so an automation can use the new shape immediately.

## Changing how it looks

Two settings, both on the app's **Configuration** page. Everything else there can be left alone.

| Setting       | What it does                                                                                       |
| ------------- | -------------------------------------------------------------------------------------------------- |
| `zone_colour` | The colour of the shapes on the map. A colour name such as `purple`, or a value such as `#800080`. |
| `theme`       | `auto` follows your device's light or dark setting. Choose `light` or `dark` to fix it either way. |

## Showing where your devices are (optional)

The map can show your phones and cars live, with a circle showing how precise each position is. It is off until you say which devices to show.

1. On the app's **Configuration** page, add device names to `overlay_entities`, for example `device_tracker.my_car`.
2. Choose **Save**, then reopen the app. Each device appears with a dashed circle. Click one to see how far it is from the nearest edge, and how long it has been there.

`tracker_refresh_seconds` sets how often those positions update while the app is open: `60` by default, `0` to load them once.

Reading positions needs permission to read Home Assistant's device states. The app asks for that permission for these positions and the arrival time, and uses it for nothing else. Only list devices whose position you are comfortable having on this map: anyone who can open the app can see them.

## Your data

- **Where your zones live.** Inside Home Assistant, on the same device, kept with the app's own data. They are in every Home Assistant backup that includes this app.
- **Deleting a zone does not delete it from old backups.** If you are removing zones for privacy, remove the old backups too: **Settings → System → Backups**.
- **The map background** comes from OpenStreetMap, CARTO or Esri, whichever you pick. Those providers see which part of the map you are looking at, and your Home Assistant address as the page you came from.
- **Your browser remembers the last map view** for each device you use, so it opens where you left it. Clearing your browser's data for Home Assistant forgets it.
- **No one else is told anything.** The app does not send your zones anywhere.

## Is this the right tool?

How accurate a zone is depends on the device being watched, not on how carefully you draw. A position is a circle, not a point: a phone standing still drifts several metres between reports, and the integration widens each zone by that error before deciding.

| Where the device is          | Usual accuracy |
| ---------------------------- | -------------- |
| Phone outdoors, clear sky    | 5–15 m         |
| Phone indoors                | 15–65 m        |
| Vehicle tracker with antenna | 3–10 m         |

**A good fit:** a garden, a paddock, a yard, a car park — any zone about 20 m across or more, where the question is simply "is it here or not".

**A poor fit:** rooms inside a building. A 4 m by 5 m room is smaller than the error of a phone standing in it, so neighbouring rooms swap at random and a car on the drive can count as being in the kitchen. For room-level presence you want Wi-Fi access points, Bluetooth room sensors or mmWave sensors instead. Careful drawing cannot fix an imprecise position.

## Setting up by hand

Only needed when the automatic setup cannot work: an app older than 0.7.0, an integration older than 1.16.0, or an app running somewhere Home Assistant cannot discover — another machine, for example.

You are joining the two pieces yourself this time, and three things have to agree: a port, a password and an address.

1. **Give the app a network port.** **Settings → Apps → Polygonal Zones → Network**, set host port `8000`, and restart the app.
2. **Choose the app's password.** On the **Configuration** page, set `save_token` to a long random value — treat it as a password. Restart the app.
3. **Add the integration.** **Settings → Devices & services → Add integration → Polygonal Zones**.
4. **Tell it where the app is.** Address: `http://<home-assistant-address>:8000/zones.json`, where the address is the number that looks like `192.168.1.20`. Use the number, not a name such as `homeassistant`: the integration only sends the password to a number.
5. **Allow your own network, and paste the password.** Turn on **Advanced mode** in your user profile if you cannot see these options. Turn on **Allow private-network URLs (LAN)**, and put the app's `save_token` in **App save token**.
6. **Tick your devices** and finish.

An integration older than 1.15.0 cannot send a password at all. It needs the app to be open to the network, which this app stopped allowing in 0.5.8, so put a web server in front of the app instead: see [advanced connections](#advanced-connections).

The password travels over your home network without encryption in this setup, so use it only on a network you trust.

## If the zones file is damaged

The app keeps your zones in one file. If that file becomes unreadable — a full disk, a damaged disk, a bad shutdown — the app says so in its log, the editor says it could not load your zones, and Home Assistant's watchdog restarts the app.

1. Open **Settings → Apps → Polygonal Zones → Log**. Look for a line about the zones file.
2. Restarting often fixes it, because the app repairs file ownership on start.
3. If it persists, restore the file from a Home Assistant backup taken before the problem, or from a copy you saved with the backup method below.

If the file is readable but not valid, the app will serve it and the editor will refuse to open it. In that case the log stays quiet; restore from a backup.

## Advanced connections

These are for people who want a fixed web address, a backup they can run from a terminal, or a connection without the automatic discovery. Skip this section unless you need it.

### Backing up and restoring with curl

```sh
# Save your zones to a file
curl -H 'X-Save-Token: <your password>' \
  http://<home-assistant-address>:8000/zones.json > zones-backup.json

# Put them back
curl -X POST -H 'Content-Type: application/json' \
  -H 'X-Save-Token: <your password>' \
  --data-binary @zones-backup.json \
  http://<home-assistant-address>:8000/save_zones
```

A restore must be sent as `application/json`, as above. Add `If-Match` with the value of the `ETag` header if you want the restore refused when someone saved in the meantime; the editor does this for you.

### Watching it behind a web address of your own

If the app must be reachable at a name rather than a number, put a web server of your own in front of it — nginx, Caddy, Traefik, or the NGINX Proxy Manager app — with a certificate, on a name that resolves publicly. Point the integration at `https://your-name/zones.json`.

The integration never sends the password to a public name, so the web server has to add it. That password also allows saving zones and reading device positions, so allow only `GET /zones.json`:

```
location = /zones.json {
    limit_except GET { deny all; }
    proxy_set_header X-Save-Token <your password>;
    proxy_pass http://<home-assistant-address>:8000;
}
```

Keep that address off the public internet. Anyone who reaches it can read your zones.

### Putting the zones on a public web address

Do not, without understanding the cost. Your zones trace the exact shape and position of your home, workplace and everywhere else you drew:

- the file is readable by anyone who has the address, including search engines and archives;
- large providers copy and cache what they serve, and copies cannot be withdrawn;
- deleting the file later does not remove archived copies.

If you have no other option, use a private store with an unguessable, rotating address, and treat a leaked address as leaked information about your home.

### Settings reference

Every setting, for completeness. The four groups are the only ones most people touch.

| Setting                   | Values                                          | Default  | What it does                                                                                                                     |
| ------------------------- | ----------------------------------------------- | -------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `zone_colour`             | colour name, or `#800080`                       | `purple` | Colour of the shapes. Shortened colours such as `#12345` are not valid.                                                          |
| `theme`                   | `auto`, `light`, `dark`                         | `auto`   | Light or dark editor, and the map style when the map is set to Auto.                                                             |
| `log_level`               | `debug`, `info`, `warning`, `error`, `critical` | `info`   | How much the app writes to its log. `debug` when asking for help.                                                                |
| `save_token`              | any text                                        | empty    | The app's password, needed by anything that is not Home Assistant itself. Empty with automatic setup.                            |
| `overlay_entities`        | device names                                    | empty    | Devices to draw on the map. Up to 25.                                                                                            |
| `tracker_refresh_seconds` | `0` to `3600`                                   | `60`     | How often those positions update. `0` loads them once. Values 1–9 are raised to 10.                                              |
| `trusted_proxies`         | numbers, comma separated                        | empty    | Only with your own web server in front: which proxy's forwarded address to believe. List the proxy's own address, never a range. |
| `allow_all_ips`           | true / false                                    | `false`  | Does nothing now. Kept so older configurations still load; use `save_token` instead.                                             |

### Limits

A save is refused unless each zone is a simple shape with closed edges and at most 1000 corners, and names and identifiers are unique. The whole set is capped at 500 zones and 10,000 corners — the same numbers the integration applies — and one save at 512 KB.

## Privacy and data

Your zones are stored on your Home Assistant device and are included in Home Assistant backups that include this app. They are never sent anywhere else. The map background comes from a third party, as described under [your data](#your-data). Positions of the devices you choose to show are read from Home Assistant and are not stored by this app.
