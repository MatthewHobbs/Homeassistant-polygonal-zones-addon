// Module-scope handles so render_zone_list / save_zones / load_bulk_json /
// edit_zone_event can reference them via closure regardless of when
// generate_map runs. Assigned once /config.json comes back (we need
// ZONE_COLOUR set before any zones render).
let map;
let editableLayers;
// Tracks the ETag of the last zones.json we read or wrote, sent as If-Match
// on /save_zones so a concurrent edit by another tab can't silently overwrite
// us — the server returns 412 and we surface a conflict notice.
let zones_etag = null;
// isDirty flips to true on any edit the user hasn't flushed to the server
// (draw, delete, rename, bulk-load). Cleared on a successful save. Drives
// the beforeunload prompt below so HA's ingress shell can't silently discard
// unsaved polygons when the user taps another sidebar entry.
let isDirty = false;
function mark_dirty() { isDirty = true; }
function mark_clean() { isDirty = false; }

window.addEventListener('beforeunload', (e) => {
    if (!isDirty) return;
    // Modern Chrome/Firefox/Safari ignore the string; they show a generic
    // "Leave site?" prompt when preventDefault is called. returnValue = ''
    // is kept for older Safari compatibility. The prompt can't be
    // customised, and triggers only on user-initiated navigations — the
    // browser still suppresses it on programmatic location changes.
    e.preventDefault();
    e.returnValue = '';
});

// Wire the Save button via addEventListener rather than an inline onclick
// attribute so the CSP's script-src can drop 'unsafe-inline' (#128). The
// button lives in index.html; this file is loaded at the end of <body>
// so the DOM is ready at parse time.
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

// User-facing labels for the basemap picker. Separate from the registry's
// `label` field (which carries provider names like "OpenStreetMap") so the
// picker can show friendlier copy without losing the provider identity in
// the attribution control Leaflet renders on the map itself.
const BASEMAP_PICKER_LABELS = {
    'osm': 'Street map',
    'carto-dark': 'Dark',
    'esri-imagery': 'Satellite',
};

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

// Default to Groningen (the original upstream author's home) only when no
// persisted viewport is available AND the user has no existing zones. It's
// an obvious "not-my-area" signal rather than a plausible-looking wrong
// location, so users outside the Netherlands know to pan immediately.
const DEFAULT_CENTER = [52.96523540264812, 6.52002831753822];
const DEFAULT_ZOOM = 13;

// Read pz:viewport from PZStorage and validate its shape. Returns an
// object carrying the initial center / zoom to hand L.map(), and a
// `restored` flag the caller uses to decide whether to auto-fit to zone
// bounds on first load.
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
        opt.textContent = BASEMAP_PICKER_LABELS[b.id] || b.label;
        select.appendChild(opt);
    });

    // Reflect the current state. When pz:basemap is unset or 'auto', the
    // picker shows 'auto'; otherwise it shows the stored id.
    const stored = window.PZStorage.get('pz:basemap');
    select.value = (stored && stored !== 'auto' && layers[stored]) ? stored : 'auto';

    select.addEventListener('change', () => onChange(select.value));
}


function generate_map(zones_url) {
    // Build one Leaflet layer instance per registered basemap. The picker
    // (#31) swaps the active layer by add/remove rather than constructing
    // fresh layer objects each time — cheaper, and the tile-error watch
    // below gets attached exactly once per layer.
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

    // Viewport restoration (#130). If the user has panned / zoomed before,
    // we remembered where — no more dumping Groningen (the original
    // upstream author's home) on users outside the Netherlands every first
    // load. Falls back to the Groningen default if no viewport is stored.
    const {center: initialCenter, zoom: initialZoom, restored: restoredViewport} =
        restore_viewport();

    const map = L.map('map', {
        layers: [layers[activeId]],
        center: initialCenter,
        zoom: initialZoom,
    });

    // Persist viewport changes on moveend/zoomend, debounced so a single
    // user pan doesn't hammer localStorage.
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
    map.on('zoomend', schedule_viewport_save);

    // swap_to drives every layer change the app makes — picker events,
    // theme auto-swap, and the initial picker-populate — so it's the
    // single place that resets the tile-error banner (stale errors from
    // a dead provider shouldn't linger after the user switches).
    function swap_to(newId) {
        if (!layers[newId]) return;
        if (newId === activeId) return;
        Object.values(layers).forEach(l => {
            if (l && map.hasLayer(l)) map.removeLayer(l);
        });
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

    fetch(zones_url)
        .then(response => {
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            zones_etag = response.headers.get('ETag');
            return response.text();
        })
        .then(json => {
            let data;
            try { data = JSON.parse(json); }
            catch (e) { throw new Error(`zones.json parse failed: ${e.message}`); }

            L.geoJSON(data, {
                style: () => ({color: window.ZONE_COLOUR}),
                onEachFeature: (feature, layer) => {
                    layer.bindPopup(name_popup_element(feature));
                    editableLayers.addLayer(layer);
                },
            })

            render_zone_list();
            // Only auto-fit to the zones when the user hasn't manually
            // positioned the viewport before (#130). Otherwise their
            // remembered center/zoom wins — someone who bounced around
            // multiple HA installs shouldn't get snapped to the zone
            // bounding box on every first load.
            if (editableLayers.getLayers().length > 0) {
                if (!restoredViewport) {
                    map.fitBounds(editableLayers.getBounds());
                    map.setView(editableLayers.getBounds().getCenter(), 13);
                }
            } else {
                create_load_btn();
            }
        })
        .catch(err => {
            console.error('Failed to load zones:', err);
            let list = document.querySelector('.zone-list');
            if (list) list.textContent = 'Failed to load zones — check the log.';
            create_load_btn();
        });

    return {map, editableLayers};
}

function setup_editing(map, editableLayers) {
    const drawing_options = {
        position: 'topleft',
        draw: {
            polyline: false,
            polygon: {
                allowIntersection: false,
                drawError: {
                    color: '#e1e100',
                    message: '<strong>Oh snap!<strong> you can\'t draw that!'
                },
                shapeOptions: {
                    color: window.ZONE_COLOUR
                }
            },
            circle: false,
            rectangle: false,
            marker: false,
            circlemarker: false
        },
        edit: {
            featureGroup: editableLayers,
            edit: false,
            remove: true
        }
    };

    let drawControl = new L.Control.Draw(drawing_options);
    let drawnItems = new L.FeatureGroup();

    map.addControl(drawControl);
    map.addLayer(drawnItems);

    // Leaflet-draw toolbar anchors ship with a `title` attribute only; many
    // screen readers (NVDA, VoiceOver on iOS) either ignore or inconsistently
    // announce `title` for interactive anchors. Copy every title into an
    // aria-label so the Draw / Delete / Clear-all controls are programmatically
    // labelled (WCAG 2.2 SC 4.1.2). Deferred to the next microtask because
    // addControl synchronously inserts the DOM but the exact anchor set depends
    // on the drawing_options object (no marker, no circle, etc.).
    Promise.resolve().then(() => {
        document
            .querySelectorAll('.leaflet-draw-toolbar a[title]:not([aria-label])')
            .forEach(a => a.setAttribute('aria-label', a.getAttribute('title')));
    });

    map.on('draw:deleted', function (e) {
        let layers = e.layers;
        layers.eachLayer(layer => {
            editableLayers.removeLayer(layer);
            render_zone_list();
        });
        mark_dirty();

        if (editableLayers.getLayers().length === 0) {
            create_load_btn();
        }
    });

    map.on('draw:created', function (e) {
        let type = e.layerType,
            layer = e.layer;

        if (type === 'marker') {
            layer.bindPopup('A popup!');
        }

        // generate a name according to `zone {n}`
        let name = `Zone ${editableLayers.getLayers().length + 1}`;
        // `type: 'Feature'` is required. Leaflet's toGeoJSON() uses this
        // object as a template — when `layer.feature` is already set it
        // extends it with a `geometry` field but does NOT auto-add the
        // `type`. Without this, saving a drawn polygon POSTed features
        // missing `"type":"Feature"`, which the server-side validator
        // rejects with 422. Existed in the code since the handler was
        // written but not caught because no test exercised the
        // draw → save round-trip end-to-end.
        layer.feature = {
            type: 'Feature',
            properties: {
                name: name
            }
        };

        editableLayers.addLayer(layer);
        render_zone_list();
        mark_dirty();

        delete_load_btn();
    });
}

function edit_zone_event(e) {
    // disable editing for all zones.
    editableLayers.eachLayer(layer => layer.editing.disable());
    document.querySelectorAll('zone-entry').forEach(zone => zone.setAttribute('editing', 'false'));

    let oldName = e.detail.oldName || e.detail.name
    let layer = editableLayers.getLayers().find(layer => layer.feature.properties.name === oldName);

    // if we start editing a zone, enable editing for that zone
    if (e.detail.editing) {
        map.fitBounds(layer.getBounds());
        layer.editing.enable();
    } else {
        // once we stop we will disable editing and save the changes
        layer.feature.properties.name = e.detail.name;
        e.target.setAttribute('name', e.detail.name);
        mark_dirty();
    }
}

// Return the number of polygon shapes a layer represents. 1 for a plain
// Polygon; N for a MultiPolygon (read via public toGeoJSON so we don't
// depend on Leaflet's internal _latlngs shape).
function shape_count(layer) {
    const geometry = layer.toGeoJSON().geometry;
    if (geometry && geometry.type === 'MultiPolygon') {
        return geometry.coordinates.length;
    }
    return 1;
}

function render_zone_list() {
    let zone_list = document.querySelector('.zone-list');
    zone_list.innerHTML = '';
    editableLayers.eachLayer(layer => {
        // render a zone-entry element and set attribute name
        let zone_entry = document.createElement('zone-entry');
        zone_entry.setAttribute('name', layer.feature.properties.name);
        const count = shape_count(layer);
        if (count > 1) zone_entry.setAttribute('shape-count', String(count));
        zone_entry.addEventListener('edit', edit_zone_event);

        zone_list.appendChild(zone_entry);
    });
}

function save_zones() {
    // Clear any prior error state on a fresh save attempt. #123: previous
    // errors persist on screen until the next user action, so this is the
    // "next action" that dismisses them. Success auto-clears at the
    // setTimeout below; 412 conflicts stay until explicitly dismissed here.
    const save_btn = document.querySelector('.save-btn');
    const save_status = document.getElementById('save-status');
    if (save_btn) save_btn.classList.remove('error');
    if (save_status && save_status.textContent && save_status.textContent !== 'Zones saved.') {
        save_status.textContent = '';
    }

    // Use Leaflet's toGeoJSON so the layer's actual geometry type survives
    // round-trip. The previous hand-assembly read _latlngs[0] only, which
    // silently converted MultiPolygon zones (loaded from zones.json or via
    // bulk-load) back into a single-ring Polygon — losing every ring
    // beyond the first.
    const features = [];
    editableLayers.eachLayer(layer => {
        const feature = layer.toGeoJSON();
        // toGeoJSON preserves the layer's original feature.properties when
        // present, but we re-write `name` to pick up any rename that hasn't
        // yet been flushed back onto the inner feature object.
        feature.properties = {name: layer.feature.properties.name};
        features.push(feature);
    });
    const geojson = {type: "FeatureCollection", features};

    const headers = {'Content-Type': 'application/json'};
    if (zones_etag) headers['If-Match'] = zones_etag;

    fetch('./save_zones', {
        method: 'POST',
        headers: headers,
        body: JSON.stringify(geojson)
    }).then(response => {
        let elem = document.querySelector('.save-btn');
        let status = document.getElementById('save-status');
        const new_etag = response.headers.get('ETag');
        if (new_etag) zones_etag = new_etag;

        if (response.status === 412) {
            elem.classList.remove('success')
            elem.classList.add('error')
            // Don't auto-clear — conflict needs explicit user attention.
            // Stay dirty: the user still hasn't flushed their edits.
            if (status) status.textContent =
                'Conflict: zones changed in another session. Reload to fetch the current version, then re-apply your edits.';
            return;
        }
        if (response.ok) {
            elem.classList.remove('error')
            elem.classList.add('success')
            if (status) status.textContent = 'Zones saved.';
            mark_clean();
            // Success auto-clears at 2s (same as before). #123 only changed
            // the error paths below — they used to auto-clear in 2s too,
            // which was too short for a user not looking at the button
            // (especially on mobile). Error state now persists until the
            // user's next save click, handled at the top of save_zones().
            setTimeout(() => {
                elem.classList.remove('success');
                if (status && status.textContent === 'Zones saved.') status.textContent = '';
            }, 2000);
        } else {
            elem.classList.remove('success')
            elem.classList.add('error')
            if (status) status.textContent = `Save failed (${response.status}).`;
            // No setTimeout: stay visible until the next save attempt.
        }
    }).catch(err => {
        let elem = document.querySelector('.save-btn');
        let status = document.getElementById('save-status');
        elem.classList.remove('success')
        elem.classList.add('error')
        if (status) status.textContent = `Save failed: ${err.message}`;
        // No setTimeout: an intermittent ingress failure flashing red for
        // 2s and disappearing left users unsure whether their zones saved.
        // The error stays until the next save click (#123).
    })
}


function load_bulk_json() {
    // open a file dialog
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
                alert('The selected file is not valid JSON.');
                return;
            }
            editableLayers.clearLayers();
            L.geoJSON(data, {
                style: () => ({color: window.ZONE_COLOUR}),
                onEachFeature: (feature, layer) => {
                    layer.bindPopup(name_popup_element(feature));
                    editableLayers.addLayer(layer);
                },
            })
            map.fitBounds(editableLayers.getBounds());
            render_zone_list();
            mark_dirty();
            if (editableLayers.getLayers().length > 0) {
                map.fitBounds(editableLayers.getBounds());
                map.setView(editableLayers.getBounds().getCenter(), 13);

                delete_load_btn();
            }
        };
        reader.readAsText(file);
    });
    input.click();
}