"""Probe the add-on through Home Assistant Core's ingress, as a user reaches it.

Run by scripts/supervisor-pilot.sh inside the Playwright image, sharing the
devcontainer's network namespace, so Core is on 127.0.0.1. Logs in to
Core (onboarding the first user if needed), asks Core for an ingress session
over the websocket API exactly as the frontend does, then runs build.yml's
smoke probes and its draw-and-save check against the ingress URL.

The draw-and-save check is duplicated from build.yml's Frontend smoke step,
which this change must not touch; fold the two together once it can.
"""

import argparse
import asyncio
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

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


def http(method, url, data=None, form=False):
    headers = {}
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
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


async def draw_and_save(page, label, errors):
    """Copied from build.yml: draw with the Geoman toolbar, save, re-read."""

    def die(msg):
        fail(f"[{label}] {msg}", errors)

    await page.evaluate("() => map.setView([50.5, 10.5], 10)")
    before = await page.evaluate(
        "() => editableLayers.getLayers().map(l => l.feature.properties.id)"
    )
    box = await page.locator("#map").bounding_box()
    points = [(0.15, 0.3), (0.35, 0.3), (0.25, 0.6)]
    points = [(box["x"] + box["width"] * fx, box["y"] + box["height"] * fy) for fx, fy in points]
    await page.click(".leaflet-pm-toolbar .leaflet-pm-icon-polygon")
    for x, y in points + points[:1]:
        await page.mouse.click(x, y)
        await page.wait_for_timeout(100)
    await page.wait_for_timeout(300)

    if [e for e in errors if e.startswith("pageerror")]:
        die("JS error while creating a zone")
    after = await page.evaluate(
        "() => editableLayers.getLayers().map(l => l.feature.properties.id)"
    )
    if len(after) != len(before) + 1:
        die(f"expected {len(before) + 1} zones after drawing one, got {len(after)}")
    new_ids = [i for i in after if i not in before]
    if len(new_ids) != 1 or not re.fullmatch(r"[0-9a-f]{32}", new_ids[0] or ""):
        die(f"new zone's properties.id missing or wrong shape: {new_ids!r}")
    new_id = new_ids[0]
    if await page.locator(f'zone-entry[zone-id="{new_id}"]').count() != 1:
        die(f"no zone-entry rendered for the new zone {new_id}")

    async with page.expect_response(
        lambda r: r.url.endswith("/save_zones") and r.request.method == "POST"
    ) as saved:
        await page.evaluate("() => save_zones()")
    status = (await saved.value).status
    if status != 200:
        die(f"draw->save round-trip returned {status} (expected 200)")
    persisted = await page.evaluate("async () => (await fetch('./zones.json').then(r => r.json()))")
    ids = [(f.get("properties") or {}).get("id") for f in persisted.get("features") or []]
    if new_id not in ids:
        die(f"new zone {new_id} not in /zones.json after save")
    print(f"OK [{label}] drew a zone, id {new_id}, saved and persisted")


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

        page = await open_editor(context, base, args.ingress, errors, True)
        await draw_and_save(page, "secure origin", errors)
        page = await open_editor(context, insecure, args.ingress, errors, False)
        await draw_and_save(page, "non-secure origin", errors)

        if errors:
            fail("JS errors while using the editor through ingress", errors)
        await browser.close()
    print("OK editor works through Core ingress on secure and non-secure origins")


asyncio.run(main())
