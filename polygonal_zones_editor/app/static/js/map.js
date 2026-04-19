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

function generate_map(zones_url) {
    // Tile layers come from the PZBasemaps registry (basemaps.js) so #31
    // can register additional providers without editing this file. The
    // two seed entries (OSM, CARTO dark) are byte-identical to the
    // literals this block previously inlined.
    const osm = window.PZBasemaps.createLayer(
        window.PZBasemaps.getDefaultBasemap('light').id);
    const dark = window.PZBasemaps.createLayer(
        window.PZBasemaps.getDefaultBasemap('dark').id);

    const dark_mq = window.matchMedia('(prefers-color-scheme: dark)');
    // Pick the initial tile layer to match the resolved theme.
    let want_dark;
    if (window.PZ_THEME === 'dark') want_dark = true;
    else if (window.PZ_THEME === 'light') want_dark = false;
    else want_dark = dark_mq.matches; // auto
    const map = L.map('map', {
        layers: [want_dark ? dark : osm],
        center: [52.96523540264812, 6.52002831753822],
        zoom: 13,
    });

    // Tracks whether the user has actively chosen a basemap (via the
    // picker landing with #31). Once true, the theme-follow auto-swap
    // below is suppressed so the user's explicit choice isn't overridden
    // when the OS light/dark preference changes. baselayerchange fires
    // for L.Control.Layers radio clicks; direct addLayer/removeLayer
    // does not fire it, so the auto-swap itself won't flip this flag.
    let userChoseTile = false;
    map.on('baselayerchange', () => {
        userChoseTile = true;
        // #31 will persist the chosen layer id to pz:basemap here.
    });

    // Only follow OS changes when theme=auto AND the user hasn't
    // manually picked a basemap yet.
    if (window.PZ_THEME !== 'light' && window.PZ_THEME !== 'dark') {
        dark_mq.addEventListener('change', e => {
            if (userChoseTile) return;
            const next = e.matches ? dark : osm;
            const prev = e.matches ? osm : dark;
            if (map.hasLayer(prev)) map.removeLayer(prev);
            if (!map.hasLayer(next)) next.addTo(map);
        });
    }

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
            if (editableLayers.getLayers().length > 0) {
                map.fitBounds(editableLayers.getBounds());
                map.setView(editableLayers.getBounds().getCenter(), 13);
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

    map.on('draw:deleted', function (e) {
        let layers = e.layers;
        layers.eachLayer(layer => {
            editableLayers.removeLayer(layer);
            render_zone_list();
        });

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
        layer.feature = {
            properties: {
                name: name
            }
        };

        console.log(layer)
        editableLayers.addLayer(layer);
        // log the geojson
        console.log(layer.toGeoJSON());
        render_zone_list();

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
            if (status) status.textContent =
                'Conflict: zones changed in another session. Reload to fetch the current version, then re-apply your edits.';
            return;
        }
        if (response.ok) {
            elem.classList.remove('error')
            elem.classList.add('success')
            if (status) status.textContent = 'Zones saved.';
        } else {
            elem.classList.remove('success')
            elem.classList.add('error')
            if (status) status.textContent = `Save failed (${response.status}).`;
        }

        setTimeout(() => {
            elem.classList.remove('error')
            elem.classList.remove('success')
            if (status) status.textContent = '';
        }, 2000)
    }).catch(err => {
        let elem = document.querySelector('.save-btn');
        let status = document.getElementById('save-status');
        elem.classList.remove('success')
        elem.classList.add('error')
        if (status) status.textContent = `Save failed: ${err.message}`;
        setTimeout(() => {
            elem.classList.remove('error')
            if (status) status.textContent = '';
        }, 2000)
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