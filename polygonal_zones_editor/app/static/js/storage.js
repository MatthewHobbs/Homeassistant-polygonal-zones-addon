// Thin wrapper over localStorage: Safari private mode, disabled storage and
// quota errors all fall back to an in-memory Map. State then does not persist,
// which beats throwing on every read for UI preferences.
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
