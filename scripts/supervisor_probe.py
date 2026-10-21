"""Probe the add-on through Home Assistant Core's ingress, as a user reaches it.

Run by scripts/supervisor-pilot.sh inside the Playwright image, sharing the
devcontainer's network namespace, so Core is on 127.0.0.1. Logs in to
Core (onboarding the first user if needed), asks Core for an ingress session
over the websocket API exactly as the frontend does, then runs build.yml's
smoke probes and draw_and_save (scripts/draw_and_save.py, shared with
build.yml's own smoke) against the ingress URL.

tracker_refresh() proves the tracker overlay end to end:
a real Core service call moves `zone.home`, and the check watches the editor's
own polling — not a stub — pick the new position up without a reload. The
container-verify stub in the addon PR covers every edge case (outages,
concurrency, hidden tabs); this only has to show the real stack agrees.
"""

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
import json
import urllib.error
import urllib.parse
import urllib.request

from draw_and_save import SMOKE_ZONES, draw_and_save, fail, watch_errors
from playwright.async_api import async_playwright

# A name that maps to the same Core but is not loopback, so the page is not a
# secure context: how HA is usually opened on a LAN (#46).
INSECURE_HOST = "ha.test"
USERNAME = "pilot"
PASSWORD = "pilot-not-a-secret"


def http(method, url, data=None, form=False, token=None):
    headers = {}
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def access_token(base):
    """Onboard the first user, or log in if a previous run already did."""
    client_id = f"{base}/"
    try:
        code = http(
            "POST",
            f"{base}/api/onboarding/users",
            {
                "client_id": client_id,
                "name": "Pilot",
                "username": USERNAME,
                "password": PASSWORD,
                "language": "en",
            },
        )["auth_code"]
    except urllib.error.HTTPError as err:
        if err.code != 403:  # 403: the user step is already done
            raise
        flow = http(
            "POST",
            f"{base}/auth/login_flow",
            {
                "client_id": client_id,
                "handler": ["homeassistant", None],
                "redirect_uri": client_id,
            },
        )
        code = http(
            "POST",
            f"{base}/auth/login_flow/{flow['flow_id']}",
            {"client_id": client_id, "username": USERNAME, "password": PASSWORD},
        )["result"]
    return http(
        "POST",
        f"{base}/auth/token",
        {"grant_type": "authorization_code", "code": code, "client_id": client_id},
        form=True,
    )["access_token"]


# The frontend's own route to an ingress session: supervisor/api over the
# websocket, which Core forwards to the Supervisor on the user's behalf.
INGRESS_SESSION_JS = """
([url, token]) => new Promise((resolve, reject) => {
  const ws = new WebSocket(url);
  const timer = setTimeout(() => reject(new Error("websocket timeout")), 30000);
  ws.onerror = () => reject(new Error("websocket error"));
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "auth_required") {
      ws.send(JSON.stringify({type: "auth", access_token: token}));
    } else if (msg.type === "auth_ok") {
      ws.send(JSON.stringify({id: 1, type: "supervisor/api",
                              endpoint: "/ingress/session", method: "post"}));
    } else if (msg.type === "auth_invalid") {
      reject(new Error("auth_invalid: " + msg.message));
    } else if (msg.id === 1) {
      clearTimeout(timer);
      ws.close();
      msg.success ? resolve(msg.result.session)
                  : reject(new Error(JSON.stringify(msg.error)));
    }
  };
})
"""


async def smoke(request, url, expect_colour):
    """build.yml's endpoint probes, through ingress instead of a direct port."""
    r = await request.get(url + "healthz")
    if r.status != 200 or "ok" not in await r.text():
        fail(f"/healthz through ingress returned {r.status}: {(await r.text())[:200]!r}")
    print("OK /healthz through ingress")

    r = await request.get(url + "zones.json")
    d = await r.json() if r.ok else None
    if not (d and d.get("type") == "FeatureCollection" and isinstance(d.get("features"), list)):
        fail(f"/zones.json through ingress: {r.status} {d!r}")
    print("OK /zones.json shape")

    r = await request.post(
        url + "save_zones",
        data=json.dumps(SMOKE_ZONES),
        headers={"Content-Type": "application/json"},
    )
    if r.status != 200 or '"status":"ok"' not in (await r.text()).replace(" ", ""):
        fail(f"/save_zones through ingress returned {r.status}: {(await r.text())[:200]!r}")
    print("OK /save_zones round-trip")

    # The option was set through the Supervisor API; only the app can echo it.
    r = await request.get(url + "config.json")
    colour = (await r.json()).get("zone_colour") if r.ok else None
    if colour != expect_colour:
        fail(
            f"/config.json zone_colour {colour!r}, expected {expect_colour!r} set via the Supervisor"
        )
    print(f"OK /config.json reflects the Supervisor-set option ({colour})")
    await caching(request, url)


async def caching(request, url):
    """Ingress must pass the revalidation headers through, or a browser keeps
    serving a stale editor after an upgrade."""
    r = await request.get(url + "js/map.js")
    etag = r.headers.get("etag")
    cache_control = [d.strip() for d in r.headers.get("cache-control", "").split(",")]
    if r.status != 200 or not etag or "no-cache" not in cache_control:
        fail(f"js/map.js through ingress: {r.status} {r.headers}")
    r = await request.get(url + "js/map.js", headers={"If-None-Match": etag})
    if r.status != 304:
        fail(f"js/map.js with If-None-Match {etag} through ingress: {r.status} {r.headers}")
    print("OK js/map.js carries no-cache and an ETag, and revalidates to 304")

    r = await request.get(url + "zones.json")
    if r.status != 200 or not r.headers.get("etag"):
        fail(f"/zones.json through ingress: {r.status} {r.headers}")
    print("OK /zones.json carries an ETag")


async def recorded(base, token, entity_id, count, timeout_s=30):
    """Wait until Core's history holds at least `count` states for the entity."""
    # Core ends a period a day after its start, so start just before the writes.
    start = urllib.parse.quote((datetime.now(UTC) - timedelta(minutes=10)).isoformat())
    url = f"{base}/api/history/period/{start}?filter_entity_id={entity_id}"
    deadline = asyncio.get_event_loop().time() + timeout_s
    states = []
    while asyncio.get_event_loop().time() < deadline:
        reply = http("GET", url, token=token)
        states = reply[0] if reply else []
        if len(states) >= count:
            return
        await asyncio.sleep(2)
    fail(f"Core's history held {len(states)} states for {entity_id}, wanted {count}")


def _instant(stamp):
    return datetime.fromisoformat(stamp).timestamp()


async def here_since(request, url, entity_id, want, timeout_s=30):
    """Poll the click lookup until it reports `want`: the recorder writes
    history on an interval, so the first read can predate a write."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    body = None
    while asyncio.get_event_loop().time() < deadline:
        r = await request.get(f"{url}trackers/here_since?entity_id={entity_id}")
        body = await r.json() if r.status == 200 else {"status": r.status}
        # As times: the history and state APIs may format the same instant differently.
        since = body.get("since")
        if since and abs(_instant(since) - _instant(want)) < 1:
            return
        await asyncio.sleep(2)
    fail(f"here since for {entity_id}: wanted {want}, last got {body}")


async def here_since_live(request, url, base, token, entity_id):
    """The click lookup reads Core's real history through the Supervisor: a
    tracker set twice at one spot dates from the first fix, and a move resets it."""
    path = f"{base}/api/states/{entity_id}"
    spot = {"latitude": 52.0, "longitude": 4.0, "gps_accuracy": 10}
    first = http("POST", path, {"state": "not_home", "attributes": spot}, token=token)
    await asyncio.sleep(2)
    http(
        "POST",
        path,
        {"state": "not_home", "attributes": {**spot, "battery_level": 50}},
        token=token,
    )
    # Both same-spot states must be in Core's history, or a match on the first
    # proves nothing about the walk crossing the second.
    await recorded(base, token, entity_id, 2)
    await here_since(request, url, entity_id, first["last_updated"])
    print(f"OK here since dates {entity_id} from its first fix at that spot")
    moved = http(
        "POST",
        path,
        {"state": "not_home", "attributes": {**spot, "latitude": 52.01}},
        token=token,
    )
    await here_since(request, url, entity_id, moved["last_updated"])
    print("OK here since resets when the tracker moves about 1 km")


async def tracker_position(page, entity_id):
    """The editor's own view of an overlay entity, straight from its live
    `pz_trackers` array — never re-fetch /trackers.json ourselves, or a poll
    the editor never made would pass the check. `pz_trackers` is declared with
    `let` at the top of a classic (non-module) script, so it is a lexical
    binding of the page's global scope, not a `window` property; reading
    `window.pz_trackers` is always undefined."""
    return await page.evaluate(
        "(id) => { const t = (typeof pz_trackers !== 'undefined' ? pz_trackers : [])"
        ".find(t => t.entity_id === id);"
        " return t ? [t.latitude, t.longitude] : null; }",
        entity_id,
    )


async def wait_for_tracker(page, entity_id, timeout_s):
    """The overlay's first read is a real fetch through the Supervisor to Core,
    not the instant thing a stub makes it look like; give it room to land."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    position = None
    while asyncio.get_event_loop().time() < deadline:
        position = await tracker_position(page, entity_id)
        if position is not None:
            return position
        await asyncio.sleep(1)
    return position


async def tracker_refresh(page, base, token, entity_id, refresh_seconds):
    """Move `zone.home` through Core's own service call and confirm the
    editor's next poll — through the real Supervisor and Core, not a stub —
    picks up the new position without a reload (#47)."""
    before = await wait_for_tracker(page, entity_id, 20)
    if before is None:
        fail(f"tracker overlay never showed {entity_id} within 20s of the page loading")
    lat, lon = before
    moved = (round(lat + 0.05, 4), round(lon + 0.05, 4))
    http(
        "POST",
        f"{base}/api/services/homeassistant/set_location",
        {"latitude": moved[0], "longitude": moved[1]},
        token=token,
    )
    print(f"OK moved {entity_id} via homeassistant.set_location: {before} -> {moved}")

    # Two polls of margin: the option is validated (and 1-9 raised to 10) by
    # the add-on itself, so this waits on whatever the Supervisor actually
    # holds, not on the value this script asked for.
    deadline = asyncio.get_event_loop().time() + refresh_seconds * 2 + 20
    after = before
    while asyncio.get_event_loop().time() < deadline:
        after = await tracker_position(page, entity_id)
        if after is not None and after != before:
            break
        await asyncio.sleep(1)
    if after is None or after == before:
        fail(
            f"{entity_id} still reports {after} through the editor "
            f"{refresh_seconds * 2 + 20}s after moving it to {moved}"
        )
    print(f"OK [{entity_id}] editor picked up the moved position via its own poll: {after}")


async def open_editor(context, origin, ingress, errors, expect_secure):
    page = await context.new_page()
    watch_errors(page, errors)
    resp = await page.goto(origin + ingress, wait_until="networkidle")
    if not resp or resp.status != 200:
        fail(f"{origin}{ingress} returned {resp.status if resp else 'nothing'}", errors)
    if await page.evaluate("() => window.isSecureContext") is not expect_secure:
        fail(f"{origin} isSecureContext is not {expect_secure}; the check would prove nothing")
    body = (await page.locator("body").text_content()) or ""
    if "Failed to load zones" in body:
        fail(f"{origin}: page shows 'Failed to load zones'", errors)
    count = await page.locator("zone-entry").count()
    if count < 2:
        fail(f"{origin}: expected the 2 smoke zones to render, got {count}", errors)
    return page


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--ingress", required=True, help="ingress_url from the Supervisor")
    ap.add_argument("--expect-colour", required=True)
    ap.add_argument(
        "--tracker-entity",
        help="overlay entity to move and watch update through ingress polling; "
        "omit to skip the tracker-refresh check",
    )
    ap.add_argument("--tracker-refresh-seconds", type=int, default=10)
    ap.add_argument(
        "--history-entity",
        help="device_tracker to create and check the here-since lookup against; omit to skip",
    )
    args = ap.parse_args()
    base = args.base.rstrip("/")
    port = urllib.parse.urlsplit(base).port
    insecure = f"http://{INSECURE_HOST}" + (f":{port}" if port else "")

    token = access_token(base)
    print("OK logged in to Core")
    errors = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            args=[f"--host-resolver-rules=MAP {INSECURE_HOST} 127.0.0.1"]
        )
        context = await browser.new_context()
        blank = await context.new_page()
        ws_url = base.replace("http://", "ws://", 1) + "/api/websocket"
        session = await blank.evaluate(INGRESS_SESSION_JS, [ws_url, token])
        await blank.close()
        if not session:
            fail("Core returned no ingress session")
        await context.add_cookies(
            [
                {"name": "ingress_session", "value": session, "url": base + "/"},
                {"name": "ingress_session", "value": session, "url": insecure + "/"},
            ]
        )
        print("OK ingress session from Core")

        await smoke(context.request, base + args.ingress, args.expect_colour)
        if args.history_entity:
            await here_since_live(
                context.request, base + args.ingress, base, token, args.history_entity
            )

        secure_page = await open_editor(context, base, args.ingress, errors, True)
        await draw_and_save(secure_page, "secure origin", errors, fail)
        if args.tracker_entity:
            await tracker_refresh(
                secure_page, base, token, args.tracker_entity, args.tracker_refresh_seconds
            )
        page = await open_editor(context, insecure, args.ingress, errors, False)
        await draw_and_save(page, "non-secure origin", errors, fail)

        if errors:
            fail("JS errors while using the editor through ingress", errors)
        await browser.close()
    print("OK editor works through Core ingress on secure and non-secure origins")


asyncio.run(main())
