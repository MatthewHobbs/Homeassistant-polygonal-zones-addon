/*
 * Live tracker overlay: plots /trackers.json and measures positions against the
 * drawn zones, so a misclassification is answerable by looking.
 *
 * It MEASURES, never PREDICTS: "inside" or "3.8 m outside", never which zone
 * the integration would report; a copy would drift. Silent without
 * `overlay_entities`.
 */

// index.html loads geometry.js and helpers.js before this file, so their
// globals exist in the browser. Under Node there is no such tag: attach them to
// globalThis — never `let`/`const`, which would shadow the browser's globals.
if (typeof module !== 'undefined' && module.exports) {
    Object.assign(globalThis, require('./geometry.js'), require('./helpers.js'));
}

const PZ_TRACKER_ENDPOINT = './trackers.json';
const PZ_HERE_SINCE_ENDPOINT = './trackers/here_since';
const PZ_HERE_SINCE_TIMEOUT_MS = 10000;
const PZ_UNKNOWN_RETRY_MS = 60000;

/* Re-measure at most this often while the user drags a vertex. The maths is
 * cheap, but rebuilding the readout DOM on every mousemove is not. */
const PZ_MEASURE_THROTTLE_MS = 150;

let pz_tracker_layer = null;
let pz_trackers = [];
let pz_measure_timer = null;
let pz_refresh_ms = 0;
let pz_refresh_timer = null;
let pz_refresh_in_flight = false;

/* True for a LatLng-shaped node — duck-typed on .lat/.lng rather than
 * `instanceof L.LatLng`, so this file (like geometry.js) can be exercised
 * with plain objects under Node, without loading Leaflet. */
function pz_is_latlng(node) {
    return !!node && typeof node.lat === 'number' && typeof node.lng === 'number';
}

/* Polygons of a layer as {outer, holes}, from live geometry. getLatLngs()
 * nests per GeoJSON level: Polygon is [ring, ...] (first outer, rest holes),
 * MultiPolygon is [[ring, ...], ...]. Rings after the first are holes of that
 * group; flattening would add area instead of subtracting it. */
function pz_layer_polygons(layer) {
    if (typeof layer.getLatLngs !== 'function') return [];
    const latlngs = layer.getLatLngs();
    if (!Array.isArray(latlngs) || !latlngs.length) return [];

    const toRing = (nodes) => {
        if (!Array.isArray(nodes) || nodes.length < 3) return null;
        const ring = nodes.map((p) => [p.lng, p.lat]);
        ring.push([ring[0][0], ring[0][1]]); // close it
        return ring;
    };

    // A Polygon's own top level is a list of rings; a MultiPolygon's is a
    // list of those lists. Tell them apart by what the first element holds.
    const groups = pz_is_latlng(latlngs[0]?.[0]) ? [latlngs] : latlngs;

    const polygons = [];
    for (const group of groups) {
        if (!Array.isArray(group)) continue;
        const rings = group.map(toRing).filter(Boolean);
        if (rings.length) polygons.push({ outer: rings[0], holes: rings.slice(1) });
    }
    return polygons;
}

/* Area of a zone in m²: each polygon's outer ring minus its own holes, summed
 * across a MultiPolygon. Here rather than geometry.js because it reads Leaflet
 * layers; geometry.js stays dependency-free so Node can run it. */
function pz_layer_area_m2(layer) {
    return pz_layer_polygons(layer).reduce((sum, { outer, holes }) => {
        const holesArea = holes.reduce((s, h) => s + pz_ring_area_m2(h), 0);
        return sum + Math.max(0, pz_ring_area_m2(outer) - holesArea);
    }, 0);
}

function pz_zone_name(layer) {
    return (layer.feature && layer.feature.properties && layer.feature.properties.name)
        || 'Unnamed zone';
}

/* Best (smallest) measurement across a multi-ring zone: a device is "in" a
 * MultiPolygon if it is in any of its parts, and "in" a part only if it is
 * inside that part's outer ring and outside every one of that part's holes. */
function pz_measure_zone(tracker, layer) {
    const polygons = pz_layer_polygons(layer);
    if (!polygons.length) return null;
    const { longitude: lon, latitude: lat, gps_accuracy: accuracyM } = tracker;
    let best = null;
    for (const { outer, holes } of polygons) {
        const insideOuter = pz_point_in_ring(lon, lat, outer);
        const insideAHole = holes.some((hole) => pz_point_in_ring(lon, lat, hole));
        const inside = insideOuter && !insideAHole;
        let edgeDistanceM = pz_distance_to_ring_m(lon, lat, outer);
        for (const hole of holes) {
            edgeDistanceM = Math.min(edgeDistanceM, pz_distance_to_ring_m(lon, lat, hole));
        }
        const acc = Number.isFinite(accuracyM) && accuracyM > 0 ? accuracyM : 0;
        const m = { inside, edgeDistanceM, withinAccuracy: inside || edgeDistanceM <= acc };
        // A device inside part 1 must stay "inside" even if part 2's boundary
        // is nearer, so only compare distances between two candidates that
        // agree on `inside`.
        if (!best || (m.inside && !best.inside) || (m.inside === best.inside && m.edgeDistanceM < best.edgeDistanceM)) {
            best = m;
        }
    }
    return best;
}

function pz_format_metres(value) {
    if (!Number.isFinite(value)) return '—';
    return value < 10 ? `${value.toFixed(1)} m` : `${Math.round(value)} m`;
}

function pz_render_readout() {
    const panel = document.querySelector('.tracker-readout');
    if (!panel) return;
    panel.innerHTML = '';
    if (!pz_trackers.length) {
        panel.hidden = true;
        return;
    }
    panel.hidden = false;

    const heading = document.createElement('h2');
    heading.textContent = 'Tracked devices';
    panel.appendChild(heading);

    pz_trackers.forEach((tracker) => {
        const card = document.createElement('div');
        card.className = 'tracker-card';

        const title = document.createElement('h3');
        title.textContent = tracker.name;
        card.appendChild(title);

        const meta = document.createElement('p');
        meta.className = 'tracker-meta';
        meta.textContent = tracker.gps_accuracy === null
            ? `${tracker.state} · no accuracy reported`
            : `${tracker.state} · ±${pz_format_metres(tracker.gps_accuracy)}`;
        card.appendChild(meta);

        const list = document.createElement('ul');
        list.className = 'tracker-zones';
        let any = false;
        editableLayers.eachLayer((layer) => {
            const m = pz_measure_zone(tracker, layer);
            if (!m) return;
            // Only zones worth mentioning: the ones it is in, or near enough
            // that a small drag would change the answer.
            const notable = m.inside || m.edgeDistanceM <= Math.max(50, (tracker.gps_accuracy || 0) * 2);
            if (!notable) return;
            any = true;
            const li = document.createElement('li');
            li.className = m.inside ? 'is-inside' : 'is-outside';
            const where = m.inside
                ? `inside, ${pz_format_metres(m.edgeDistanceM)} from the edge`
                : `${pz_format_metres(m.edgeDistanceM)} outside`;
            li.textContent = `${pz_zone_name(layer)} — ${where}`;
            if (!m.inside && m.withinAccuracy) {
                // Its error circle reaches the zone. Whether that counts is the
                // integration's call, not ours — say what we measured, not what
                // it will decide.
                li.textContent += ' (within its accuracy)';
            }
            list.appendChild(li);
        });
        if (!any) {
            const li = document.createElement('li');
            li.className = 'is-outside';
            li.textContent = 'Not in or near any zone';
            list.appendChild(li);
        }
        card.appendChild(list);
        panel.appendChild(card);
    });

    const note = document.createElement('p');
    note.className = 'tracker-note';
    note.textContent =
        'Distances are measured by this editor. Which zone the integration '
        + 'reports is decided by the integration, not shown here.';
    panel.appendChild(note);
}

/* "over 24 h", why it is unknown, or the arrival time (with the date when not today). */
function pz_here_since_text(body, now = new Date()) {
    if (body && body.over_window) return 'over 24 h';
    const since = new Date(body && typeof body.since === 'string' ? body.since : NaN);
    if (Number.isNaN(since.getTime())) {
        /* A reason the server gave for having no time, so "no history" is not
         * shown for a mistyped entity or a failed read. */
        const reasons = {
            busy: 'too many lookups, try again',
            lookup_failed: 'history unavailable',
            no_history: 'no history',
            no_coordinates: 'no positions recorded',
        };
        return (body && reasons[body.reason]) || 'unknown';
    }
    const time = since.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    return since.toDateString() === now.toDateString() ? time : `${since.toLocaleDateString()} ${time}`;
}

/* Resolves to the lookup's body, or null on any failure. AbortController +
 * setTimeout rather than AbortSignal.timeout, which Safari/iOS before 16 lack. */
function pz_fetch_here_since(entity_id) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), PZ_HERE_SINCE_TIMEOUT_MS);
    return fetch(`${PZ_HERE_SINCE_ENDPOINT}?entity_id=${encodeURIComponent(entity_id)}`, {
        headers: { Accept: 'application/json' },
        signal: controller.signal,
    })
        .then((r) => (r.ok ? r.json() : null))
        .catch(() => null)
        .finally(() => clearTimeout(timer));
}

/* The last lookup per tracker and the position it was made at. A refresh
 * reopens an open popup; reusing this while the tracker hasn't moved keeps
 * that from re-reading a day of history on every poll. */
const pz_here_since_cache = new Map();
let pz_reopening = false;

function pz_here_since_for(tracker) {
    const at = `${tracker.latitude},${tracker.longitude}`;
    const hit = pz_here_since_cache.get(tracker.entity_id);
    // An answer is reused while the tracker is unmoved, one still in flight
    // too; an unknown one only for a minute, so a later refresh can correct it.
    const fresh = hit && (!hit.empty || Date.now() - hit.done < PZ_UNKNOWN_RETRY_MS);
    if (pz_reopening && hit && hit.at === at && fresh) return hit.lookup;
    const entry = { at, empty: false, done: 0 };
    entry.lookup = pz_fetch_here_since(tracker.entity_id).then((body) => {
        entry.empty = !(body && (body.since || body.over_window));
        entry.done = Date.now();
        return body;
    });
    pz_here_since_cache.set(tracker.entity_id, entry);
    return entry.lookup;
}

/* Built from text nodes: names come from Home Assistant. */
function pz_tracker_popup(tracker) {
    const box = document.createElement('div');
    const line = (tag, text) => {
        const el = box.appendChild(document.createElement(tag));
        el.textContent = text;
        return el;
    };
    line('strong', tracker.name);
    if (Number.isFinite(tracker.gps_accuracy)) line('div', `±${pz_format_metres(tracker.gps_accuracy)}`);
    if (Number.isFinite(tracker.battery_level)) line('div', `Battery ${tracker.battery_level}%`);
    const since = line('div', 'Here since checking…');
    pz_here_since_for(tracker).then((body) => {
        since.textContent = `Here since ${pz_here_since_text(body)}`;
    });
    return box;
}

function pz_render_markers() {
    if (!pz_tracker_layer) return;
    // Each poll redraws the dots; reopen the popup the user had open.
    let open_id = null;
    pz_tracker_layer.eachLayer((l) => { if (l.isPopupOpen?.()) open_id = l.pz_entity_id; });
    pz_tracker_layer.clearLayers();
    // pmIgnore: Geoman would otherwise let delete mode remove a dot and snap
    // drawn vertices to it.
    pz_trackers.forEach((tracker) => {
        const latlng = [tracker.latitude, tracker.longitude];
        if (Number.isFinite(tracker.gps_accuracy) && tracker.gps_accuracy > 0) {
            L.circle(latlng, {
                radius: tracker.gps_accuracy,
                className: 'tracker-accuracy',
                interactive: false,
                pmIgnore: true,
            }).addTo(pz_tracker_layer);
        }
        const marker = L.circleMarker(latlng, { radius: 6, className: 'tracker-marker', pmIgnore: true })
            .bindTooltip(text_element(tracker.name), { permanent: false })
            // Leaflet calls this on each open, so every click looks afresh.
            // A reopen must not pan the map the user may have moved.
            .bindPopup(() => pz_tracker_popup(tracker), { autoPan: tracker.entity_id !== open_id })
            .addTo(pz_tracker_layer);
        marker.pz_entity_id = tracker.entity_id;
        if (tracker.entity_id === open_id) {
            pz_reopening = true;
            try {
                marker.openPopup();
            } finally {
                pz_reopening = false;
            }
        }
    });
}

/* Re-measure after the shapes change. Throttled: dragging a vertex fires
 * continuously and the DOM rebuild is the expensive half. */
function pz_schedule_measure() {
    if (pz_measure_timer) return;
    pz_measure_timer = setTimeout(() => {
        pz_measure_timer = null;
        pz_render_readout();
    }, PZ_MEASURE_THROTTLE_MS);
}

function pz_fetch_trackers() {
    return fetch(PZ_TRACKER_ENDPOINT, { headers: { Accept: 'application/json' } })
        .then((r) => (r.ok ? r.json() : null));
}

/* A tracker the add-on could not read this poll keeps its last position; one
 * that Home Assistant answered for but without coordinates is dropped, since
 * that is real data saying there is no position. */
function pz_apply_trackers(body) {
    const fresh = Array.isArray(body.trackers) ? body.trackers : [];
    const unavailable = new Set(Array.isArray(body.unavailable) ? body.unavailable : []);
    const kept = pz_trackers.filter((t) => unavailable.has(t.entity_id));
    pz_trackers = fresh.concat(kept);
    pz_render_markers();
    pz_render_readout();
}

/* One poll at a time: the next is scheduled only once this one settles, so a
 * slow Home Assistant cannot pile requests up. A hidden tab stops polling and
 * the visibilitychange handler catches it up on return. */
function pz_refresh_trackers() {
    clearTimeout(pz_refresh_timer);
    pz_refresh_timer = null;
    if (document.hidden || pz_refresh_in_flight) return;
    pz_refresh_in_flight = true;
    pz_fetch_trackers()
        .then((body) => {
            // A failed or refused poll keeps the last positions on the map
            // rather than blanking it.
            if (body && body.configured && !body.error) pz_apply_trackers(body);
        })
        .catch((err) => {
            console.warn('Tracker overlay refresh failed:', err);
        })
        .finally(() => {
            pz_refresh_in_flight = false;
            if (!document.hidden) pz_refresh_timer = setTimeout(pz_refresh_trackers, pz_refresh_ms);
        });
}

function pz_start_tracker_refresh(seconds) {
    if (!Number.isFinite(seconds) || seconds <= 0) return;
    pz_refresh_ms = seconds * 1000;
    pz_refresh_timer = setTimeout(pz_refresh_trackers, pz_refresh_ms);
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden && !pz_refresh_timer) pz_refresh_trackers();
    });
}

function setup_tracker_overlay(mapInstance) {
    return pz_fetch_trackers()
        .then((body) => {
            // Not opted in (the default), or the endpoint refused us: no overlay.
            if (!body || !body.configured) return;
            if (body.error === 'no_supervisor_token') {
                console.warn(
                    'Tracker overlay is configured but the add-on has no Supervisor token — '
                    + 'check that homeassistant_api is enabled.',
                );
                return;
            }
            // Created even when nothing has a position yet, so a tracker that
            // reports later still has a layer to be drawn on.
            pz_tracker_layer = L.layerGroup().addTo(mapInstance);
            pz_apply_trackers(body);

            // map.js fires this after every draw, delete, load and vertex drag.
            mapInstance.on('pz:zoneschanged', pz_schedule_measure);
            pz_start_tracker_refresh(body.refresh_seconds);
        })
        .catch((err) => {
            // A missing overlay must never break the editor itself.
            console.warn('Tracker overlay unavailable:', err);
        });
}

if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        pz_layer_polygons, pz_layer_area_m2, pz_measure_zone, pz_format_metres, setup_tracker_overlay,
        pz_tracker_popup, pz_here_since_text, pz_apply_trackers,
    };
}
