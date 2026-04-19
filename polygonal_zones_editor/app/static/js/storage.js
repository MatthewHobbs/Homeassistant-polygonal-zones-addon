// Thin wrapper over window.localStorage so callers never have to think
// about Safari private-mode, disabled storage, or quota errors. On any
// failure we silently fall back to an in-memory Map — state simply
// does not persist across reloads, which is the right failure mode for
// UI preferences (pz:sidebar, pz:basemap, etc.) where losing the value
// is strictly better than throwing on every read.
(function () {
    const fallback = new Map();
    let storageOk = true;
    try {
        const probe = '__pz_probe__';
        window.localStorage.setItem(probe, '1');
        window.localStorage.removeItem(probe);
    } catch (e) {
        storageOk = false;
    }

    function get(key) {
        if (storageOk) {
            try { return window.localStorage.getItem(key); }
            catch (e) { /* fall through */ }
        }
        return fallback.has(key) ? fallback.get(key) : null;
    }

    function set(key, value) {
        if (storageOk) {
            try { window.localStorage.setItem(key, value); return; }
            catch (e) { /* fall through */ }
        }
        fallback.set(key, value);
    }

    function remove(key) {
        if (storageOk) {
            try { window.localStorage.removeItem(key); }
            catch (e) { /* ignore */ }
        }
        fallback.delete(key);
    }

    window.PZStorage = { get, set, remove };
})();
