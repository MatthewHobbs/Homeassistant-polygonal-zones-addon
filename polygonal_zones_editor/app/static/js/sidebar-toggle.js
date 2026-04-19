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

    // Initial sync — ui-state.js may have resolved a persisted state on
    // load, and the HTML default is "open". Mirror whichever ended up on
    // the DOM so the button's aria-label and aria-expanded are correct
    // before the first click.
    const initial = window.PZUiState.getSidebarState() || 'open';
    syncButton(initial);

    button.addEventListener('click', () => {
        const current = window.PZUiState.getSidebarState() || 'open';
        const next = current === 'open' ? 'collapsed' : 'open';
        window.PZUiState.setSidebarState(next);
        syncButton(next);
    });
})();
