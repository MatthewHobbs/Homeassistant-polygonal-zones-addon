// Collapsible sidebar toggle. Thin wiring only: the state contract (attribute
// write, persistence, map.invalidateSize) lives in ui-state.js. This file owns
// the button — click handler, keyboard access, and mirroring aria-expanded.
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
