// Node tests for trackers.js and the geometry.js it measures with. Run
// with `node --test tests/test_trackers.js` from polygonal_zones_editor/
// (also run in CI by test.yml).
//
// trackers.js reads Leaflet layers via layer.getLatLngs(), so these tests
// build minimal fake layers with that one method rather than loading real
// Leaflet (which expects a browser/DOM at load time).

const assert = require('node:assert/strict');
const { test } = require('node:test');
const path = require('node:path');
const { pz_layer_polygons, pz_layer_area_m2, pz_measure_zone } = require(
    path.join(__dirname, '..', 'app', 'static', 'js', 'trackers.js')
);

function latlng(lng, lat) {
    return { lng, lat };
}

// A ring's positions as fake LatLngs, open (no repeated first point) —
// matching what Leaflet's own getLatLngs() returns for a live layer.
function ringLatLngs(positions) {
    return positions.map(([lng, lat]) => latlng(lng, lat));
}

// 1km square outer ring (roughly, at the equator: ~0.009 degrees per km).
const OUTER = [
    [0, 0],
    [0.009, 0],
    [0.009, 0.009],
    [0, 0.009],
];
// A 100m-ish square hole in the middle of that square.
const HOLE = [
    [0.004, 0.004],
    [0.005, 0.004],
    [0.005, 0.005],
    [0.004, 0.005],
];

function fakePolygonLayer(rings) {
    // Leaflet: a Polygon's getLatLngs() is [outerRing, hole1, ...].
    return { getLatLngs: () => rings.map(ringLatLngs) };
}

function fakeMultiPolygonLayer(polygons) {
    // Leaflet: a MultiPolygon's getLatLngs() is [[outer, holes...], ...].
    return { getLatLngs: () => polygons.map((rings) => rings.map(ringLatLngs)) };
}

test('pz_layer_polygons groups a hole with its own outer ring, not as a sibling', () => {
    const layer = fakePolygonLayer([OUTER, HOLE]);
    const polygons = pz_layer_polygons(layer);
    assert.equal(polygons.length, 1);
    assert.equal(polygons[0].holes.length, 1);
});

test('area subtracts the hole instead of adding it', () => {
    const withoutHole = pz_layer_area_m2(fakePolygonLayer([OUTER]));
    const withHole = pz_layer_area_m2(fakePolygonLayer([OUTER, HOLE]));
    assert.ok(withHole < withoutHole, `hole must reduce area: ${withHole} vs ${withoutHole}`);
    assert.ok(withHole > 0, 'square minus a smaller square must still have positive area');
});

test('a point inside the hole is not inside the zone', () => {
    const layer = fakePolygonLayer([OUTER, HOLE]);
    const tracker = { longitude: 0.0045, latitude: 0.0045, gps_accuracy: 0 };
    const m = pz_measure_zone(tracker, layer);
    assert.equal(m.inside, false, 'a point in the donut hole must read as outside');
});

test('a point inside the outer ring but outside the hole is inside the zone', () => {
    const layer = fakePolygonLayer([OUTER, HOLE]);
    const tracker = { longitude: 0.001, latitude: 0.001, gps_accuracy: 0 };
    const m = pz_measure_zone(tracker, layer);
    assert.equal(m.inside, true);
});

test('a point outside everything is outside, even near the hole', () => {
    const layer = fakePolygonLayer([OUTER, HOLE]);
    const tracker = { longitude: 0.02, latitude: 0.02, gps_accuracy: 0 };
    const m = pz_measure_zone(tracker, layer);
    assert.equal(m.inside, false);
});

test('MultiPolygon: a hole in one part never subtracts from, or leaks into, another part', () => {
    const part2 = [
        [1, 1],
        [1.009, 1],
        [1.009, 1.009],
        [1, 1.009],
    ];
    const layer = fakeMultiPolygonLayer([[OUTER, HOLE], [part2]]);
    const polygons = pz_layer_polygons(layer);
    assert.equal(polygons.length, 2);
    assert.equal(polygons[0].holes.length, 1);
    assert.equal(polygons[1].holes.length, 0);

    const areaWithHole = pz_layer_area_m2(fakePolygonLayer([OUTER, HOLE]));
    const areaPart2 = pz_layer_area_m2(fakePolygonLayer([part2]));
    const totalArea = pz_layer_area_m2(layer);
    assert.ok(
        Math.abs(totalArea - (areaWithHole + areaPart2)) < 1e-6,
        'MultiPolygon area is the sum of each part, each already hole-adjusted'
    );
});

test('MultiPolygon: a point inside one part stays inside even when another part is nearer', () => {
    // Regression test: comparing edgeDistanceM regardless of `inside` let a
    // closer-but-outside second part overturn an already-inside result from
    // the first. The point below sits deep
    // inside part 1 (OUTER, ~500m from its own edges) but just ~11m outside
    // part 2's edge — under the bug, part 2's smaller edgeDistanceM won the
    // comparison and overturned the correct "inside" result from part 1.
    const part2 = [
        [0.0045, 0.0045],
        [0.02, 0.0045],
        [0.02, 0.02],
        [0.0045, 0.02],
    ];
    const layer = fakeMultiPolygonLayer([[OUTER], [part2]]);
    const tracker = { longitude: 0.0044, latitude: 0.0044, gps_accuracy: 0 };
    const m = pz_measure_zone(tracker, layer);
    assert.equal(m.inside, true, 'inside part 1 must not be overturned by a nearer part 2 edge');
});

// Values, not just directions. 0.001° of latitude, or of longitude at the
// equator, is 111.19 m on geometry.js's 6,371 km sphere, so this square is
// 12,364 m². Every test below fails if that radius is wrong by any factor.
const { pz_ring_area_m2, pz_distance_to_ring_m } = require(
    path.join(__dirname, '..', 'app', 'static', 'js', 'geometry.js')
);
const SQUARE = [[0, 0], [0.001, 0], [0.001, 0.001], [0, 0.001]];
const SQUARE_M2 = 12364;
const QUARTER_HOLE = [[0.00025, 0.00025], [0.00075, 0.00025], [0.00075, 0.00075], [0.00025, 0.00075]];
const EDGE_GAP_M = 11.12; // 0.0001°

function assertNear(actual, expected, what) {
    assert.ok(
        Math.abs(actual - expected) <= expected * 0.01,
        `${what}: ${actual} is not within 1% of ${expected}`
    );
}

test('a 0.001° square at the equator measures 12,364 m²', () => {
    assertNear(pz_ring_area_m2([...SQUARE, SQUARE[0]]), SQUARE_M2, 'square');
    assertNear(pz_layer_area_m2(fakePolygonLayer([SQUARE])), SQUARE_M2, 'square as a layer');
});

test('a hole half the side across takes a quarter of the area off', () => {
    assertNear(
        pz_layer_area_m2(fakePolygonLayer([SQUARE, QUARTER_HOLE])), SQUARE_M2 * 0.75, 'square with hole'
    );
});

test('edge distance of a point 0.0001° inside an edge is 11.1 m', () => {
    assertNear(pz_distance_to_ring_m(0.0005, 0.0001, [...SQUARE, SQUARE[0]]), EDGE_GAP_M, 'ring');
    const m = pz_measure_zone({ longitude: 0.0005, latitude: 0.0001, gps_accuracy: 0 },
        fakePolygonLayer([SQUARE]));
    assert.equal(m.inside, true);
    assertNear(m.edgeDistanceM, EDGE_GAP_M, 'zone');
});

function fakeNode(tag) {
    return {
        tagName: tag.toUpperCase(),
        children: [],
        textContent: '',
        className: '',
        hidden: false,
        set innerHTML(_v) { this.children = []; },
        appendChild(child) { this.children.push(child); return child; },
    };
}

test('a non-zero gps_accuracy reaches the readout, measured against the zone', async () => {
    const { setup_tracker_overlay } = require(
        path.join(__dirname, '..', 'app', 'static', 'js', 'trackers.js')
    );
    const panel = fakeNode('div');
    const zone = fakePolygonLayer([SQUARE]);
    zone.feature = { properties: { name: 'Yard' } };
    const saved = {
        fetch: globalThis.fetch, L: globalThis.L, document: globalThis.document,
        editableLayers: globalThis.editableLayers,
    };
    const layer = () => ({ addTo() { return this; }, clearLayers() {}, bindTooltip() { return this; } });
    globalThis.document = {
        createElement: fakeNode,
        querySelector: (sel) => (sel === '.tracker-readout' ? panel : null),
    };
    globalThis.L = { layerGroup: layer, circle: layer, circleMarker: layer };
    globalThis.editableLayers = { eachLayer: (fn) => fn(zone) };
    // 0.0001° south of the square's bottom edge, with a 15 m error circle.
    globalThis.fetch = async () => ({
        ok: true,
        json: async () => ({
            configured: true,
            trackers: [{
                entity_id: 'device_tracker.car', name: 'Car', state: 'not_home',
                latitude: -0.0001, longitude: 0.0005, gps_accuracy: 15,
            }],
            unavailable: [],
        }),
    });
    try {
        await setup_tracker_overlay({ on() {} });
    } finally {
        Object.assign(globalThis, saved);
    }

    const card = panel.children.find((c) => c.className === 'tracker-card');
    const meta = card.children.find((c) => c.className === 'tracker-meta');
    assert.equal(meta.textContent, 'not_home · ±15 m');
    const zones = card.children.find((c) => c.className === 'tracker-zones');
    assert.deepEqual(zones.children.map((li) => li.textContent),
        ['Yard — 11 m outside (within its accuracy)']);
});

// Leaflet renders a string tooltip as HTML, so the tooltip must be a node
// whose text is the name, never the name string itself. Drives the real
// overlay setup with minimal stand-ins for fetch, Leaflet and the DOM.
test('tracker tooltip carries the name as text, not markup', async () => {
    const { setup_tracker_overlay } = require(
        path.join(__dirname, '..', 'app', 'static', 'js', 'trackers.js')
    );
    const name = '<b>Car</b>';
    const created = [];
    const tooltips = [];
    const saved = {
        fetch: globalThis.fetch, L: globalThis.L, document: globalThis.document,
    };

    function fakeElement(tag) {
        const el = { tagName: tag.toUpperCase(), markupAssigned: false, _text: '' };
        Object.defineProperty(el, 'textContent', {
            get() { return this._text; },
            set(v) { this._text = String(v); },
        });
        Object.defineProperty(el, 'innerHTML', {
            get() { return this._text; },
            set() { this.markupAssigned = true; },
        });
        created.push(el);
        return el;
    }
    const layer = () => ({ addTo() { return this; }, clearLayers() {} });

    globalThis.document = { createElement: fakeElement, querySelector: () => null };
    globalThis.L = {
        layerGroup: layer,
        circle: layer,
        circleMarker: () => ({
            bindTooltip(content) { tooltips.push(content); return this; },
            addTo() { return this; },
        }),
    };
    globalThis.fetch = async () => ({
        ok: true,
        json: async () => ({
            configured: true,
            trackers: [{
                entity_id: 'device_tracker.car', name, state: 'home',
                latitude: 1, longitude: 2, gps_accuracy: null,
            }],
            unavailable: [],
        }),
    });
    try {
        await setup_tracker_overlay({ on() {} });
    } finally {
        Object.assign(globalThis, saved);
    }

    assert.equal(tooltips.length, 1);
    const content = tooltips[0];
    assert.notEqual(typeof content, 'string', 'a string tooltip is parsed as HTML');
    assert.ok(created.includes(content), 'tooltip must be a created DOM node');
    assert.equal(content.textContent, name);
    assert.equal(content.markupAssigned, false);
});

test('Geoman ignores the tracker dots, so delete mode and snapping skip them', async () => {
    const { setup_tracker_overlay } = require(
        path.join(__dirname, '..', 'app', 'static', 'js', 'trackers.js')
    );
    const options = [];
    const saved = { fetch: globalThis.fetch, L: globalThis.L, document: globalThis.document };
    const layer = (_latlng, opts) => {
        if (opts) options.push(opts);
        return { addTo() { return this; }, clearLayers() {}, bindTooltip() { return this; } };
    };
    globalThis.document = { createElement: () => ({}), querySelector: () => null };
    globalThis.L = { layerGroup: layer, circle: layer, circleMarker: layer };
    globalThis.fetch = async () => ({
        ok: true,
        json: async () => ({
            configured: true,
            trackers: [{
                entity_id: 'device_tracker.car', name: 'Car', state: 'home',
                latitude: 1, longitude: 2, gps_accuracy: 15,
            }],
            unavailable: [],
        }),
    });
    try {
        await setup_tracker_overlay({ on() {} });
    } finally {
        Object.assign(globalThis, saved);
    }
    assert.equal(options.length, 2, 'an accuracy circle and a dot');
    assert.ok(options.every((o) => o.pmIgnore === true));
});
