/*
 * Live tracker overlay.
 *
 * Plots the positions the add-on serves from /trackers.json over the map, and
 * measures each one against the zones currently drawn — so "why is this device
 * not in that zone?" is answerable by looking rather than by reading the
 * integration's debug log.
 *
 * Two deliberate limits:
 *
 *  - It MEASURES, it does not PREDICT. The readout says "inside" or "3.8 m
 *    outside"; it never says which zone the integration would report. Those
 *    rules live in the integration and reimplementing them here would create a
 *    second source of truth that drifts (see js/geometry.js for the incident
 *    that motivated this).
 *  - It is silent unless the user opts in. With `overlay_entities` unset the
 *    endpoint reports `configured: false` and this module renders nothing at
 *    all — no panel, no error, no marker.
 */

// In the browser, index.html loads geometry.js before this file as separate
// <script> tags, so pz_ring_area_m2 & co. are already real globals by the
// time this runs. Under Node (tests) there is no such tag, so pull them onto
// globalThis here — never with `let`/`const`, which would shadow the
// browser's own globals for every reference below instead of only adding a
// fallback for Node.
if (typeof module !== 'undefined' && module.exports) {
    Object.assign(globalThis, require('./geometry.js'));
}

const PZ_TRACKER_ENDPOINT = './trackers.json';

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

/* Every polygon of a layer, grouped as {outer, holes} — read from the LIVE
 * Leaflet geometry rather than layer.feature, so measurements track the
 * shape under the user's cursor, not the shape as last saved.
 *
 * layer.getLatLngs() nests one level deeper per GeoJSON level: a Polygon is
 * [ring, ring, ...] (first ring outer, the rest holes); a MultiPolygon is
 * [[ring, ...], [ring, ...], ...], one such group per part. Either way, the
 * first ring in a group is that group's outer boundary and every ring after
 * it is a hole *of that same group* — never of any other. Previously every
 * ring was flattened into one list with no such grouping, so a hole added
 * to a zone's area instead of subtracting, and a point inside a hole read as
 * inside the zone (ADR 0002 row 12). */
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

/* Total area of a zone in square metres: each polygon's outer ring, minus
 * its own holes, summed across every polygon (a MultiPolygon has more than
 * one). Lives here rather than in geometry.js because it reads Leaflet
 * layers; geometry.js is kept free of any dependency so it can be
 * exercised without a browser. */
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
        if (!best || (m.inside && !best.inside) || m.edgeDistanceM < best.edgeDistanceM) {
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

function pz_render_markers() {
    if (!pz_tracker_layer) return;
    pz_tracker_layer.clearLayers();
    pz_trackers.forEach((tracker) => {
        const latlng = [tracker.latitude, tracker.longitude];
        if (Number.isFinite(tracker.gps_accuracy) && tracker.gps_accuracy > 0) {
            L.circle(latlng, {
                radius: tracker.gps_accuracy,
                className: 'tracker-accuracy',
                interactive: false,
            }).addTo(pz_tracker_layer);
        }
        L.circleMarker(latlng, { radius: 6, className: 'tracker-marker' })
            .bindTooltip(tracker.name, { permanent: false })
            .addTo(pz_tracker_layer);
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
            // Not opted in (the default), or the endpoint refused us. Either
            // way the editor behaves exactly as it did before this feature.
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

            ['pz:zoneschanged', 'pm:create', 'pm:remove', 'pm:edit', 'pm:update']
                .forEach((evt) => mapInstance.on(evt, pz_schedule_measure));
            pz_start_tracker_refresh(body.refresh_seconds);
        })
        .catch((err) => {
            // A missing overlay must never break the editor itself.
            console.warn('Tracker overlay unavailable:', err);
        });
}

if (typeof module !== 'undefined' && module.exports) {
    module.exports = { pz_layer_polygons, pz_layer_area_m2, pz_measure_zone, pz_format_metres };
}
