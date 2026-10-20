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
        get innerHTML() { return ''; },
        set innerHTML(_v) { this.children = []; },
        textContent: '',
        listeners,
        // Like the DOM: attribute values are stored as strings.
        setAttribute(k, v) { this.attributes[k] = String(v); },
        getAttribute(k) { return k in this.attributes ? this.attributes[k] : null; },
        classes: new Set(),
        get classList() {
            const s = this.classes;
            return {
                add: (c) => s.add(c),
                remove: (...cs) => cs.forEach((c) => s.delete(c)),
                contains: (c) => s.has(c),
            };
        },
        removeAttribute(k) { delete this.attributes[k]; },
        appendChild(child) { this.children.push(child); return child; },
        addEventListener(evt, fn) { (listeners[evt] = listeners[evt] || []).push(fn); },
        click() { (listeners.change || []).forEach((fn) => fn()); },
        remove() {},
    };
}

// zone-entry's contract with map.js: its Edit/Done button sends an `edit`
// event carrying the name typed in its box, then flips its `editing`
// attribute. `typed` stands in for that box.
function fakeZoneEntry() {
    const el = fakeElement('zone-entry');
    el.typed = null;
    el.edit_event_handler = function () {
        const editing = this.attributes.editing === 'true';
        const detail = {
            editing: !editing,
            name: this.typed ?? this.attributes.name,
            id: this.attributes['zone-id'] || null,
        };
        (this.listeners.edit || []).forEach((fn) => fn({ detail, target: this }));
        this.setAttribute('editing', String(!editing));
    };
    return el;
}

// Leaflet's Evented, reduced to on/fire.
function evented(target) {
    const handlers = {};
    target.on = (evt, fn) => { (handlers[evt] = handlers[evt] || []).push(fn); };
    target.fire = (evt, e = {}) => { (handlers[evt] || []).forEach((fn) => fn(e)); };
    return target;
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
    constructor(feature) {
        this.feature = feature;
        this.pm = { enabled: false, enable() { this.enabled = true; }, disable() { this.enabled = false; } };
    }
    bindPopup(content) { this.popup = content; return this; }
    getBounds() { return bounds([this]); }
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

function loadMap({ noAbortTimeout = false } = {}) {
    const errors = [];
    const calls = [];
    const elements = { '.zone-list': fakeElement('div'), '.save-btn': fakeElement('button') };
    const status = fakeElement('p');
    const posts = [];
    let reply = { status: 200, etag: null };
    const timers = [];
    const cleared = [];
    let pendingFile = null;

    const layers = [];
    const editableLayers = evented({
        addLayer(l) { layers.push(l); },
        removeLayer(l) { const i = layers.indexOf(l); if (i >= 0) layers.splice(i, 1); },
        hasLayer: (l) => layers.includes(l),
        clearLayers() { calls.push('clearLayers'); layers.length = 0; },
        getLayers: () => layers.slice(),
        eachLayer(fn) { layers.slice().forEach(fn); },
        getBounds: () => bounds(layers),
    });
    const map = evented({
        fitBounds(b) {
            if (!b.isValid()) throw new Error('Bounds are not valid.');
            calls.push('fitBounds');
        },
        setView(center, zoom) { calls.push(['setView', zoom]); },
        removeLayer() {},
        pm: { setGlobalOptions() {}, addControls() {} },
    });
    const mapFire = map.fire;
    map.fire = (evt, e) => { calls.push(evt); mapFire(evt, e); };
    const entries = () => elements['.zone-list'].children.filter((c) => c.tagName === 'ZONE-ENTRY');

    const document = {
        querySelector: (sel) => (sel === 'zone-entry[editing="true"]'
            ? entries().find((e) => e.attributes.editing === 'true') || null
            : elements[sel] || null),
        getElementById: (id) => (id === 'save-status' ? status : null),
        querySelectorAll: (sel) => (sel === 'zone-entry' ? entries() : []),
        createElement(tag) {
            const el = tag === 'zone-entry' ? fakeZoneEntry() : fakeElement(tag);
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
        fetch: (url, opts) => {
            if (url !== './save_zones') return new Promise(() => {});
            posts.push(opts);
            if (reply.throwSync) throw new TypeError('fetch threw');
            if (reply.hang) {
                // Like fetch: an already-aborted signal rejects at once.
                const aborted = () => { const e = new Error('aborted'); e.name = 'AbortError'; return e; };
                if (opts.signal.aborted) return Promise.reject(aborted());
                return new Promise((_, reject) => opts.signal.addEventListener('abort', () => reject(aborted())));
            }
            return Promise.resolve({
                status: reply.status,
                ok: reply.status >= 200 && reply.status < 300,
                headers: { get: (h) => (h.toLowerCase() === 'etag' ? reply.etag : null) },
            });
        },
        setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
        clearTimeout: (id) => { cleared.push(id); },
        AbortController,
        console,
        L: { geoJSON, Polygon },
        crypto: require('node:crypto').webcrypto,
        AbortSignal: noAbortTimeout ? {} : AbortSignal,
        FileReader: class {
            readAsText(file) { this.onload({ target: { result: file } }); }
        },
        name_popup_element: (feature) => ({ text: feature.properties.name }),
        pz_layer_polygons: () => [{}],
        pz_layer_area_m2: (layer) => layer.area || 0,
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
        zoneList: elements['.zone-list'],
        posts,
        status,
        reply: (s, etag = null) => { reply = { status: s, etag }; },
        hang: () => { reply = { hang: true }; },
        throwSync: () => { reply = { throwSync: true }; },
        timers,
        cleared,
        map,
        editableLayers,
        entries,
        setupEditing: () => vm.runInContext('setup_editing', context)(map, editableLayers),
        fireSaveTimeout: () => timers.filter((t) => t.ms === 30000).forEach((t) => t.fn()),
        setEtag: (e) => vm.runInContext('(e) => { zones_etag = e; }', context)(e),
        save: () => vm.runInContext('save_zones', context)(),
        saveBtn: elements['.save-btn'],
        setLoadFailed: () => vm.runInContext('zones_load_failed = true', context),
        markDirty: () => vm.runInContext('mark_dirty', context)(),
        addZones: (data, replace) => vm.runInContext('add_zones', context)(data, replace),
        editZone: (detail) => vm.runInContext('edit_zone_event', context)({ detail, target: fakeElement('zone-entry') }),
        loadSaved: (data, restored = false) => vm.runInContext('load_saved_zones', context)(data, restored),
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

const unnamed = (properties) => ({
    type: 'Feature', properties,
    geometry: { type: 'Polygon', coordinates: [[[0, 0], [1, 0], [1, 1], [0, 0]]] },
});

test('bulk load accepts polygons without a name, as the server does', () => {
    const m = withExistingZone();
    m.bulkLoad(collection(unnamed(null), unnamed({ id: 'abc' })));
    assert.deepEqual(m.errors, []);
    assert.equal(m.layers.length, 2);
    assert.deepEqual(m.layers.map((l) => l.feature.properties.name), [undefined, undefined]);
    assert.equal(m.layers[1].feature.properties.id, 'abc', 'a saved id is kept');
    assert.ok(m.layers[0].feature.properties.id, 'a zone without one gets an id');
});

test('first load keeps unnamed zones, so a save cannot drop them', () => {
    const m = loadMap();
    m.loadSaved(JSON.parse(collection(polygon('A'), unnamed({ id: 'abc' }), unnamed(null))));
    assert.deepEqual(m.errors, []);
    assert.equal(m.layers.length, 3);
    assert.deepEqual(m.layers.map((l) => l.feature.properties.name), ['A', undefined, undefined]);
    assert.equal(m.layers[1].feature.properties.id, 'abc');
});

test('first load adds to zones already drawn instead of replacing them', () => {
    const m = withExistingZone();
    m.loadSaved(JSON.parse(collection(polygon('A'))));
    assert.ok(!m.calls.includes('clearLayers'), 'a zone drawn before the load finished was cleared');
    assert.deepEqual(m.layers.map((l) => l.feature.properties.name), ['Home', 'A']);
});

test('first load keeps the polygon zones and says what it skipped', () => {
    const m = loadMap();
    m.loadSaved(JSON.parse(collection(polygon('A'), {
        type: 'Feature', properties: { name: 'Pin' }, geometry: { type: 'Point', coordinates: [0, 0] },
    })));
    assert.deepEqual(m.errors, ['1 saved feature(s) are not polygon zones; they were not loaded and the next save drops them.']);
    assert.deepEqual(m.layers.map((l) => l.feature.properties.name), ['A']);
});

test('bulk load refuses a zone name that is not text, before clearing', () => {
    const m = withExistingZone();
    m.bulkLoad(collection(unnamed({ name: 5 })));
    assert.equal(m.errors.length, 1);
    assert.ok(!m.calls.includes('clearLayers'));
    assert.deepEqual(m.layers.map((l) => l.feature.properties.name), ['Home']);
});

test('an unnamed zone is listed with an empty name, not "undefined"', () => {
    const m = loadMap();
    m.loadSaved(JSON.parse(collection(unnamed({ id: 'abc' }))));
    const entries = m.zoneList.children.filter((c) => c.tagName === 'ZONE-ENTRY');
    assert.equal(entries.length, 1);
    assert.equal(entries[0].attributes.name, '');
});

test('bulk load accepts a null name, as the server does', () => {
    const m = withExistingZone();
    m.bulkLoad(collection(unnamed({ name: null })));
    assert.deepEqual(m.errors, []);
    assert.equal(m.layers.length, 1);
});

test('finishing an edit of an unnamed zone with an empty box leaves it unnamed', () => {
    const m = loadMap();
    m.loadSaved(JSON.parse(collection(unnamed({ id: 'a' }), unnamed({ id: 'b' }))));
    for (const id of ['a', 'b']) m.editZone({ editing: false, name: '', oldName: '', id });
    assert.deepEqual(m.layers.map((l) => l.feature.properties.id), ['a', 'b'], 'saved ids are kept');
    // Two zones named "" would be refused on save as duplicate names.
    assert.deepEqual(m.layers.map((l) => 'name' in l.feature.properties), [false, false]);
});

test('the second of two unnamed zones without ids is the one renamed', () => {
    const m = loadMap();
    m.loadSaved(JSON.parse(collection(unnamed(null), unnamed({}))));
    const ids = m.layers.map((l) => l.feature.properties.id);
    assert.ok(ids.every(Boolean) && ids[0] !== ids[1], 'each loaded zone gets its own id');
    m.editZone({ editing: false, name: 'Shed', oldName: '', id: ids[1] });
    assert.deepEqual(m.layers.map((l) => l.feature.properties.name), [undefined, 'Shed']);
});

test('first load keeps a remembered view instead of centring on the zones', () => {
    const m = loadMap();
    m.loadSaved(JSON.parse(collection(polygon('A'))), true);
    assert.ok(!m.calls.some((c) => Array.isArray(c) && c[0] === 'setView'));
});

test('first load of no zones offers the load button', () => {
    const m = loadMap();
    m.loadSaved(JSON.parse(collection()));
    assert.ok(m.calls.includes('create_load_btn'));
});

test('after a 412, the next save still sends the ETag the editor loaded', async () => {
    const m = loadMap();
    m.loadSaved(JSON.parse(collection(polygon('A'))));
    m.setEtag('"loaded"');
    m.reply(412, '"theirs"');
    await m.save();
    m.reply(200, '"next"');
    await m.save();
    assert.equal(m.posts.length, 2);
    assert.equal(m.posts[1].headers['If-Match'], '"loaded"', 'a retry must not adopt the conflicting version');
});

test('a successful save adopts the new ETag', async () => {
    const m = loadMap();
    m.loadSaved(JSON.parse(collection(polygon('A'))));
    m.setEtag('"loaded"');
    m.reply(200, '"saved"');
    await m.save();
    await m.save();
    assert.equal(m.posts[1].headers['If-Match'], '"saved"');
});

test('save refuses until the saved zones have loaded', async () => {
    const m = loadMap();
    await m.save();
    assert.equal(m.posts.length, 0, 'saved without knowing what was on disk');
    assert.ok(m.status.textContent.length > 0, 'the refusal is shown');
});

test('save is allowed after a bulk load even if the first load failed', async () => {
    const m = loadMap();
    m.setLoadFailed();
    m.bulkLoad(collection(polygon('A')));
    await m.save();
    assert.equal(m.posts.length, 1);
});

test('a 412 keeps the editor dirty and says why', async () => {
    const m = loadMap();
    m.loadSaved(JSON.parse(collection(polygon('A'))));
    m.markDirty();
    m.reply(412, '"theirs"');
    await m.save();
    assert.equal(m.isDirty(), true);
    assert.match(m.status.textContent, /^Conflict/);
});

test('a second click while a save is in flight sends nothing', async () => {
    const m = loadMap();
    m.loadSaved(JSON.parse(collection(polygon('A'))));
    m.setEtag('"loaded"');
    m.reply(200, '"saved"');
    const first = m.save();
    const second = m.save();
    await Promise.all([first, second]);
    assert.equal(m.posts.length, 1, 'the duplicate would hit its own 412');
});

test('an edit made while a save is in flight stays unsaved', async () => {
    const m = loadMap();
    m.loadSaved(JSON.parse(collection(polygon('A'))));
    m.markDirty();
    m.reply(200, '"saved"');
    const pending = m.save();
    m.markDirty();
    await pending;
    assert.equal(m.isDirty(), true, 'the later edit was marked saved');
});

test('while the saved zones are still loading, save asks you to wait', async () => {
    const m = loadMap();
    await m.save();
    assert.match(m.status.textContent, /still loading/);
});

test('a save says it is saving, and a click meanwhile leaves that in place', async () => {
    const m = loadMap();
    m.loadSaved(JSON.parse(collection(polygon('A'))));
    m.reply(200, '"saved"');
    const first = m.save();
    assert.equal(m.status.textContent, 'Saving…');
    const second = m.save();
    assert.equal(m.status.textContent, 'Saving…', 'the ignored click wiped the status');
    await Promise.all([first, second]);
    assert.equal(m.status.textContent, 'Zones saved.');
});

test('a save that gets no answer times out, says so, and frees Save', async () => {
    const m = loadMap();
    m.loadSaved(JSON.parse(collection(polygon('A'))));
    m.hang();
    const pending = m.save();
    m.fireSaveTimeout();
    await pending;
    assert.match(m.status.textContent, /^Save timed out\. It may not have saved/);
    m.reply(200, '"saved"');
    await m.save();
    assert.equal(m.posts.length, 2, 'Save stayed blocked after the timeout');
});

test('saving works on a browser without AbortSignal.timeout', async () => {
    const m = loadMap({ noAbortTimeout: true });
    m.loadSaved(JSON.parse(collection(polygon('A'))));
    m.reply(200, '"saved"');
    await m.save();
    await m.save();
    assert.equal(m.posts.length, 2);
    assert.equal(m.status.textContent, 'Zones saved.');
});

test('a bulk load clears a not-loaded refusal', async () => {
    const m = loadMap();
    await m.save();
    m.bulkLoad(collection(polygon('A')));
    assert.equal(m.status.textContent, '');
    assert.ok(!m.saveBtn.classList.contains('error'));
});

test('sending a save drops the previous success colour', async () => {
    const m = loadMap();
    m.loadSaved(JSON.parse(collection(polygon('A'))));
    m.reply(200, '"saved"');
    await m.save();
    assert.ok(m.saveBtn.classList.contains('success'));
    m.hang();
    m.save();
    assert.ok(!m.saveBtn.classList.contains('success'));
});

test('after a failed load, save says the zones could not be loaded', async () => {
    const m = loadMap();
    m.setLoadFailed();
    await m.save();
    assert.match(m.status.textContent, /couldn't be loaded/);
});

test('the not-loaded refusal clears once the zones load', async () => {
    const m = loadMap();
    await m.save();
    assert.ok(m.saveBtn.classList.contains('error'));
    m.loadSaved(JSON.parse(collection(polygon('A'))));
    assert.equal(m.status.textContent, '');
    assert.ok(!m.saveBtn.classList.contains('error'));
});

test('a new save cancels the previous success timer', async () => {
    const m = loadMap();
    m.loadSaved(JSON.parse(collection(polygon('A'))));
    m.reply(200, '"saved"');
    await m.save();
    const id = m.timers.findIndex((t) => t.ms === 2000) + 1;
    assert.ok(id > 0, 'the success message is not timed');
    await m.save();
    assert.ok(m.cleared.includes(id), "the old timer would clear the new save's message");
});

test('a fetch that throws at once still frees Save', async () => {
    const m = loadMap();
    m.loadSaved(JSON.parse(collection(polygon('A'))));
    m.throwSync();
    await m.save();
    assert.match(m.status.textContent, /^Save failed/);
    m.reply(200, '"saved"');
    await m.save();
    assert.equal(m.posts.length, 2);
});

test('dragging a vertex marks the editor unsaved and re-measures the zone', () => {
    const m = loadMap();
    m.setupEditing();
    m.loadSaved(JSON.parse(collection(polygon('A'))));
    const layer = m.layers[0];
    layer.area = 1234;
    m.calls.length = 0;
    // Geoman fires pm:edit on the layer and its parent groups, not the map.
    m.editableLayers.fire('pm:edit', { layer });
    assert.equal(m.isDirty(), true);
    assert.equal(m.entries()[0].attributes['area-m2'], '1234');
    assert.ok(m.calls.includes('pz:zoneschanged'), 'the tracker readout is not re-measured');
});

// Opens the first listed zone and types `name` into its box.
function typeInto(m, index, name) {
    const row = m.entries()[index];
    row.edit_event_handler();
    row.typed = name;
    return row;
}

test('saving with a rename still open saves the typed name and closes the row', async () => {
    const m = loadMap();
    m.loadSaved(JSON.parse(collection(polygon('A'), polygon('B'))));
    typeInto(m, 0, 'Renamed');
    assert.equal(m.layers[0].pm.enabled, true);
    m.reply(200, '"saved"');
    await m.save();
    const sent = JSON.parse(m.posts[0].body).features.map((f) => f.properties.name);
    assert.deepEqual(sent, ['Renamed', 'B']);
    assert.equal(m.layers[0].pm.enabled, false, 'the edit handles stayed on');
    assert.equal(m.isDirty(), false);
});

test('saving with an emptied name box leaves the zone unnamed', async () => {
    const m = loadMap();
    m.loadSaved(JSON.parse(collection(polygon('A'))));
    typeInto(m, 0, '');
    m.reply(200, '"saved"');
    await m.save();
    assert.ok(!('name' in JSON.parse(m.posts[0].body).features[0].properties));
});

test('opening another zone keeps the name typed in the open one', () => {
    const m = loadMap();
    m.loadSaved(JSON.parse(collection(polygon('A'), polygon('B'))));
    const a = typeInto(m, 0, 'A2');
    m.entries()[1].edit_event_handler();
    assert.deepEqual(m.layers.map((l) => l.feature.properties.name), ['A2', 'B']);
    assert.equal(a.attributes.editing, 'false');
    assert.deepEqual(m.layers.map((l) => l.pm.enabled), [false, true]);
});

test('drawing a zone while a rename is open keeps the typed name', () => {
    const m = loadMap();
    m.setupEditing();
    m.loadSaved(JSON.parse(collection(polygon('A'))));
    typeInto(m, 0, 'A2');
    m.map.fire('pm:create', { layer: new Polygon() });
    assert.deepEqual(m.layers.map((l) => l.feature.properties.name), ['A2', 'Zone 1']);
    assert.equal(m.layers[0].pm.enabled, false);
});
