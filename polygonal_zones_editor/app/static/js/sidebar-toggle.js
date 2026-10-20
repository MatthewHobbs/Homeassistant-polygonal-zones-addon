// Collapsible sidebar toggle (#29). Thin wiring only — the state
// contract (attribute write + persistence + map.invalidateSize) lives
// in ui-state.js. This file owns the button: click handler, keyboard
// accessibility, aria-state mirroring.
//
// The button is always visible: in "open" state it sits on the left
// edge of the sidebar pointing right-into-left (collapse icon); in
// "collapsed" state it moves to the right edge of the map pointing
// left-into-right (expand icon). Both positions are set by CSS off
// the .body[data-sidebar] attribute.
(function () {
    const button = document.querySelector('.sidebar-toggle');
    if (!button || !window.PZUiState) return;

    function labelFor(state) {
        return state === 'open' ? 'Collapse sidebar' : 'Expand sidebar';
    }

    function syncButton(state) {
        button.setAttribute('aria-label', labelFor(state));
        button.setAttribute('aria-expanded', state === 'open' ? 'true' : 'false');
    }

    // ui-state.js has already applied any remembered state; mirror the DOM,
    // not the HTML default.
    syncButton(window.PZUiState.getSidebarState());

    button.addEventListener('click', () => {
        const next = window.PZUiState.getSidebarState() === 'open' ? 'collapsed' : 'open';
        window.PZUiState.setSidebarState(next);
        syncButton(window.PZUiState.getSidebarState());
    });
})();
