"""Probe the add-on through Home Assistant Core's ingress, as a user reaches it.

Run by scripts/supervisor-pilot.sh inside the Playwright image, sharing the
devcontainer's network namespace, so Core is on 127.0.0.1. Logs in to
Core (onboarding the first user if needed), asks Core for an ingress session
over the websocket API exactly as the frontend does, then runs build.yml's
smoke probes and draw_and_save (scripts/draw_and_save.py, shared with
build.yml's own smoke — ADR 0002 row 8) against the ingress URL.

tracker_refresh() proves the tracker overlay (ADR 0001, issue #47) end to end:
a real Core service call moves `zone.home`, and the check watches the editor's
own polling — not a stub — pick the new position up without a reload. The
container-verify stub in the addon PR covers every edge case (outages,
concurrency, hidden tabs); this only has to show the real stack agrees.
"""

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

from draw_and_save import draw_and_save
from playwright.async_api import async_playwright

# A name that maps to the same Core but is not loopback, so the page is not a
# secure context: how HA is usually opened on a LAN (#46).
INSECURE_HOST = "ha.test"
USERNAME = "pilot"
PASSWORD = "pilot-not-a-secret"


def fail(msg, errors=()):
    print(f"FAIL {msg}")
    for e in errors:
        print(" -", e)
    sys.exit(1)


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

    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "smoke"},
                "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
            },
            {
                "type": "Feature",
                "properties": {"name": "multi"},
                "geometry": {
                    "type": "MultiPolygon",
                    "coordinates": [
                        [[[2, 2], [3, 2], [3, 3], [2, 2]]],
                        [[[4, 4], [5, 4], [5, 5], [4, 4]]],
                    ],
                },
            },
        ],
    }
    r = await request.post(
        url + "save_zones", data=json.dumps(payload), headers={"Content-Type": "application/json"}
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
    picks up the new position without a reload (#47, ADR 0001)."""
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
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: m.type == "error" and errors.append(f"console.error: {m.text}"))
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
