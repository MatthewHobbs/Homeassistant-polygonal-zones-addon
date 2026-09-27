"""Draw a triangle with the real Geoman toolbar, save it through the app's
own save_zones(), and confirm it reached /zones.json.

Shared by build.yml's standalone smoke and scripts/supervisor_probe.py's
ingress-path check (ADR 0002 row 8) — previously duplicated between the
two, which let a fix to one (such as #46's non-secure-origin leg) land in
only one copy. The pilot's own ingress-specific setup (Core login, the
ingress session, the option check) stays in supervisor_probe.py; only this
shared mechanic moved here.
"""

import re


async def draw_and_save(page, label, errors, fail):
    """fail(msg, errors) reports the failure and must not return (it exits
    or raises), so a broken assertion here never continues past it."""

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
    # Clicking the first vertex again is what closes the shape.
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


async def draw_rectangle_and_save(page, errors, fail):
    """Rectangle-drawing regression guard (ADR 0002 row 11). drawRectangle
    was collateral-disabled in #36 and turned back on later — this
    exercises the mode neither smoke touched before, so a regression that
    hides or breaks the button, or makes Geoman emit anything other than a
    plain Polygon, fails a real run instead of only having been checked
    once by hand."""

    def die(msg):
        fail(f"[rectangle] {msg}", errors)

    button = page.locator(".leaflet-pm-toolbar .leaflet-pm-icon-rectangle")
    aria = await button.locator("xpath=..").get_attribute("aria-label")
    if not aria:
        die("rectangle toolbar button has no aria-label")

    await page.evaluate("() => map.setView([51.0, 11.0], 10)")
    before = await page.evaluate(
        "() => editableLayers.getLayers().map(l => l.feature.properties.id)"
    )
    box = await page.locator("#map").bounding_box()
    x0, y0 = box["x"] + box["width"] * 0.3, box["y"] + box["height"] * 0.3
    x1, y1 = box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5
    # Unlike the polygon tool's click-per-vertex, Geoman's rectangle mode is
    # click-move-click: no drag, and no vertex is placed until the second
    # click.
    await button.click()
    await page.wait_for_timeout(150)
    await page.mouse.move(x0, y0)
    await page.mouse.click(x0, y0)
    await page.wait_for_timeout(150)
    await page.mouse.move(x1, y1, steps=5)
    await page.wait_for_timeout(150)
    await page.mouse.click(x1, y1)
    await page.wait_for_timeout(300)

    after = await page.evaluate(
        "() => editableLayers.getLayers().map(l => l.feature.properties.id)"
    )
    if len(after) != len(before) + 1:
        die(f"expected {len(before) + 1} zones after drawing one, got {len(after)}")
    new_id = next(i for i in after if i not in before)

    geom_type = await page.evaluate(
        f"""() => editableLayers.getLayers()
            .find(l => l.feature.properties.id === "{new_id}")
            .toGeoJSON().geometry.type"""
    )
    if geom_type != "Polygon":
        die(f"rectangle saved as geometry.type {geom_type!r}, expected Polygon")

    async with page.expect_response(
        lambda r: r.url.endswith("/save_zones") and r.request.method == "POST"
    ) as saved:
        await page.evaluate("() => save_zones()")
    status = (await saved.value).status
    if status != 200:
        die(f"draw->save round-trip returned {status} (expected 200)")
    persisted = await page.evaluate("async () => (await fetch('./zones.json').then(r => r.json()))")
    feature = next(
        (
            f
            for f in persisted.get("features") or []
            if (f.get("properties") or {}).get("id") == new_id
        ),
        None,
    )
    if feature is None:
        die(f"new zone {new_id} not in /zones.json after save")
    if feature.get("geometry", {}).get("type") != "Polygon":
        die(f"persisted rectangle has geometry.type {feature.get('geometry', {}).get('type')!r}")
    print(f"OK drew a rectangle, id {new_id}, saved as a Polygon and persisted")
