/*
 * Delivery status: have the saved zones reached Home Assistant?
 *
 * The add-on pushes every save to the companion integration in the background
 * (app/push.py). A push can wait, be refused, or retry, and a save that looks
 * fine here but never arrived would leave presence automations on old zones.
 * So this line says where the zones stand, and says it in words.
 *
 * Text from the server (`detail`) is only ever set with textContent.
 */

const PZ_PUSH_ENDPOINT = './push_status';
const PZ_PUSH_POLL_MS = 10000;
// Right after a save: a quick look, then a slower one, then back to the poll.
const PZ_PUSH_AFTER_SAVE_MS = [1500, 4000];
const PZ_PUSH_TIMEOUT_MS = 8000;

const PZ_PUSH_SENDING = 'Sending to Home Assistant…';

function pz_push_time(iso) {
    const when = new Date(iso);
    if (typeof iso !== 'string' || Number.isNaN(when.getTime())) return '';
    return ` at ${when.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
}

/* What to show for a status reply: {level, text}, or null to show nothing. */
function pz_push_describe(status) {
    if (!status || typeof status !== 'object') return null;
    switch (status.state) {
        case 'pending':
            return { level: 'info', text: PZ_PUSH_SENDING };
        case 'delivered':
            // Delivered, but the zones have changed since.
            if (!status.in_sync) return { level: 'info', text: PZ_PUSH_SENDING };
            return { level: 'ok', text: `Sent to Home Assistant${pz_push_time(status.delivered_at)}` };
        case 'waiting':
            return {
                level: 'muted',
                text: 'Not sent automatically: the integration is not set up to receive zones '
                    + 'from this add-on. This is fine if you use a zone URL.',
            };
        case 'rejected': {
            const detail = typeof status.detail === 'string' ? status.detail.trim() : '';
            return {
                level: 'error',
                text: detail ? `Home Assistant rejected the zones: ${detail}` : 'Home Assistant rejected the zones.',
            };
        }
        case 'retrying':
            return { level: 'warn', text: 'Could not reach Home Assistant. Retrying…' };
        default:
            return null; // off, or a state this editor does not know
    }
}

/*
 * The poller. Everything it touches is passed in, so tests run it with fakes:
 * deps = {element, fetch, setTimeout, clearTimeout, visible(), AbortController}.
 */
function pz_push_status_create(deps) {
    const { element, fetch: doFetch, visible } = deps;
    let shown = null; // the text on screen, so an unchanged state is not re-announced
    let pollTimer = null;
    let latest = 0; // the newest request started; an older one finishing late is ignored
    const KEEP = Symbol('keep what is on screen');

    function render(description) {
        if (!element) return;
        const text = description ? description.text : '';
        if (text === shown) return;
        shown = text;
        element.textContent = text;
        element.className = description ? `push-status push-${description.level}` : 'push-status';
    }

    function refresh() {
        if (!visible()) return Promise.resolve();
        const mine = ++latest;
        const controller = new deps.AbortController();
        const timer = deps.setTimeout(() => controller.abort(), PZ_PUSH_TIMEOUT_MS);
        return Promise.resolve()
            .then(() => doFetch(PZ_PUSH_ENDPOINT, { signal: controller.signal, cache: 'no-store' }))
            // A refused request keeps what is shown; a reply that is not JSON clears it.
            .then((response) => (response.ok ? response.json().catch(() => null) : KEEP))
            .then((status) => {
                if (mine === latest && status !== KEEP) render(pz_push_describe(status));
            })
            // A dropped connection keeps what is shown too: blanking and redrawing a
            // rejection on every blip would announce it again each time.
            .catch(() => {})
            .finally(() => deps.clearTimeout(timer));
    }

    function schedule() {
        deps.clearTimeout(pollTimer);
        pollTimer = deps.setTimeout(() => {
            refresh().finally(schedule);
        }, PZ_PUSH_POLL_MS);
    }

    return {
        refresh,
        start() {
            refresh();
            schedule();
        },
        afterSave() {
            PZ_PUSH_AFTER_SAVE_MS.forEach((ms) => deps.setTimeout(refresh, ms));
        },
    };
}

if (typeof module !== 'undefined' && module.exports) {
    module.exports = { pz_push_describe, pz_push_status_create, PZ_PUSH_POLL_MS, PZ_PUSH_AFTER_SAVE_MS };
} else {
    const controller = pz_push_status_create({
        element: document.getElementById('push-status'),
        fetch: (url, options) => fetch(url, options),
        setTimeout: (fn, ms) => setTimeout(fn, ms),
        clearTimeout: (id) => clearTimeout(id),
        visible: () => document.visibilityState === 'visible',
        AbortController,
    });
    window.pz_push_status_after_save = controller.afterSave;
    // Come back to the tab and see the truth at once, not after the next poll.
    document.addEventListener('visibilitychange', controller.refresh);
    controller.start();
}
