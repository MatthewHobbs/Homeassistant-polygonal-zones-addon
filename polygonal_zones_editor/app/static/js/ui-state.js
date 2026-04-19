// Sidebar UI-state contract. The .body element carries a
// data-sidebar="open|collapsed|drawer" attribute; CSS keys its layout
// off the value. This module is the single write-point for that
// attribute and owns the plumbing that every caller needs to get right
// — persist the choice to localStorage (pz:sidebar), tell Leaflet its
// container resized (invalidateSize), and announce pz:map-resized so
// downstream listeners (Playwright smoke, future picker-control code)
// can react.
//
// In this refactor nothing calls setSidebarState — the attribute only
// ever holds its hardcoded "open" default. #29 (collapsible sidebar)
// and #30 (responsive drawer) wire the toggle handlers that exercise
// the contract.
(function () {
    const ROOT = document.querySelector('.body');
    let boundMap = null;

    function getSidebarState() {
        return ROOT ? ROOT.getAttribute('data-sidebar') : null;
    }

    function setSidebarState(state, opts) {
        if (!ROOT) return;
        opts = opts || {};
        const persist = opts.persist !== false;
        ROOT.setAttribute('data-sidebar', state);
        if (persist && window.PZStorage) {
            window.PZStorage.set('pz:sidebar', state);
        }
        if (!boundMap) return;

        // After the CSS grid-column transition settles, ask Leaflet to
        // recompute its container size and dispatch pz:map-resized so
        // tests and future picker code have a reliable hook. Under
        // prefers-reduced-motion there is no transition to wait for, so
        // fire on the next tick. Otherwise listen for transitionend on
        // the grid-template-columns property with a 350 ms safety net.
        const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        let fired = false;
        function onResized() {
            if (fired) return;
            fired = true;
            ROOT.removeEventListener('transitionend', onTransitionEnd);
            boundMap.invalidateSize({pan: false});
            document.dispatchEvent(new CustomEvent('pz:map-resized'));
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
        if (!ROOT) return;
        // Resolution: honour a persisted value if present. Viewport-based
        // defaults (drawer on narrow viewports) are deliberately deferred
        // to #30 so this refactor stays pixel-neutral — if a user has
        // never toggled the sidebar, the HTML default ("open") stays.
        const stored = window.PZStorage ? window.PZStorage.get('pz:sidebar') : null;
        if (stored === 'open' || stored === 'collapsed' || stored === 'drawer') {
            ROOT.setAttribute('data-sidebar', stored);
        }
    }

    window.PZUiState = {getSidebarState, setSidebarState, initSidebarState};
})();
