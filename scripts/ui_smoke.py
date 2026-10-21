"""Headless-browser smoke for build.yml, against the container it booted.

Loads index.html, checks it renders the SMOKE_ZONES the boot step saved with
no JS errors, and exercises the editor's regression guards. Catches
client-side regressions that endpoint-only probing misses (e.g. the 0.2.6 ->
0.2.8 module-scope bug where /zones.json returned 200 but the page showed
"Failed to load zones" because of a JS ReferenceError).

Run from the repo root as `python3 scripts/ui_smoke.py`; the script's own
directory is then on sys.path, so draw_and_save imports by name.
"""

import asyncio

from draw_and_save import (
    SMOKE_TOKEN,
    draw_and_save,
    draw_rectangle_and_save,
    fail,
    watch_errors,
)
from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8000"
# 127.0.0.1 is a secure context, so on its own it cannot see bugs that only
# bite over plain HTTP on the LAN, which is how HA is often opened (#46).
# This name maps to the same server but is not.
INSECURE_HOST = "polyzones.test"


async def main():
    errors = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            args=[f"--host-resolver-rules=MAP {INSECURE_HOST} 127.0.0.1"]
        )
        # The browser reaches the container directly, not through ingress, so
        # every request it makes, page loads included, carries the token.
        context = await browser.new_context(extra_http_headers={"X-Save-Token": SMOKE_TOKEN})
        page = await context.new_page()
        watch_errors(page, errors)
        await page.goto(f"{BASE}/", wait_until="networkidle")

        body = (await page.locator("body").text_content()) or ""
        if "Failed to load zones" in body:
            fail("page shows 'Failed to load zones'", errors)

        # The boot step saved a Polygon ("smoke") and a MultiPolygon with two
        # shapes ("multi"). Both must render as <zone-entry> elements.
        count = await page.locator("zone-entry").count()
        if count < 2:
            fail(f"expected 2 zone-entry elements, got {count}", errors)

        # The MultiPolygon zone must surface a shape count in its shadow DOM —
        # regression guard for the save-round-trip fix that preserves
        # MultiPolygon geometry.
        shape_count_text = await page.evaluate(
            "() => Array.from(document.querySelectorAll('zone-entry'))"
            ".map(z => z.shadowRoot.querySelector('.shape-count').textContent)"
            ".filter(t => t.trim()).join('|')"
        )
        if "2 shapes" not in shape_count_text:
            fail(
                "expected '(2 shapes)' indicator on the MultiPolygon zone; "
                f"got: {shape_count_text!r}",
                errors,
            )

        # Every zone shows its measured area. Cheap sanity check on a
        # hand-drawn shape — a room-sized figure on something meant to be a
        # property boundary is obvious in the list and invisible on the map.
        area_text = await page.evaluate(
            "() => Array.from(document.querySelectorAll('zone-entry'))"
            ".map(z => z.shadowRoot.querySelector('.zone-area')?.textContent || '')"
            ".filter(t => t.trim()).join('|')"
        )
        if not any(u in area_text for u in ("m²", "ha")):
            fail(f"expected an area figure on each zone; got: {area_text!r}", errors)

        # The tracker overlay must stay completely silent when
        # overlay_entities is unset, which is the default and what this smoke
        # runs with. A visible panel here would mean the opt-out had regressed.
        overlay_hidden = await page.evaluate(
            "() => document.querySelector('.tracker-readout')?.hidden !== false"
        )
        if not overlay_hidden:
            fail("tracker readout is visible with overlay_entities unset", errors)

        # ...and the endpoint that feeds it says so explicitly, rather than
        # 404ing or returning an empty list that the client cannot distinguish
        # from "configured but nothing to show".
        overlay_body = await page.evaluate(
            "async () => (await (await fetch('./trackers.json')).json())"
        )
        if overlay_body != {"configured": False, "trackers": []}:
            fail(f"unexpected /trackers.json body: {overlay_body!r}", errors)

        # A11y regression guard (#116). The Geoman toolbar anchors are patched
        # post-addControls to mirror their title= into aria-label (WCAG 2.2
        # SC 4.1.2). The zone-entry rename input lives inside shadow DOM where
        # <label for=…> never associates per spec, so it uses aria-label
        # directly. Assert the PRIMARY controls specifically. Geoman labels its
        # action buttons (Finish/Cancel) itself, so a check for "any
        # aria-label in the toolbar" passes even when the draw and delete
        # buttons are bare unlabelled icons — which is exactly what the
        # Leaflet-Draw -> Geoman swap initially produced.
        draw_aria = await page.evaluate(
            "() => Array.from(document.querySelectorAll("
            "'.leaflet-pm-toolbar a.leaflet-buttons-control-button'))"
            ".map(a => a.getAttribute('aria-label') || '').join('|')"
        )
        if not draw_aria or "" in draw_aria.split("|"):
            fail(f"Geoman primary toolbar controls missing aria-label; got: {draw_aria!r}", errors)

        # Zone-entry only renders the input when editing, so trigger edit
        # mode on the first entry before asserting.
        await page.evaluate(
            "() => document.querySelector('zone-entry')"
            "?.shadowRoot?.querySelector('.edit-btn')?.click()"
        )
        input_aria = await page.evaluate(
            "() => document.querySelector('zone-entry')"
            "?.shadowRoot?.querySelector('input[type=text]')?.getAttribute('aria-label')"
        )
        if not input_aria:
            fail(f"zone-entry rename input missing aria-label; got: {input_aria!r}", errors)

        # A name typed in one row is kept when the user opens another row
        # instead of pressing Done, and is what Save writes.
        rows = page.locator("zone-entry")
        renamed = "smoke renamed"
        renamed_id = await rows.nth(0).get_attribute("zone-id")
        await rows.nth(0).locator("input[type=text]").fill(renamed)
        await rows.nth(1).locator(".edit-btn").click()
        listed = await rows.nth(0).locator(".zone-name").text_content()
        if listed != renamed:
            fail(f"typed name lost on opening another row: list shows {listed!r}", errors)
        async with page.expect_response(
            lambda r: r.url.endswith("/save_zones") and r.request.method == "POST"
        ) as saved:
            await page.click(".save-btn")
        if (await saved.value).status != 200:
            fail(f"save after rename returned {(await saved.value).status}", errors)
        persisted = await page.evaluate(
            "async () => (await fetch('./zones.json').then(r => r.json()))"
        )
        saved_names = {
            (f.get("properties") or {}).get("id"): (f.get("properties") or {}).get("name")
            for f in persisted.get("features") or []
        }
        if saved_names.get(renamed_id) != renamed:
            fail(f"saved zones.json does not carry the typed name: {saved_names!r}", errors)

        # Tile-picker regression guard (#31, 0.2.28). Asserts the <select>
        # exists, carries the expected four options in order (auto + three
        # basemaps), defaults to 'auto' on a fresh load, and that switching to
        # satellite actually swaps the active Leaflet tile layer to
        # arcgisonline.
        picker_options = await page.evaluate(
            "() => Array.from(document.querySelectorAll('#pz-basemap-select option'))"
            ".map(o => o.value)"
        )
        expected_options = ["auto", "osm", "carto-dark", "esri-imagery"]
        if picker_options != expected_options:
            fail(f"picker options {picker_options!r} != {expected_options!r}", errors)

        picker_initial = await page.evaluate(
            "() => document.getElementById('pz-basemap-select').value"
        )
        if picker_initial != "auto":
            fail(f"picker default value {picker_initial!r} != 'auto' on fresh load", errors)

        await page.select_option("#pz-basemap-select", "esri-imagery")
        await page.wait_for_timeout(150)
        active_url = await page.evaluate(
            """() => {
                let url = null;
                map.eachLayer(l => { if (l instanceof L.TileLayer) url = l._url; });
                return url;
            }"""
        )
        if not active_url or "arcgisonline.com" not in active_url:
            fail(
                "active tile URL after switch to satellite doesn't mention arcgisonline: "
                f"{active_url!r}",
                errors,
            )

        # Reset to 'auto' so the subsequent draw→save round-trip runs on the
        # default OSM layer (no stale satellite state).
        await page.select_option("#pz-basemap-select", "auto")
        await page.wait_for_timeout(150)

        # Draw→save round-trip regression guard. The pm:create handler once
        # built a feature without `type: "Feature"` (422 on save), and later
        # called crypto.randomUUID(), which does not exist over plain HTTP, so
        # the new zone was silently dropped (#46). An earlier version of this
        # check fired Leaflet-Draw's draw:created, which nothing has listened
        # to since the Geoman swap, and passed with no create handler at all.
        # It now draws through the toolbar, first on 127.0.0.1 and then on a
        # non-secure origin.
        await draw_and_save(page, "secure origin", errors, fail)
        await draw_rectangle_and_save(page, errors, fail)

        insecure = await context.new_page()
        watch_errors(insecure, errors)
        await insecure.goto(f"http://{INSECURE_HOST}:8000/", wait_until="networkidle")
        # Without this the check could run on a secure origin and pass whether
        # or not the bug is there.
        if await insecure.evaluate("() => window.isSecureContext") is not False:
            fail(
                f"http://{INSECURE_HOST}:8000/ is a secure context; "
                "the non-secure check would prove nothing"
            )
        await draw_and_save(insecure, "non-secure origin", errors, fail)
        await insecure.close()

        # The server stamps schema_version where the integration reads it
        # (polygonal_zones.schema_version, not top-level), and every saved
        # feature keeps an id: the integration binds automations to it.
        persisted = await page.evaluate(
            "async () => (await fetch('./zones.json').then(r => r.json()))"
        )
        persisted_version = (persisted.get("polygonal_zones") or {}).get("schema_version")
        if persisted_version != 1 or "schema_version" in persisted:
            fail(
                "schema_version must be at polygonal_zones.schema_version==1 and not "
                f"top-level; got nested={persisted_version!r}, "
                f"top_level_present={'schema_version' in persisted}",
                errors,
            )
        for idx, feat in enumerate(persisted.get("features") or []):
            fid = (feat.get("properties") or {}).get("id")
            if not isinstance(fid, str) or len(fid) != 32:
                fail(f"features[{idx}].properties.id missing or wrong shape: {fid!r}", errors)

        # #130 viewport-persistence regression guard (0.2.31). Programmatically
        # pan to LA, wait past the 500ms debounce, reload, assert the persisted
        # center/zoom is honoured.
        await page.evaluate("() => map.setView([34.0522, -118.2437], 11)")
        await page.wait_for_timeout(800)
        stored_viewport = await page.evaluate("() => window.PZStorage.get('pz:viewport')")
        if not stored_viewport or "34.0522" not in stored_viewport:
            fail(f"pz:viewport not written after setView: {stored_viewport!r}", errors)
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_selector("zone-entry", timeout=5000)
        post_reload_center = await page.evaluate(
            "() => ({lat: map.getCenter().lat, lng: map.getCenter().lng})"
        )
        if abs(post_reload_center["lat"] - 34.0522) > 0.01:
            fail(f"viewport not restored across reload: {post_reload_center!r}", errors)

        # #123 save-error-persistence regression guard (0.2.31). Intercept
        # /save_zones, force a 500, trigger a save, wait past the old 2s
        # auto-clear, assert the error is still visible. Kept at the very end
        # of the smoke because page.route applies to subsequent requests only
        # and doesn't need to be unrouted before browser.close.
        async def fail_save(route, request):
            if request.url.endswith("/save_zones") and request.method == "POST":
                await route.fulfill(status=500, body="boom")
            else:
                await route.continue_()

        await page.route("**/*", fail_save)
        await page.evaluate("() => save_zones()")
        await page.wait_for_function(
            "() => document.querySelector('.save-btn').classList.contains('error')",
            timeout=5000,
        )
        await page.wait_for_timeout(2500)
        still_error = await page.evaluate(
            "() => document.querySelector('.save-btn').classList.contains('error') && "
            "(document.getElementById('save-status')?.textContent || '').startsWith('Save failed (500)')"
        )
        if not still_error:
            fail("save error cleared within 2.5s (regression of #123)", errors)

        # Filter out the expected 500 console.error from our own fail_save
        # route — Chromium logs 'Failed to load resource' on any non-OK
        # response, and that lands in errors via watch_errors' console
        # listener. Everything up to the #123 block was asserted clean above;
        # only post-#123 *unexpected* entries should fail the check.
        errors[:] = [e for e in errors if "500 (Internal Server Error)" not in e]

        if errors:
            fail("JS errors during page load:", errors)

        print(f"OK page loaded clean, {count} zone-entry rendered, shape-count ok")
        await browser.close()


asyncio.run(main())
