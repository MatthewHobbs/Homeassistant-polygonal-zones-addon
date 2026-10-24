// Sidebar UI state. `.body` carries data-sidebar="open|collapsed" and CSS keys
// layout off it; this module is its only writer, and owns persisting the choice
// (pz:sidebar) and telling Leaflet its container resized.
(function () {
    const ROOT = document.querySelector('.body');
    let boundMap = null;

    // Applied now, before map.js builds the map: Leaflet measures its
    // container once, and a sidebar collapsed after that leaves a blank strip.
    const stored = window.PZStorage ? window.PZStorage.get('pz:sidebar') : null;
    if (ROOT && (stored === 'open' || stored === 'collapsed')) {
        ROOT.setAttribute('data-sidebar', stored);
    }

    function getSidebarState() {
        return ROOT ? ROOT.getAttribute('data-sidebar') : null;
    }

    function setSidebarState(state) {
        if (!ROOT) return;
        ROOT.setAttribute('data-sidebar', state);
        if (window.PZStorage) window.PZStorage.set('pz:sidebar', state);
        if (!boundMap) return;

        // After the CSS grid-column transition settles, ask Leaflet to
        // recompute its container size. Under prefers-reduced-motion there
        // is no transition to wait for, so do it on the next tick. Otherwise
        // listen for transitionend on grid-template-columns, with a 350 ms
        // safety net.
        const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        let fired = false;
        function onResized() {
            if (fired) return;
            fired = true;
            ROOT.removeEventListener('transitionend', onTransitionEnd);
            boundMap.invalidateSize({pan: false});
        }
        function onTransitionEnd(e) {
            if (e.target === ROOT && e.propertyName === 'grid-template-columns') {
                onResized();
            }
        }
        if (reduced) {
            setTimeout(onResized, 0);
        } else {
            ROOT.addEventListener('transitionend', onTransitionEnd);
            setTimeout(onResized, 350);
        }
    }

    function initSidebarState(map) {
        boundMap = map;
    }

    window.PZUiState = {getSidebarState, setSidebarState, initSidebarState};
})();
