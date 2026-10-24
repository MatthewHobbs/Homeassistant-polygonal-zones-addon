// Module scope, assigned once /config.json comes back: several functions
// below run before or after generate_map and need the same handles.
let map;
let editableLayers;
// Tracks the ETag of the last zones.json we read or wrote, sent as If-Match
// on /save_zones so a concurrent edit by another tab can't silently overwrite
// us — the server returns 412 and we surface a conflict notice.
let zones_etag = null;
// Whether the editor knows what is on disk: the saved zones loaded, or a bulk
// load replaced them on purpose. Until then a save could overwrite zones the
// editor never showed, so save_zones refuses.
let zones_loaded = false;
let zones_load_failed = false;
// One save at a time: a second click sends the same If-Match and would be
// refused as a conflict with its own first save.
let save_in_flight = false;
// A request that never answers must not block every later click. Timed with
// AbortController + setTimeout rather than AbortSignal.timeout, which
// Safari/iOS before 16 lack.
const REQUEST_TIMEOUT_MS = 30000;
let save_status_timer = null;
const NOT_LOADED_TEXT = {
    pending: 'The saved zones are still loading. Try again in a moment.',
    failed: "The saved zones couldn't be loaded, so saving now could overwrite them. Reload the page and try again.",
};

/* Colour the Save button (cls: 'success', 'error' or null) and say why. */
function show_save_state(cls, text) {
    const btn = document.querySelector('.save-btn');
    const status = document.getElementById('save-status');
    if (btn) {
        btn.classList.remove('success', 'error');
        if (cls) btn.classList.add(cls);
    }
    if (status) status.textContent = text;
}

/* Once saving is possible, drop a not-loaded refusal still on screen. */
function clear_not_loaded_refusal() {
    const status = document.getElementById('save-status');
    if (status && Object.values(NOT_LOADED_TEXT).includes(status.textContent)) show_save_state(null, '');
}
// isDirty flips to true on any edit the user hasn't flushed to the server
// (draw, delete, rename, bulk-load). Cleared on a successful save. Drives
// the beforeunload prompt below so HA's ingress shell can't silently discard
// unsaved polygons when the user taps another sidebar entry.
let isDirty = false;
// Bumped on every edit, so a save only marks clean what it actually sent.
let edit_seq = 0;
function mark_dirty() { isDirty = true; edit_seq++; update_dirty_indicator(); }
function mark_clean() { isDirty = false; update_dirty_indicator(); }
function update_dirty_indicator() {
    // The beforeunload prompt only fires on navigation, which the ingress
    // tab-switch doesn't trigger, so the page needs its own cue.
    let dot = document.querySelector('.dirty-dot');
    if (dot) dot.hidden = !isDirty;
}

window.addEventListener('beforeunload', (e) => {
    if (!isDirty) return;
    // Browsers show a generic "Leave site?" prompt and ignore the text.
    // returnValue is kept for older Safari; programmatic navigation is exempt.
    e.preventDefault();
    e.returnValue = '';
});

// addEventListener rather than an inline onclick, so the CSP's script-src
// needs no 'unsafe-inline'. This file loads at the end of <body>, so the
// button already exists.
const saveButton = document.querySelector('.save-btn');
if (saveButton) saveButton.addEventListener('click', save_zones);

fetch('./config.json')
    .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
    .catch(err => {
        console.warn('config.json fetch failed, using defaults:', err);
        return {};
    })
    .then(cfg => {
        window.ZONE_COLOUR = cfg.zone_colour || 'green';
        // theme: 'auto' (default) follows OS prefers-color-scheme.
        // 'light' / 'dark' force the choice, ignoring OS preference.
        const theme = (cfg.theme === 'light' || cfg.theme === 'dark') ? cfg.theme : 'auto';
        if (theme !== 'auto') document.documentElement.dataset.theme = theme;
        window.PZ_THEME = theme;
        ({map, editableLayers} = generate_map('./zones.json'));
        setup_editing(map, editableLayers);
        window.PZUiState.initSidebarState(map);
    });

// Consecutive tileerror events across the active basemap. Threshold of 5
// lets a single flaky tile or momentary DNS blip pass without nagging the
// user, but catches a dead provider within a couple of seconds of panning.
let consecutiveTileErrors = 0;
const TILE_ERROR_THRESHOLD = 5;

function reset_tile_error_banner() {
    consecutiveTileErrors = 0;
    const banner = document.getElementById('tile-error-banner');
    if (banner) banner.hidden = true;
}

function attach_tile_error_watch(layer) {
    layer.on('tileerror', () => {
        consecutiveTileErrors++;
        if (consecutiveTileErrors >= TILE_ERROR_THRESHOLD) {
            const banner = document.getElementById('tile-error-banner');
            if (banner) banner.hidden = false;
        }
    });
    layer.on('tileload', () => {
        if (consecutiveTileErrors > 0) reset_tile_error_banner();
    });
}

// Only used when there is no remembered viewport and no zones to fit: an empty
// stretch of ocean at world scale. It is obviously "not your area", so the
// first pan is the user's own, and it points at nobody.
const DEFAULT_CENTER = [0, 0];
const DEFAULT_ZOOM = 2;

function restore_viewport() {
    const raw = window.PZStorage.get('pz:viewport');
    if (!raw) return {center: DEFAULT_CENTER, zoom: DEFAULT_ZOOM, restored: false};
    let parsed;
    try { parsed = JSON.parse(raw); }
    catch (e) { return {center: DEFAULT_CENTER, zoom: DEFAULT_ZOOM, restored: false}; }
    const c = parsed && parsed.center;
    const z = parsed && parsed.zoom;
    const validCenter = Array.isArray(c) && c.length === 2
        && typeof c[0] === 'number' && typeof c[1] === 'number'
        && isFinite(c[0]) && isFinite(c[1])
        && c[0] >= -90 && c[0] <= 90 && c[1] >= -180 && c[1] <= 180;
    const validZoom = typeof z === 'number' && isFinite(z) && z >= 0 && z <= 25;
    if (!validCenter || !validZoom) {
        return {center: DEFAULT_CENTER, zoom: DEFAULT_ZOOM, restored: false};
    }
    return {center: c, zoom: z, restored: true};
}


function setup_basemap_picker(layers, onChange) {
    const select = document.getElementById('pz-basemap-select');
    if (!select) return;

    select.innerHTML = '';
    const autoOpt = document.createElement('option');
    autoOpt.value = 'auto';
    autoOpt.textContent = 'Auto (follows theme)';
    select.appendChild(autoOpt);

    window.PZBasemaps.listBasemaps().forEach(b => {
        if (!layers[b.id]) return;
        const opt = document.createElement('option');
        opt.value = b.id;
        opt.textContent = b.label;
        select.appendChild(opt);
    });

    const stored = window.PZStorage.get('pz:basemap');
    select.value = (stored && stored !== 'auto' && layers[stored]) ? stored : 'auto';

    select.addEventListener('change', () => onChange(select.value));
}


function generate_map(zones_url) {
    // One layer per basemap, built once, so the picker swaps layers rather
    // than rebuilding them and each gets exactly one tile-error watch.
    const layers = {};
    window.PZBasemaps.listBasemaps().forEach(b => {
        const layer = window.PZBasemaps.createLayer(b.id);
        if (layer) {
            layers[b.id] = layer;
            attach_tile_error_watch(layer);
        }
    });

    // Restore the user's persisted explicit choice, or fall back to the
    // theme-appropriate default. userChoseTile tracks whether the user
    // made an explicit pick: when true, the prefers-color-scheme auto-
    // swap below is suppressed so the user's choice isn't overridden
    // when the OS light/dark preference changes.
    const stored = window.PZStorage.get('pz:basemap');
    let userChoseTile = false;
    let activeId;
    if (stored && stored !== 'auto' && layers[stored]) {
        activeId = stored;
        userChoseTile = true;
    } else {
        activeId = window.PZBasemaps.getDefaultBasemap(window.PZ_THEME).id;
    }

    const {center: initialCenter, zoom: initialZoom, restored: restoredViewport} =
        restore_viewport();

    const map = L.map('map', {
        layers: [layers[activeId]],
        center: initialCenter,
        zoom: initialZoom,
    });

    // Debounced so a pan doesn't hammer localStorage. A zoom fires moveend too.
    let viewportSaveTimer = null;
    const schedule_viewport_save = () => {
        if (viewportSaveTimer) clearTimeout(viewportSaveTimer);
        viewportSaveTimer = setTimeout(() => {
            const c = map.getCenter();
            window.PZStorage.set('pz:viewport', JSON.stringify({
                center: [c.lat, c.lng],
                zoom: map.getZoom(),
            }));
        }, 500);
    };
    map.on('moveend', schedule_viewport_save);

    // swap_to drives every layer change the app makes — picker events,
    // theme auto-swap, and the initial picker-populate — so it's the
    // single place that resets the tile-error banner (stale errors from
    // a dead provider shouldn't linger after the user switches).
    function swap_to(newId) {
        if (!layers[newId]) return;
        if (newId === activeId) return;
        map.removeLayer(layers[activeId]);
        layers[newId].addTo(map);
        activeId = newId;
        reset_tile_error_banner();
    }

    // Prefers-color-scheme auto-swap: only runs when the user hasn't
    // picked an explicit basemap AND the theme option is 'auto' (forced
    // light/dark via config skips the OS-follow entirely).
    if (window.PZ_THEME !== 'light' && window.PZ_THEME !== 'dark') {
        const dark_mq = window.matchMedia('(prefers-color-scheme: dark)');
        dark_mq.addEventListener('change', e => {
            if (userChoseTile) return;
            const preferredId = window.PZBasemaps.getDefaultBasemap(
                e.matches ? 'dark' : 'light').id;
            swap_to(preferredId);
        });
    }

    setup_basemap_picker(layers, (choice) => {
        if (choice === 'auto') {
            userChoseTile = false;
            window.PZStorage.remove('pz:basemap');
            swap_to(window.PZBasemaps.getDefaultBasemap(window.PZ_THEME).id);
        } else {
            userChoseTile = true;
            window.PZStorage.set('pz:basemap', choice);
            swap_to(choice);
        }
    });

    let editableLayers = new L.FeatureGroup();
    map.addLayer(editableLayers);

    // Loading affordance so an in-flight fetch is distinguishable from a
    // genuinely empty zone list. Cleared by render_zone_list() or by
    // load_zones on failure.
    let _loading_list = document.querySelector('.zone-list');
    if (_loading_list) {
        _loading_list.setAttribute('aria-busy', 'true');
        _loading_list.textContent = 'Loading zones…';
    }

    load_zones(zones_url, restoredViewport);

    // Opt-in tracker overlay. Resolves to nothing when overlay_entities is
    // unset, which is the default.
    if (typeof setup_tracker_overlay === 'function') setup_tracker_overlay(map);

    return {map, editableLayers};
}

/* The first load of the saved zones. Save stays refused until it succeeds. */
function load_zones(zones_url, restoredViewport) {
    // A stalled load would keep Save refused for good.
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    return fetch(zones_url, {signal: controller.signal})
        .then(response => {
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            return response.text().then(json => {
                // A bulk load that landed first replaced the saved zones on
                // purpose; this answer is stale.
                if (zones_loaded) return;
                let data;
                try { data = JSON.parse(json); }
                catch (e) { throw new Error(`zones.json parse failed: ${e.message}`); }
                zones_etag = response.headers.get('ETag');
                load_saved_zones(data, restoredViewport);
            });
        })
        .catch(err => {
            console.error('Failed to load zones:', err);
            if (zones_loaded) return;
            zones_load_failed = true;
            let list = document.querySelector('.zone-list');
            if (list) {
                list.removeAttribute('aria-busy');
                list.textContent =
                    "Couldn't load zones. Check the add-on log (Settings → Add-ons → Polygonal Zones → Log).";
            }
            create_load_btn();
        })
        .finally(() => clearTimeout(timer));
}

function setup_editing(map, editableLayers) {
    // Only the controls this editor needs are enabled: zones are polygons, so
    // every other shape is off. drawRectangle stays on: a rectangle is drawn
    // as an L.Rectangle, which extends L.Polygon, so toGeoJSON() emits a
    // plain Polygon indistinguishable from one drawn point-by-point.
    map.pm.setGlobalOptions({
        allowSelfIntersection: false,
        snappable: true,
        snapDistance: 12,
        pathOptions: { color: window.ZONE_COLOUR },
        templineStyle: { color: window.ZONE_COLOUR },
        hintlineStyle: { color: window.ZONE_COLOUR, dashArray: '5,5' },
    });

    map.pm.addControls({
        position: 'topleft',
        drawPolygon: true,
        removalMode: true,
        // Per-zone editing is driven from the sidebar's edit button
        // (edit_zone_event), not a global toolbar mode — keeping the toolbar
        // free of a second, competing way to enter edit mode.
        editMode: false,
        dragMode: false,
        cutPolygon: false,
        rotateMode: false,
        drawMarker: false,
        drawCircle: false,
        drawCircleMarker: false,
        drawPolyline: false,
        drawRectangle: true,
        drawText: false,
    });

// Toolbar accessibility (WCAG 2.2 SC 4.1.2). Geoman's action buttons (Finish,
// Cancel, Remove Last Vertex) carry only a title, which NVDA and VoiceOver
// announce inconsistently, so it is mirrored into aria-label.

// Its primary draw and delete buttons are bare icons with nothing to mirror,
// so they are named from the icon class. Anchors have no href, so
// role="button" makes them announce as the controls they are.
    const PM_CONTROL_LABELS = {
        'leaflet-pm-icon-polygon': 'Draw a zone',
        'leaflet-pm-icon-rectangle': 'Draw a rectangular zone',
        'leaflet-pm-icon-delete': 'Delete a zone',
    };
    Promise.resolve().then(() => {
        // Geoman renders one .leaflet-pm-toolbar per control block; the
        // delete button is not in the first.
        document.querySelectorAll('.leaflet-pm-toolbar a[title]:not([aria-label])')
            .forEach(a => a.setAttribute('aria-label', a.getAttribute('title')));

        document.querySelectorAll('.leaflet-pm-toolbar a.leaflet-buttons-control-button')
            .forEach(a => {
                if (!a.hasAttribute('role')) a.setAttribute('role', 'button');
                if (a.hasAttribute('aria-label')) return;
                const icon = a.querySelector('[class*="leaflet-pm-icon-"]');
                if (!icon) return;
                const key = Array.from(icon.classList)
                    .find(c => Object.prototype.hasOwnProperty.call(PM_CONTROL_LABELS, c));
                if (key) {
                    a.setAttribute('aria-label', PM_CONTROL_LABELS[key]);
                    // Sighted keyboard users get a tooltip too; Geoman ships none.
                    if (!a.hasAttribute('title')) a.setAttribute('title', PM_CONTROL_LABELS[key]);
                }
            });
    });

    // Geoman fires pm:edit on the layer and its parent groups, never on the
    // map. No list rebuild: that would close the row being edited.
    editableLayers.on('pm:edit', e => {
        mark_dirty();
        const entry = Array.from(document.querySelectorAll('zone-entry'))
            .find(z => z.getAttribute('zone-id') === e.layer.feature.properties.id);
        if (entry) show_zone_area(entry, e.layer);
        map.fire('pz:zoneschanged');
    });

    // Geoman removes one layer per click, so there is no batch of deletions
    // to fold into one rebuild.
    map.on('pm:remove', function (e) {
        if (!editableLayers.hasLayer(e.layer)) return;
        editableLayers.removeLayer(e.layer);
        render_zone_list();
        mark_dirty();

        if (editableLayers.getLayers().length === 0) {
            create_load_btn();
        }
    });

    map.on('pm:create', function (e) {
        let layer = e.layer;

        // One past the highest "Zone N", not the layer count, which repeats a
        // name after a deletion.
        let maxN = 0;
        editableLayers.eachLayer(l => {
            let m = /^Zone (\d+)$/.exec(l.feature?.properties?.name ?? '');
            if (m) maxN = Math.max(maxN, parseInt(m[1], 10));
        });
        let name = `Zone ${maxN + 1}`;
        // toGeoJSON() extends layer.feature with a geometry but never adds
        // `type`, and the server refuses a feature without it. The id keeps
        // automations bound to the zone across renames.
        layer.feature = {
            type: 'Feature',
            properties: {
                name: name,
                id: new_zone_id()
            }
        };

        // Geoman adds the new shape to the map itself. Take it off the map
        // before handing it to editableLayers, or it ends up owned by both
        // and a later group removal leaves an orphan rendering on the map.
        map.removeLayer(layer);
        layer.bindPopup(l => text_element(l.feature.properties.name));
        editableLayers.addLayer(layer);
        render_zone_list();
        mark_dirty();
    });
}

// 32 lowercase hex characters: a UUID with the dashes removed. randomUUID
// exists only in secure contexts, and HA is often opened over plain HTTP on
// the LAN, where calling it throws.
function new_zone_id() {
    if (typeof crypto.randomUUID === 'function') {
        return crypto.randomUUID().replace(/-/g, '');
    }
    return Array.from(crypto.getRandomValues(new Uint8Array(16)),
        b => b.toString(16).padStart(2, '0')).join('');
}

/* Finish the open row as its Done button would, so a name typed there is kept
 * by whatever comes next: a save, another row, or a list rebuild. */
function close_open_edit() {
    document.querySelector('zone-entry[editing="true"]')?.edit_event_handler();
}

function edit_zone_event(e) {
    // Every zone has an id (drawn ones at creation, loaded ones in add_zones),
    // so the id alone finds it whatever its name.
    let layer = editableLayers.getLayers().find(l => l.feature.properties.id === e.detail.id);
    // The zone was deleted while its row was open.
    if (!layer) return;

    if (e.detail.editing) {
        close_open_edit();
        map.fitBounds(layer.getBounds());
        // A self-crossing ring is a zone whose inside is ambiguous.
        layer.pm.enable({ allowSelfIntersection: false });
    } else {
        layer.pm.disable();
        // An empty box leaves the zone unnamed: two zones named "" would be
        // refused on save as duplicates.
        const old_name = layer.feature.properties.name;
        if (e.detail.name) layer.feature.properties.name = e.detail.name;
        else delete layer.feature.properties.name;
        e.target.setAttribute('name', e.detail.name);
        if ((layer.feature.properties.name ?? '') !== (old_name ?? '')) {
            mark_dirty();
            map.fire('pz:zoneschanged');
        }
    }
}

// Area is the cheapest sanity check there is on a hand-drawn zone: a
// room-sized number on something meant to be a property boundary is obvious at
// a glance and invisible on the map itself. trackers.js measures it.
function show_zone_area(entry, layer) {
    if (typeof pz_layer_area_m2 !== 'function') return;
    const area = pz_layer_area_m2(layer);
    if (area > 0) entry.setAttribute('area-m2', String(Math.round(area)));
    else entry.removeAttribute('area-m2');
}

function render_zone_list() {
    close_open_edit();
    let zone_list = document.querySelector('.zone-list');
    zone_list.innerHTML = '';
    zone_list.removeAttribute('aria-busy');
    editableLayers.eachLayer(layer => {
        let zone_entry = document.createElement('zone-entry');
        // A zone may have no name; an absent attribute value would read "undefined".
        zone_entry.setAttribute('name', layer.feature.properties.name ?? '');
        // Edit events find the layer by id, since names may repeat or be absent.
        zone_entry.setAttribute('zone-id', layer.feature.properties.id);
        if (typeof pz_layer_polygons === 'function') {
            const count = pz_layer_polygons(layer).length;
            if (count > 1) zone_entry.setAttribute('shape-count', String(count));
        }
        show_zone_area(zone_entry, layer);
        zone_entry.addEventListener('edit', edit_zone_event);

        zone_list.appendChild(zone_entry);
    });

    // Let the tracker overlay (if any) re-measure against the new shapes.
    map.fire('pz:zoneschanged');

    // Distinct empty state — a blank list otherwise reads identically to a
    // still-loading one.
    if (!zone_list.children.length) {
        let empty = document.createElement('p');
        empty.className = 'zone-list-empty';
        empty.textContent = 'No zones yet. Draw one on the map to get started.';
        zone_list.appendChild(empty);
    }
}

/* The zones on the map as the FeatureCollection a save sends. Leaflet's
 * toGeoJSON keeps a MultiPolygon's every ring; reading _latlngs[0] by hand
 * would not. */
function zones_collection() {
    const features = [];
    editableLayers.eachLayer(layer => {
        const feature = layer.toGeoJSON();
        // Every property is sent back: priority decides overlapping zones and
        // polygonal_zones_ext belongs to whoever wrote it. Only the name and id
        // are the editor's to set.
        const src = layer.feature.properties || {};
        feature.properties = {...src, name: src.name};
        if (src.id) feature.properties.id = src.id;
        features.push(feature);
    });
    return {type: "FeatureCollection", features};
}

function save_zones() {
    // Ignored while a save is on its way, before anything below changes what
    // the user sees.
    if (save_in_flight) return Promise.resolve();
    if (!zones_loaded) {
        show_save_state('error', zones_load_failed ? NOT_LOADED_TEXT.failed : NOT_LOADED_TEXT.pending);
        return Promise.resolve();
    }

    close_open_edit();
    const geojson = zones_collection();

    const headers = {'Content-Type': 'application/json'};
    if (zones_etag) headers['If-Match'] = zones_etag;

    save_in_flight = true;
    const sent_seq = edit_seq;
    clearTimeout(save_status_timer);
    // Errors stay on screen until this, the next click: one that vanished
    // after a moment left users unsure whether their zones saved.
    show_save_state(null, 'Saving…');
    const controller = new AbortController();
    let timed_out = false;
    const timer = setTimeout(() => { timed_out = true; controller.abort(); }, REQUEST_TIMEOUT_MS);
    // Started inside the chain, so even a synchronous throw reaches .finally
    // and Save is never left blocked.
    return Promise.resolve().then(() => fetch('./save_zones', {
        method: 'POST',
        headers: headers,
        body: JSON.stringify(geojson),
        signal: controller.signal,
    })).then(response => {
        // Stays dirty: the edits are still unsaved.
        if (response.status === 412) {
            show_save_state('error',
                'Conflict: zones changed in another session. Reload and re-apply your edits, or download yours, reload, and use Replace zones from file (that discards the other session\'s changes).');
            return offer_download();
        }
        if (!response.ok) {
            // The server's reason (main.py: {"error", "detail"}) is shown as text only.
            return response.json().catch(() => null).then(body => {
                const detail = typeof body?.detail === 'string' ? `: ${body.detail}` : '.';
                show_save_state('error', `Save failed (${response.status})${detail}`);
            });
        }
        return response.json().catch(() => null).then(body => {
            // The save response says whether Home Assistant re-read the file.
            const reload = body?.reload;
            const note = typeof reload?.note === 'string' && reload.note ? ` ${reload.note}` : '';
            show_save_state('success', `Zones saved.${note}`);
// Only a save that landed moves the editor to the new version: a 412 carries
// the other session's ETag, and adopting it would let the next click overwrite
// their edits. The body's value is checked too — it becomes a request header.
            const body_etag = /^"[0-9a-f]+"$/.test(body?.etag) ? body.etag : null;
            const new_etag = response.headers.get('ETag') || body_etag;
            if (new_etag) zones_etag = new_etag;
            // An edit made while the request was in flight is still unsaved.
            if (edit_seq === sent_seq) mark_clean();
            // A note about Home Assistant stays up; a bare "saved" fades.
            if (!note) save_status_timer = setTimeout(() => show_save_state(null, ''), 2000);
        });
    }).catch(err => {
        show_save_state('error', timed_out
            // The server may still have written: say so rather than "not saved".
            ? 'Save timed out. It may not have saved; reload to check before saving again.'
            : `Save failed: ${err.message}`);
        if (timed_out) offer_download();
    }).finally(() => {
        clearTimeout(timer);
        save_in_flight = false;
    });
}

/* Reloading to resolve a conflict discards the edits, so offer them as a file
 * "Replace zones from file" accepts. Built on click, so edits made since are in it. The
 * control sits in the status line, which the next save's status text replaces. */
function offer_download() {
    const status = document.getElementById('save-status');
    if (!status) return;
    const button = document.createElement('button');
    button.className = 'btn';
    button.textContent = 'Download unsaved zones';
    button.addEventListener('click', () => {
        close_open_edit();
        const url = URL.createObjectURL(new Blob([JSON.stringify(zones_collection())], {type: 'application/json'}));
        const link = document.createElement('a');
        link.href = url;
        link.download = 'unsaved-zones.json';
        // Attached and revoked late: some browsers drop a detached or
        // immediately revoked download.
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 10000);
    });
    status.appendChild(button);
}

function show_transient_error(message) {
    // alert() is unreliable inside Home Assistant's ingress iframe (sandboxed
    // dialogs may be suppressed), so surface errors as an in-page banner.
    let el = document.createElement('div');
    el.setAttribute('role', 'alert');
    el.className = 'transient-error';
    el.textContent = message;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 5000);
}

/* Show the saved zones from the first load. They join any zone drawn while
 * the fetch was in flight rather than replacing it. */
function load_saved_zones(data, restoredViewport) {
    const {skipped} = add_zones(data, false);
    zones_loaded = true;
    clear_not_loaded_refusal();
    render_zone_list();
    if (skipped) {
        show_transient_error(`${skipped} saved feature(s) are not polygon zones; they were not loaded and the next save drops them.`);
    }
    // Always offered: it is how a conflict download gets back in.
    create_load_btn();
    if (editableLayers.getLayers().length > 0 && !restoredViewport) {
        map.setView(editableLayers.getBounds().getCenter(), 13);
    }
}

/* Add the polygon zones in `data`; return {count, skipped}. Layers are built
 * before anything changes. With `replace`, a non-polygon throws and leaves the
 * editor as it was; otherwise zones are added beside what is drawn. An unnamed
 * zone is kept — dropping it would delete it on the next save. */
function add_zones(data, replace) {
    const layers = [];
    L.geoJSON(data, {
        style: () => ({color: window.ZONE_COLOUR}),
        onEachFeature: (feature, layer) => {
            // GeoJSON allows null properties, and older files contain them.
            if (!feature.properties) feature.properties = {};
            // A function, so the popup reads the name at open, after any rename.
            layer.bindPopup(l => text_element(l.feature.properties.name));
            layers.push(layer);
        },
    });
    // Polygon covers MultiPolygon too; the server refuses anything else.
    const zones = layers.filter(layer => layer instanceof L.Polygon);
    const skipped = layers.length - zones.length;
    if (replace) {
        if (skipped) throw new Error('not polygon zones');
        // The server requires a non-blank name and an id that is unique,
        // non-empty text; refuse here rather than at save.
        const ids = new Set();
        for (const {feature: {properties: {name, id}}} of zones) {
            if (typeof name !== 'string' || !name.trim()) throw new Error('a zone has no name');
            if (id == null) continue;
            if (typeof id !== 'string' || !id || ids.has(id)) throw new Error('bad zone id');
            ids.add(id);
        }
        if (!zones.length) return {count: 0, skipped: 0};
        editableLayers.clearLayers();
    }
    zones.forEach(layer => {
        // Every zone gets a text id, so its list entry finds it even when
        // names are missing or repeated. The server keeps an id it is given.
        const p = layer.feature.properties;
        if (typeof p.id !== 'string' || !p.id) p.id = new_zone_id();
        editableLayers.addLayer(layer);
    });
    return {count: zones.length, skipped};
}

function load_bulk_json() {
    let input = document.createElement('input');
    input.type = 'file';
    input.accept = '.json';
    input.addEventListener('change', function () {
        let file = input.files[0];
        let reader = new FileReader();
        reader.onload = function (e) {
            let data;
            try { data = JSON.parse(e.target.result); }
            catch (err) {
                show_transient_error('The selected file is not valid JSON.');
                return;
            }
            let count;
            try { ({count} = add_zones(data, true)); }
            catch (err) {
                show_transient_error('The selected file is not GeoJSON polygon zones.');
                return;
            }
            if (!count) {
                show_transient_error('The selected file has no zones in it.');
                return;
            }
            zones_loaded = true;
            clear_not_loaded_refusal();
            render_zone_list();
            mark_dirty();
            map.setView(editableLayers.getBounds().getCenter(), 13);
        };
        reader.readAsText(file);
    });
    input.click();
}