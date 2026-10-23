// Node tests for push-status.js: what the editor says about delivery to Home
// Assistant. Run with `node --test tests/test_push_status.js` from
// polygonal_zones_editor/ (also run in CI by test.yml).

const assert = require('node:assert/strict');
const { test } = require('node:test');
const path = require('node:path');
const { pz_push_describe, pz_push_status_create, PZ_PUSH_POLL_MS, PZ_PUSH_AFTER_SAVE_MS } = require(
    path.join(__dirname, '..', 'app', 'static', 'js', 'push-status.js')
);

// --- what each state says ---------------------------------------------------

test('each state has its own words and level', () => {
    assert.deepEqual(pz_push_describe({ state: 'pending' }), { level: 'info', text: 'Sending to Home Assistant…' });
    assert.deepEqual(pz_push_describe({ state: 'retrying' }), { level: 'warn', text: 'Could not reach Home Assistant. Retrying…' });
    const waiting = pz_push_describe({ state: 'waiting' });
    assert.equal(waiting.level, 'muted');
    assert.match(waiting.text, /Not sent automatically/);
    assert.match(waiting.text, /fine if you use a zone URL/);
});

test('delivered and in sync says when; out of sync says it is sending', () => {
    const done = pz_push_describe({ state: 'delivered', in_sync: true, delivered_at: '2026-10-23T09:05:00Z' });
    assert.equal(done.level, 'ok');
    assert.match(done.text, /^Sent to Home Assistant at \d/);
    assert.equal(pz_push_describe({ state: 'delivered', in_sync: false }).text, 'Sending to Home Assistant…');
});

test('a missing or unreadable time is left out, not shown as nonsense', () => {
    for (const delivered_at of [null, undefined, 'not a date', 5]) {
        assert.equal(pz_push_describe({ state: 'delivered', in_sync: true, delivered_at }).text, 'Sent to Home Assistant');
    }
});

test('a rejection gives the reason, or says plainly there is none', () => {
    assert.equal(
        pz_push_describe({ state: 'rejected', detail: '  has 11000 total vertices ' }).text,
        'Home Assistant rejected the zones: has 11000 total vertices',
    );
    for (const detail of [null, '', '   ', 5, undefined]) {
        assert.equal(pz_push_describe({ state: 'rejected', detail }).text, 'Home Assistant rejected the zones.');
    }
    assert.equal(pz_push_describe({ state: 'rejected' }).level, 'error');
});

test('off, unknown states and garbage show nothing', () => {
    for (const status of [{ state: 'off' }, { state: 'from-the-future' }, {}, null, undefined, 'x', 5, []]) {
        assert.equal(pz_push_describe(status), null);
    }
});

// --- the poller -------------------------------------------------------------

function harness({ visible = true, replies = [] } = {}) {
    const element = { className: '', _text: '', sets: 0 };
    Object.defineProperty(element, 'textContent', {
        get() { return this._text; },
        set(v) { this._text = v; this.sets++; },
    });
    // Anything that put server text through markup would break the page.
    Object.defineProperty(element, 'innerHTML', { set() { throw new Error('innerHTML used'); } });
    const state = { timers: [], cleared: [], fetches: [], visible, replies: [...replies], nextId: 1 };
    const deps = {
        element,
        visible: () => state.visible,
        AbortController,
        setTimeout: (fn, ms) => { const id = state.nextId++; state.timers.push({ id, fn, ms }); return id; },
        clearTimeout: (id) => { state.cleared.push(id); },
        fetch: (url, options) => {
            state.fetches.push({ url, options });
            const next = state.replies.shift();
            if (next instanceof Error) return Promise.reject(next);
            return Promise.resolve(next);
        },
    };
    return { element, state, deps, controller: pz_push_status_create(deps) };
}

const ok = (body) => ({ ok: true, json: () => Promise.resolve(body) });
const settle = () => new Promise((resolve) => setImmediate(resolve));

test('a refresh asks for ./push_status without caching and shows the answer', async () => {
    const h = harness({ replies: [ok({ state: 'pending' })] });
    await h.controller.refresh();
    assert.equal(h.state.fetches[0].url, './push_status');
    assert.equal(h.state.fetches[0].options.cache, 'no-store');
    assert.equal(h.element.textContent, 'Sending to Home Assistant…');
    assert.equal(h.element.className, 'push-status push-info');
});

test('the server\'s reason is shown as text, never as markup', async () => {
    const evil = '<img src=x onerror=alert(1)>';
    const h = harness({ replies: [ok({ state: 'rejected', detail: evil })] });
    await h.controller.refresh(); // an innerHTML write would have thrown
    assert.equal(h.element.textContent, `Home Assistant rejected the zones: ${evil}`);
});

test('an unchanged state is not written again, so it is not announced again', async () => {
    const h = harness({ replies: [ok({ state: 'retrying' }), ok({ state: 'retrying' }), ok({ state: 'pending' })] });
    await h.controller.refresh();
    await h.controller.refresh();
    assert.equal(h.element.sets, 1);
    await h.controller.refresh();
    assert.equal(h.element.sets, 2);
});

test('off clears a line that was showing', async () => {
    const h = harness({ replies: [ok({ state: 'pending' }), ok({ state: 'off' })] });
    await h.controller.refresh();
    await h.controller.refresh();
    assert.equal(h.element.textContent, '');
    assert.equal(h.element.className, 'push-status');
});

test('a dropped or refused request keeps what is shown, so a blip does not blank or re-announce it', async () => {
    const replies = [new Error('network down'), { ok: false, json: () => Promise.resolve({ state: 'off' }) }];
    for (const reply of replies) {
        const h = harness({ replies: [ok({ state: 'rejected', detail: 'too many vertices' }), reply] });
        await h.controller.refresh();
        const before = h.element.textContent;
        assert.match(before, /rejected the zones: too many vertices/);
        await h.controller.refresh();
        assert.equal(h.element.textContent, before);
        assert.equal(h.element.sets, 1, 'the text was written again');
    }
});

test('a reply that is not JSON clears the line and never throws', async () => {
    const h = harness({
        replies: [ok({ state: 'pending' }), { ok: true, json: () => Promise.reject(new SyntaxError('bad json')) }],
    });
    await h.controller.refresh();
    assert.equal(h.element.textContent, 'Sending to Home Assistant…');
    await h.controller.refresh();
    assert.equal(h.element.textContent, '');
});

test('an older reply that arrives late does not overwrite a newer one', async () => {
    let finishOlder;
    const older = new Promise((resolve) => { finishOlder = resolve; });
    const h = harness({ replies: [older, ok({ state: 'delivered', in_sync: true })] });
    const first = h.controller.refresh();
    const second = h.controller.refresh();
    await second;
    assert.match(h.element.textContent, /^Sent to Home Assistant/);
    finishOlder(ok({ state: 'pending' }));
    await first;
    assert.match(h.element.textContent, /^Sent to Home Assistant/, 'the stale reply won');
});

test('a hidden tab asks nothing', async () => {
    const h = harness({ visible: false });
    await h.controller.refresh();
    assert.equal(h.state.fetches.length, 0);
});

test('each request has a timeout that is cleared afterwards', async () => {
    const h = harness({ replies: [ok({ state: 'pending' })] });
    await h.controller.refresh();
    const abort = h.state.timers.find((t) => t.ms === 8000);
    assert.ok(abort, 'no abort timer');
    assert.ok(h.state.cleared.includes(abort.id), 'the timer was left running');
    abort.fn(); // firing it only aborts the request; it must not throw
});

test('start looks now, then again every ten seconds', async () => {
    const h = harness({ replies: [ok({ state: 'pending' }), ok({ state: 'pending' }), ok({ state: 'pending' })] });
    h.controller.start();
    await settle();
    assert.equal(h.state.fetches.length, 1);
    const poll = h.state.timers.filter((t) => t.ms === PZ_PUSH_POLL_MS);
    assert.equal(poll.length, 1);
    poll[0].fn();
    await settle();
    assert.equal(h.state.fetches.length, 2);
    assert.equal(h.state.timers.filter((t) => t.ms === PZ_PUSH_POLL_MS).length, 2, 'the poll did not reschedule');
});

test('after a save it looks again soon, then a little later', async () => {
    const h = harness({ replies: [ok({ state: 'pending' }), ok({ state: 'delivered', in_sync: true })] });
    h.controller.afterSave();
    assert.deepEqual(h.state.timers.map((t) => t.ms), PZ_PUSH_AFTER_SAVE_MS);
    assert.deepEqual(PZ_PUSH_AFTER_SAVE_MS, [1500, 4000]);
    h.state.timers[0].fn();
    await settle();
    h.state.timers[1].fn();
    await settle();
    assert.equal(h.state.fetches.length, 2);
    assert.match(h.element.textContent, /^Sent to Home Assistant/);
});

test('a page without the status element still works', async () => {
    const h = harness({ replies: [ok({ state: 'pending' })] });
    const controller = pz_push_status_create({ ...h.deps, element: null });
    await controller.refresh(); // nothing to draw on: no error
    assert.equal(h.state.fetches.length, 1);
});
