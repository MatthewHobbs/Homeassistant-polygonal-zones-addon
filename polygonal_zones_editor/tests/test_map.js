// Node tests for map.js's zone loading. Run with `node --test tests/test_map.js`
// from polygonal_zones_editor/.
//
// map.js is a browser script with no exports, so it runs here in a vm context,
// as a <script> tag would, against stand-ins for the DOM, Leaflet and
// FileReader. The Leaflet stand-in throws where Leaflet does: L.geoJSON on
// input that is not GeoJSON, and fitBounds on empty bounds. What it cannot
// show is real Leaflet rendering; the browser smoke covers that.

const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const MAP_JS = fs.readFileSync(
    path.join(__dirname, '..', 'app', 'static', 'js', 'map.js'), 'utf8'
);

function fakeElement(tag) {
    const listeners = {};
    return {
        tagName: tag.toUpperCase(),
        attributes: {},
        children: [],
        innerHTML: '',
        textContent: '',
        setAttribute(k, v) { this.attributes[k] = v; },
        removeAttribute(k) { delete this.attributes[k]; },
        appendChild(child) { this.children.push(child); return child; },
        addEventListener(evt, fn) { (listeners[evt] = listeners[evt] || []).push(fn); },
        click() { (listeners.change || []).forEach((fn) => fn()); },
        remove() {},
    };
}

function bounds(layers) {
    return {
        isValid: () => layers.length > 0,
        getCenter() {
            if (!layers.length) throw new Error('empty bounds have no centre');
            return { lat: 1, lng: 2 };
        },
    };
}

class Polygon {
    constructor(feature) { this.feature = feature; }
    bindPopup() { return this; }
    toGeoJSON() { return { type: 'Feature', geometry: this.feature.geometry }; }
}

class Marker {
    constructor(feature) { this.feature = feature; }
    bindPopup() { return this; }
}

// Leaflet 1.9's GeoJSON.addData, reduced to what decides layer or throw.
function geoJSON(data, options) {
    function add(obj) {
        const features = Array.isArray(obj) ? obj : obj.features;
        if (features) {
            features.forEach((f) => {
                if (f.geometries || f.geometry || f.features || f.coordinates) add(f);
            });
            return;
        }
        const geometry = obj.type === 'Feature' ? obj.geometry : obj;
        const feature = obj.type === 'Feature' ? obj : { type: 'Feature', properties: {}, geometry: obj };
        let layer;
        switch (geometry.type) {
            case 'Polygon':
            case 'MultiPolygon':
                layer = new Polygon(feature);
                break;
            case 'Point':
                layer = new Marker(feature);
                break;
            default:
                throw new Error('Invalid GeoJSON object.');
        }
        options.onEachFeature(feature, layer);
    }
    if (data) add(data);
}

function loadMap() {
    const errors = [];
    const calls = [];
    const elements = { '.zone-list': fakeElement('div') };
    let pendingFile = null;

    const layers = [];
    const editableLayers = {
        addLayer(l) { layers.push(l); },
        clearLayers() { calls.push('clearLayers'); layers.length = 0; },
        getLayers: () => layers.slice(),
        eachLayer(fn) { layers.slice().forEach(fn); },
        getBounds: () => bounds(layers),
    };
    const map = {
        fitBounds(b) {
            if (!b.isValid()) throw new Error('Bounds are not valid.');
            calls.push('fitBounds');
        },
        setView(center, zoom) { calls.push(['setView', zoom]); },
        fire() {},
    };

    const document = {
        querySelector: (sel) => elements[sel] || null,
        createElement(tag) {
            const el = fakeElement(tag);
            if (tag === 'input') el.files = [pendingFile];
            return el;
        },
        body: {
            appendChild(el) {
                if (el.className === 'transient-error') errors.push(el.textContent);
            },
        },
    };

    const context = vm.createContext({
        window: { addEventListener() {}, ZONE_COLOUR: 'green' },
        document,
        fetch: () => new Promise(() => {}),
        setTimeout: () => 0,
        console,
        L: { geoJSON, Polygon },
        FileReader: class {
            readAsText(file) { this.onload({ target: { result: file } }); }
        },
        name_popup_element: () => ({}),
        delete_load_btn: () => calls.push('delete_load_btn'),
        create_load_btn: () => calls.push('create_load_btn'),
    });
    vm.runInContext(MAP_JS, context);
    vm.runInContext('(m, g) => { map = m; editableLayers = g; }', context)(map, editableLayers);

    return {
        layers,
        errors,
        calls,
        isDirty: () => vm.runInContext('isDirty', context),
        bulkLoad(text) {
            pendingFile = text;
            vm.runInContext('load_bulk_json', context)();
        },
    };
}

const polygon = (name) => ({
    type: 'Feature',
    properties: { name },
    geometry: { type: 'Polygon', coordinates: [[[0, 0], [1, 0], [1, 1], [0, 0]]] },
});
const collection = (...features) => JSON.stringify({ type: 'FeatureCollection', features });

function withExistingZone() {
    const m = loadMap();
    m.layers.push(new Polygon(polygon('Home')));
    return m;
}

test('bulk load of an empty collection says so and keeps the current zones', () => {
    const m = withExistingZone();
    m.bulkLoad(collection());
    assert.equal(m.errors.length, 1);
    assert.deepEqual(m.layers.map((l) => l.feature.properties.name), ['Home']);
    assert.equal(m.isDirty(), false);
});

test('bulk load of JSON that is not GeoJSON says so before clearing anything', () => {
    const m = withExistingZone();
    m.bulkLoad(JSON.stringify({ hello: 'world' }));
    assert.equal(m.errors.length, 1);
    assert.ok(!m.calls.includes('clearLayers'), 'zones cleared before the file was checked');
    assert.deepEqual(m.layers.map((l) => l.feature.properties.name), ['Home']);
});

test('bulk load of GeoJSON that is not polygons is refused', () => {
    const m = withExistingZone();
    m.bulkLoad(collection({
        type: 'Feature', properties: { name: 'Pin' }, geometry: { type: 'Point', coordinates: [0, 0] },
    }));
    assert.equal(m.errors.length, 1);
    assert.deepEqual(m.layers.map((l) => l.feature.properties.name), ['Home']);
});

test('bulk load of polygons replaces the zones and centres on them at zoom 13', () => {
    const m = withExistingZone();
    m.bulkLoad(collection(polygon('A'), polygon('B')));
    assert.deepEqual(m.errors, []);
    assert.deepEqual(m.layers.map((l) => l.feature.properties.name), ['A', 'B']);
    assert.deepEqual(m.calls.filter((c) => Array.isArray(c)), [['setView', 13]]);
    assert.ok(!m.calls.includes('fitBounds'), 'setView overrides fitBounds; it is not needed');
    assert.ok(m.calls.includes('delete_load_btn'));
    assert.equal(m.isDirty(), true);
});

test('bulk load of a file that is not JSON says so', () => {
    const m = withExistingZone();
    m.bulkLoad('{not json');
    assert.deepEqual(m.errors, ['The selected file is not valid JSON.']);
    assert.equal(m.layers.length, 1);
});
