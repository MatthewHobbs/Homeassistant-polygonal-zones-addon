// Registry of Leaflet basemap layers used by the editor. map.js resolves
// its initial tile layer through getDefaultBasemap() and createLayer()
// instead of hardcoding L.tileLayer calls, so #31 (user-selectable tile
// layers) only needs to call register() for each additional provider —
// no further changes to map.js or index.html.
//
// The two seed entries are copied byte-identical from the original
// map.js:30-37 tile layer literals so this refactor is pixel-neutral.
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

    function register(entry) {
        entries.push(entry);
    }

    function createLayer(id) {
        const entry = getBasemap(id);
        if (!entry) return null;
        const options = Object.assign({}, entry.options, {attribution: entry.attribution});
        return L.tileLayer(entry.url, options);
    }

    window.PZBasemaps = {getBasemap, getDefaultBasemap, listBasemaps, register, createLayer};
})();
