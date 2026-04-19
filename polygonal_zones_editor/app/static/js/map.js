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
    });

function generate_map(zones_url) {
    const osmAttrib = '&copy; <a href="https://openstreetmap.org">OpenStreetMap</a> contributors';
    const osm = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
        {maxZoom: 18, attribution: osmAttrib});
    // CARTO dark basemap for prefers-color-scheme: dark. Free, OSM-based.
    const dark = L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
        {maxZoom: 19,
         subdomains: 'abcd',
         attribution: osmAttrib + ' &copy; <a href="https://carto.com/attributions">CARTO</a>'});

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

    // Only follow OS changes when theme=auto. theme=light/dark stay pinned.
    if (window.PZ_THEME !== 'light' && window.PZ_THEME !== 'dark') {
        dark_mq.addEventListener('change', e => {
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

function render_zone_list() {
    let zone_list = document.querySelector('.zone-list');
    zone_list.innerHTML = '';
    editableLayers.eachLayer(layer => {
        // render a zone-entry element and set attribute name
        let zone_entry = document.createElement('zone-entry');
        zone_entry.setAttribute('name', layer.feature.properties.name);
        zone_entry.addEventListener('edit', edit_zone_event);

        zone_list.appendChild(zone_entry);
    });
}

function save_zones() {
    let geojson = {
        type: "FeatureCollection",
        features: Object.values(editableLayers._layers).map(value => {
            const points = value._latlngs[0].map(point => [point.lng, point.lat]);
            return {
                type: "Feature",
                properties: {
                    name: value.feature.properties.name
                },
                geometry: {
                    type: "Polygon",
                    coordinates: [points]
                }
            }
        })
    };

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