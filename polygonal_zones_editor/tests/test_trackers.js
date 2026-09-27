// Node tests for trackers.js's polygon-with-holes measurement (ADR 0002
// row 12). Run with `node --test tests/test_trackers.js` from
// polygonal_zones_editor/ (also wired into CI — see lint.yml).
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
    // Sanity: previously (flattening rings) a hole was summed in, so this
    // specific regression — area going *up* with a hole — is exactly what
    // this test would have missed if it only checked "some area value".
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
    // Found by adversarial review, not by hand: comparing edgeDistanceM
    // regardless of `inside` let a closer-but-outside second part overturn
    // an already-inside result from the first. The point below sits deep
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
