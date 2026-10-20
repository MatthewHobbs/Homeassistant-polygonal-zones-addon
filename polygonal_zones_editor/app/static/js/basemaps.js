// Registry of the Leaflet basemap layers the editor offers. map.js builds its
// tile layers and the basemap picker from this list, so a provider is added
// here, plus its tile host in main.py's CSP img-src.
(function () {
    const osmAttrib = '&copy; <a href="https://openstreetmap.org">OpenStreetMap</a> contributors';

    const entries = [
        {
            id: 'osm',
            label: 'OpenStreetMap',
            url: 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
            attribution: osmAttrib,
            options: {maxZoom: 18},
            darkAffinity: 'light',
        },
        {
            id: 'carto-dark',
            label: 'CARTO Dark',
            url: 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
            attribution: osmAttrib + ' &copy; <a href="https://carto.com/attributions">CARTO</a>',
            options: {maxZoom: 19, subdomains: 'abcd'},
            darkAffinity: 'dark',
        },
        {
            // Esri World Imagery is the only free satellite option with a
            // permissive ToS for embedded use (Mapbox / Google require API
            // keys and a billing relationship). darkAffinity is 'neither' so
            // it never gets picked by getDefaultBasemap's auto resolution —
            // users explicitly opt in via the picker.
            id: 'esri-imagery',
            label: 'Esri World Imagery',
            url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
            attribution: 'Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics',
            options: {maxZoom: 19},
            darkAffinity: 'neither',
        },
    ];

    function getBasemap(id) {
        return entries.find(e => e.id === id) || null;
    }

    function getDefaultBasemap(theme) {
        // theme: 'light' | 'dark' | 'auto' (anything else treated as 'auto')
        let wantDark;
        if (theme === 'dark') wantDark = true;
        else if (theme === 'light') wantDark = false;
        else wantDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
        const affinity = wantDark ? 'dark' : 'light';
        return entries.find(e => e.darkAffinity === affinity) || entries[0];
    }

    function listBasemaps() {
        return entries.slice();
    }

    function createLayer(id) {
        const entry = getBasemap(id);
        if (!entry) return null;
        const options = Object.assign({}, entry.options, {attribution: entry.attribution});
        return L.tileLayer(entry.url, options);
    }

    window.PZBasemaps = {getDefaultBasemap, listBasemaps, createLayer};
})();
