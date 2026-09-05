/*
 * Geometry helpers for the zone editor.
 *
 * These MEASURE; they do not PREDICT. Given a point and a ring they answer
 * "is this inside?" and "how far from the edge?" — they never rank zones or
 * claim which one the companion integration would report.
 *
 * That restraint is deliberate. An earlier prototype did rank, and disagreed
 * with the integration on live data: it matched a zone at 3.8 m against a 5 m
 * accuracy ring where the integration did not, because the integration
 * tie-breaks on distance-to-exterior and the prototype sorted by area. Two
 * implementations of the same rule drift, and the editor being confidently
 * wrong about a verdict is worse than it offering no verdict at all. A ruler
 * cannot contradict a verdict, because it makes none.
 *
 * All functions take rings as GeoJSON [lon, lat] pairs and return metres.
 * The projection is a local equirectangular approximation, which at property
 * scale (hundreds of metres) is accurate to well under the GPS noise it is
 * being compared against.
 */

const PZ_EARTH_RADIUS_M = 6371000;

function pz_to_radians(degrees) {
    return (degrees * Math.PI) / 180;
}

/* Metres-per-degree projection anchored at a reference latitude. Longitude
 * degrees shrink toward the poles, hence the cosine term. */
function pz_projector(referenceLat) {
    const lonScale = Math.cos(pz_to_radians(referenceLat));
    return function project(lon, lat) {
        return [
            pz_to_radians(lon) * PZ_EARTH_RADIUS_M * lonScale,
            pz_to_radians(lat) * PZ_EARTH_RADIUS_M,
        ];
    };
}

/* Area of a closed ring in square metres, via the shoelace formula. Returns 0
 * for anything too short to enclose area rather than throwing — a half-drawn
 * polygon is a normal transient state in an editor. */
function pz_ring_area_m2(ring) {
    if (!Array.isArray(ring) || ring.length < 4) return 0;
    const meanLat = ring.reduce((sum, p) => sum + p[1], 0) / ring.length;
    const project = pz_projector(meanLat);
    let doubled = 0;
    for (let i = 0; i < ring.length - 1; i++) {
        const [x1, y1] = project(ring[i][0], ring[i][1]);
        const [x2, y2] = project(ring[i + 1][0], ring[i + 1][1]);
        doubled += x1 * y2 - x2 * y1;
    }
    return Math.abs(doubled) / 2;
}

/* Ray-casting point-in-polygon. A point exactly on an edge is not guaranteed
 * either way — floating point decides — which is why callers should treat a
 * near-zero edge distance as "on the boundary" rather than trusting this alone. */
function pz_point_in_ring(lon, lat, ring) {
    if (!Array.isArray(ring) || ring.length < 4) return false;
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
        const xi = ring[i][0], yi = ring[i][1];
        const xj = ring[j][0], yj = ring[j][1];
        const straddles = (yi > lat) !== (yj > lat);
        if (straddles && lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi) {
            inside = !inside;
        }
    }
    return inside;
}

/* Shortest distance in metres from a point to a ring's boundary. Always
 * positive — combine with pz_point_in_ring to learn which side you are on. */
function pz_distance_to_ring_m(lon, lat, ring) {
    if (!Array.isArray(ring) || ring.length < 2) return Infinity;
    const project = pz_projector(lat);
    const [px, py] = project(lon, lat);
    let best = Infinity;
    for (let i = 0; i < ring.length - 1; i++) {
        const [ax, ay] = project(ring[i][0], ring[i][1]);
        const [bx, by] = project(ring[i + 1][0], ring[i + 1][1]);
        const dx = bx - ax;
        const dy = by - ay;
        let t = 0;
        if (dx !== 0 || dy !== 0) {
            t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy);
            t = Math.max(0, Math.min(1, t));
        }
        const cx = ax + t * dx;
        const cy = ay + t * dy;
        best = Math.min(best, Math.hypot(px - cx, py - cy));
    }
    return best;
}

/* How a point relates to one ring, as a plain measurement.
 *
 * `inside` and `edgeDistanceM` are facts about geometry. `withinAccuracy` says
 * only that the point's own error circle reaches the ring — it is NOT a claim
 * that the integration would match this zone, which depends on rules that live
 * there and are deliberately not reimplemented here. */
function pz_measure(lon, lat, ring, accuracyM) {
    const inside = pz_point_in_ring(lon, lat, ring);
    const edgeDistanceM = pz_distance_to_ring_m(lon, lat, ring);
    const acc = Number.isFinite(accuracyM) && accuracyM > 0 ? accuracyM : 0;
    return {
        inside,
        edgeDistanceM,
        withinAccuracy: inside || edgeDistanceM <= acc,
        areaM2: pz_ring_area_m2(ring),
    };
}

/* Node (tests / local checks) and browser both, without a module system. */
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        pz_ring_area_m2,
        pz_point_in_ring,
        pz_distance_to_ring_m,
        pz_measure,
    };
}
