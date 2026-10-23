"""The pusher: classification, scheduling, persistence, logging, and the route and limits wired to it."""

import hashlib
import http.client
import io
import json
import logging
from pathlib import Path
import urllib.error

import push
import pytest
from starlette.testclient import TestClient

import const

INGRESS = ("172.30.32.2", 5000)
TOKEN = "supervisor-secret-token-123"


def etag_for(body: bytes) -> str:
    return f'"{hashlib.sha256(body).hexdigest()}"'


class Reply:
    def __init__(self, status, body=b""):
        self.status, self._body, self.read_limit = status, body, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, limit):
        self.read_limit = limit
        return self._body[:limit]


class Opener:
    """Hands out replies in order and records every request it was given."""

    def __init__(self, *replies):
        self.replies, self.requests, self.last = list(replies), [], None

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        item = self.replies.pop(0)
        if isinstance(item, BaseException):
            raise item
        status, body = item
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "x", {}, io.BytesIO(body))
        self.last = Reply(status, body)
        return self.last


@pytest.fixture
def env(tmp_path, monkeypatch):
    zones = tmp_path / "zones.json"
    zones.write_bytes(b'{"type":"FeatureCollection","features":[]}')
    monkeypatch.setattr(const, "ZONES_FILE", str(zones))
    monkeypatch.setattr(const, "PUSH_STATE_FILE", str(tmp_path / "push-state.json"))
    return zones


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make(opener, options=None, clock=None, rng=lambda: 0.5, token=TOKEN):
    return push.Pusher(
        options or {},
        etag_for=etag_for,
        token=token,
        opener=opener,
        monotonic=clock or Clock(),
        rng=rng,
    )


# --- classify -----------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "state", "schedule", "delay"),
    [
        (200, push.DELIVERED, push.NONE, 0.0),
        (401, push.REJECTED, push.FIXED, const.PUSH_REFUSED_RETRY_SECONDS),
        (403, push.REJECTED, push.FIXED, const.PUSH_REFUSED_RETRY_SECONDS),
        (404, push.WAITING, push.FIXED, const.PUSH_WAITING_RETRY_SECONDS),
        (409, push.WAITING, push.FIXED, const.PUSH_WAITING_RETRY_SECONDS),
        (422, push.REJECTED, push.UNTIL_CHANGED, 0.0),
        (429, push.RETRYING, push.FIXED, const.PUSH_RATE_LIMITED_RETRY_SECONDS),
        (500, push.RETRYING, push.BACKOFF, 0.0),
        (502, push.RETRYING, push.BACKOFF, 0.0),
        (503, push.RETRYING, push.BACKOFF, 0.0),
        (418, push.RETRYING, push.BACKOFF, 0.0),
        (None, push.RETRYING, push.BACKOFF, 0.0),
    ],
)
def test_classify_each_reply(status, state, schedule, delay):
    outcome = push.classify(status, b"")
    assert (outcome.state, outcome.schedule, outcome.delay) == (state, schedule, delay)


def test_a_rejection_shows_the_integrations_reason():
    body = json.dumps(
        {"code": "invalid_zones", "message": "FeatureCollection has 11000 total vertices"}
    )
    assert push.classify(422, body.encode()).detail == "FeatureCollection has 11000 total vertices"


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"[1,2]",
        b'{"message": 5}',
        b'{"message": ""}',
        b'{"code": "x"}',
        b"\xff\xfe",
    ],
)
def test_a_rejection_without_a_usable_reason_says_so_plainly(body):
    assert push.classify(422, body).detail == "The integration refused the zones."


def test_the_reason_is_one_short_printable_line():
    long = "x" * 500
    assert (
        len(push.clean_message(json.dumps({"message": long}).encode()))
        == const.PUSH_DETAIL_MAX_CHARS
    )
    flat = push.clean_message(json.dumps({"message": "a\nb\x00c\td"}).encode())
    assert flat == "a b c d"


# --- backoff --------------------------------------------------------------


@pytest.mark.parametrize(
    ("attempt", "base"), [(0, 2), (1, 2), (2, 4), (3, 8), (4, 16), (5, 32), (6, 60), (20, 60)]
)
def test_backoff_doubles_to_the_cap(attempt, base):
    assert push.backoff_delay(attempt, lambda: 0.5) == pytest.approx(base)


def test_backoff_jitter_stays_within_a_fifth_either_side():
    assert push.backoff_delay(1, lambda: 0.0) == pytest.approx(1.6)
    assert push.backoff_delay(1, lambda: 1.0) == pytest.approx(2.4)
    assert push.backoff_delay(1) > 0  # the default random source


# --- switched off ---------------------------------------------------------


def test_off_when_the_option_is_false(env):
    opener = Opener()
    pusher = make(opener, {"push_to_core": False})
    assert pusher.enabled is False
    assert pusher.run_once() is None
    assert pusher.status()["state"] == push.OFF
    assert opener.requests == []


def test_off_without_a_supervisor_token(env, monkeypatch):
    monkeypatch.delenv(const.SUPERVISOR_TOKEN_ENV, raising=False)
    pusher = push.Pusher({}, etag_for=etag_for)
    assert pusher.enabled is False
    assert pusher.status()["state"] == push.OFF


def test_the_token_comes_from_the_environment_by_default(env, monkeypatch):
    monkeypatch.setenv(const.SUPERVISOR_TOKEN_ENV, TOKEN)
    assert push.Pusher({}, etag_for=etag_for).enabled is True


def test_start_when_off_starts_no_thread_and_says_why(env, monkeypatch, caplog):
    def no_thread(*_a, **_k):
        raise AssertionError("a thread was started")

    monkeypatch.setattr(push.threading, "Thread", no_thread)
    with caplog.at_level(logging.INFO, logger=push.__name__):
        make(Opener(), {"push_to_core": False}).start()
    assert "Not sending zones" in caplog.text


def test_start_runs_one_daemon_thread(env, monkeypatch):
    seen = {}

    class FakeThread:
        def __init__(self, *, target, name, daemon):
            seen.update(target=target, name=name, daemon=daemon)

        def start(self):
            seen["started"] = True

    monkeypatch.setattr(push.threading, "Thread", FakeThread)
    pusher = make(Opener())
    pusher.start()
    assert seen == {"target": pusher._loop, "name": "push", "daemon": True, "started": True}


def test_wake_sets_the_event_without_blocking(env):
    pusher = make(Opener())
    pusher.wake()
    assert pusher._event.is_set()


# --- delivering -----------------------------------------------------------


def test_a_delivery_posts_the_exact_bytes_with_the_supervisor_token(env):
    opener = Opener((200, b'{"code":"ok"}'))
    pusher = make(opener)
    assert pusher.run_once() == const.PUSH_RESYNC_SECONDS
    request, timeout = opener.requests[0]
    assert request.full_url == const.PUSH_ENDPOINT
    assert request.get_method() == "POST"
    assert request.data == env.read_bytes()
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert request.get_header("Content-type") == "application/json"
    assert timeout == const.PUSH_TIMEOUT_SECONDS
    assert opener.last.read_limit == const.PUSH_REPLY_LIMIT_BYTES
    status = pusher.status()
    assert status["state"] == push.DELIVERED
    assert status["in_sync"] is True
    assert status["detail"] is None
    assert status["delivered_at"].endswith("Z")


def test_the_delivered_version_survives_a_restart_but_is_still_sent_once(env):
    first = make(Opener((200, b"{}")))
    first.run_once()
    stored = json.loads(Path(const.PUSH_STATE_FILE).read_text())
    assert stored["delivered_etag"] == etag_for(env.read_bytes())

    opener = Opener((200, b"{}"))
    second = make(opener)
    assert second.status()["in_sync"] is True  # remembered
    second.run_once()
    assert len(opener.requests) == 1  # the first run after a start always sends
    second.run_once()
    assert len(opener.requests) == 1  # then only on a change


def test_nothing_is_sent_again_until_the_zones_change(env):
    opener = Opener((200, b"{}"), (200, b"{}"))
    pusher = make(opener)
    pusher.run_once()
    wait = pusher.run_once()
    assert len(opener.requests) == 1
    assert wait == pytest.approx(const.PUSH_RESYNC_SECONDS)
    env.write_bytes(b'{"type":"FeatureCollection","features":[],"x":1}')
    assert pusher.status()["state"] == push.PENDING
    assert pusher.status()["in_sync"] is False
    pusher.run_once()
    assert len(opener.requests) == 2
    assert pusher.status()["state"] == push.DELIVERED


def test_the_zones_are_sent_again_after_the_resync_interval(env):
    clock = Clock()
    opener = Opener((200, b"{}"), (200, b"{}"))
    pusher = make(opener, clock=clock)
    pusher.run_once()
    clock.now += const.PUSH_RESYNC_SECONDS - 1
    pusher.run_once()
    assert len(opener.requests) == 1
    clock.now += 2
    pusher.run_once()
    assert len(opener.requests) == 2


def test_the_latest_file_is_read_at_send_time_not_queued(env):
    seen = []

    def opener(request, timeout):
        seen.append(request.data)
        return Reply(200, b"{}")

    pusher = make(opener)
    env.write_bytes(b'{"type":"FeatureCollection","features":[],"v":1}')
    env.write_bytes(b'{"type":"FeatureCollection","features":[],"v":2}')
    pusher.run_once()
    assert seen == [b'{"type":"FeatureCollection","features":[],"v":2}']


# --- refusals -------------------------------------------------------------


def test_a_refusal_is_final_for_those_bytes_and_clears_when_they_change(env):
    body = json.dumps({"message": "FeatureCollection has 11000 total vertices"}).encode()
    opener = Opener((422, body), (200, b"{}"))
    pusher = make(opener)
    assert pusher.run_once() is None
    status = pusher.status()
    assert status["state"] == push.REJECTED
    assert status["detail"] == "FeatureCollection has 11000 total vertices"
    assert pusher.run_once() is None
    assert len(opener.requests) == 1  # not retried
    env.write_bytes(b'{"type":"FeatureCollection","features":[],"x":2}')
    assert pusher.status()["state"] == push.PENDING  # the refused version is gone
    pusher.run_once()
    assert len(opener.requests) == 2
    assert pusher.status()["state"] == push.DELIVERED


@pytest.mark.parametrize(
    ("code", "state", "delay"),
    [
        (401, push.REJECTED, const.PUSH_REFUSED_RETRY_SECONDS),
        (404, push.WAITING, const.PUSH_WAITING_RETRY_SECONDS),
        (409, push.WAITING, const.PUSH_WAITING_RETRY_SECONDS),
        (429, push.RETRYING, const.PUSH_RATE_LIMITED_RETRY_SECONDS),
    ],
)
def test_fixed_delay_replies_ask_again_after_their_delay(env, code, state, delay):
    pusher = make(Opener((code, b"")))
    assert pusher.run_once() == delay
    assert pusher.status()["state"] == state


def test_waiting_ends_when_the_integration_accepts(env):
    pusher = make(Opener((409, b'{"code":"no_addon_entry"}'), (200, b"{}")))
    pusher.run_once()
    assert pusher.status()["state"] == push.WAITING
    pusher.run_once()
    assert pusher.status()["state"] == push.DELIVERED


# --- transient failures ---------------------------------------------------


def test_transient_failures_back_off_and_reset_on_success(env):
    opener = Opener(urllib.error.URLError("down"), (502, b""), (500, b""), (200, b"{}"), (502, b""))
    pusher = make(opener)
    assert pusher.run_once() == pytest.approx(2.0)
    assert pusher.run_once() == pytest.approx(4.0)
    assert pusher.run_once() == pytest.approx(8.0)
    assert pusher.status()["state"] == push.RETRYING
    pusher.run_once()  # delivered
    env.write_bytes(b'{"type":"FeatureCollection","features":[],"x":3}')
    assert pusher.run_once() == pytest.approx(2.0)  # the counter started over


@pytest.mark.parametrize(
    "error", [OSError("boom"), TimeoutError(), http.client.RemoteDisconnected("gone")]
)
def test_any_connection_error_is_a_retry(env, error):
    pusher = make(Opener(error))
    assert pusher.run_once() == pytest.approx(2.0)
    assert pusher.status()["state"] == push.RETRYING


def test_an_error_reply_whose_body_cannot_be_read_still_counts(env):
    class Broken(io.BytesIO):
        def read(self, *_a):
            raise OSError("reset")

    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 409, "x", {}, Broken())

    pusher = make(opener)
    assert pusher.run_once() == const.PUSH_WAITING_RETRY_SECONDS
    assert pusher.status()["state"] == push.WAITING


def test_a_huge_or_garbage_reply_is_cut_off_and_survived(env):
    pusher = make(Opener((422, b"x" * (const.PUSH_REPLY_LIMIT_BYTES * 2))))
    pusher.run_once()
    assert pusher.status()["detail"] == "The integration refused the zones."


def test_an_unreadable_zones_file_is_a_retry_not_a_crash(env):
    opener = Opener()
    pusher = make(opener)
    env.unlink()
    assert pusher.run_once() == const.PUSH_WAITING_RETRY_SECONDS
    assert pusher.status()["state"] == push.RETRYING
    assert pusher.status()["in_sync"] is False
    assert opener.requests == []


# --- logging --------------------------------------------------------------


def test_each_state_is_logged_once_not_per_retry(env, caplog):
    opener = Opener((502, b""), (502, b""), (502, b""), (200, b"{}"), (404, b""), (404, b""))
    pusher = make(opener)
    with caplog.at_level(logging.INFO, logger=push.__name__):
        for _ in range(4):
            pusher.run_once()
        env.write_bytes(b'{"type":"FeatureCollection","features":[],"x":4}')
        pusher.run_once()
        env.write_bytes(b'{"type":"FeatureCollection","features":[],"x":5}')
        pusher.run_once()
    lines = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == push.__name__]
    assert [m.split(":")[0].split(" (")[0] for _, m in lines] == [
        "Zones not delivered to Home Assistant",
        "Zones delivered to Home Assistant.",
        "Waiting for the integration to accept zones",
    ]
    assert lines[0][0] == logging.WARNING


def test_the_token_never_reaches_a_log_line_or_the_status(env, caplog):
    echoed = json.dumps({"message": f"bad bearer {TOKEN}"}).encode()
    pusher = make(Opener((401, echoed), (502, b""), (422, echoed), (200, b"{}")))
    with caplog.at_level(logging.DEBUG, logger=push.__name__):
        for n in range(4):
            env.write_bytes(b'{"type":"FeatureCollection","features":[],"n":%d}' % n)
            pusher.run_once()
            assert TOKEN not in json.dumps(pusher.status())
    assert TOKEN not in caplog.text
    assert "Authorization" not in caplog.text


def test_a_token_echoed_by_the_integration_is_blanked_wherever_it_is_shown(env):
    body = json.dumps({"message": f"unknown bearer {TOKEN} given"}).encode()
    pusher = make(Opener((422, body)))
    pusher.run_once()
    assert pusher.status()["detail"] == "unknown bearer *** given"


# --- hostile or broken replies and servers -------------------------------


def test_deeply_nested_garbage_in_a_reply_is_survived():
    assert push.clean_message(b"[" * 65536) is None
    outcome = push.classify(422, b"[" * 65536)
    assert outcome.state == push.REJECTED
    assert outcome.detail == "The integration refused the zones."


def test_an_unexpected_error_cannot_end_the_delivery_thread(env, caplog):
    pusher = make(Opener())

    def boom():
        raise RuntimeError(f"surprise with {TOKEN}")

    pusher.run_once = boom
    with caplog.at_level(logging.WARNING, logger=push.__name__):
        assert [pusher._guarded_run() for _ in range(3)] == [const.PUSH_BACKOFF_MAX_SECONDS] * 3
    assert pusher.status()["state"] == push.RETRYING
    assert pusher.status()["detail"] == "Unexpected RuntimeError while sending the zones."
    assert len([r for r in caplog.records if r.name == push.__name__]) == 1  # once, not per try
    assert "RuntimeError" in caplog.text
    assert TOKEN not in caplog.text  # only the error's type is logged


def test_a_token_cut_by_the_length_cap_leaves_no_prefix(env):
    message = "x" * 190 + TOKEN
    pusher = make(Opener((422, json.dumps({"message": message}).encode())))
    pusher.run_once()
    detail = pusher.status()["detail"]
    assert TOKEN[:10] not in detail
    assert "***" in detail


def test_a_state_file_that_recurses_too_deep_is_ignored(env):
    Path(const.PUSH_STATE_FILE).write_text("[" * 100000)
    assert make(Opener()).status()["delivered_at"] is None


def test_a_non_waiting_failure_resets_the_waiting_count(env):
    opener = Opener(*[(409, b"")] * 10, (502, b""), (409, b""))
    pusher = make(opener)
    for n in range(10):
        env.write_bytes(b'{"type":"FeatureCollection","features":[],"n":%d}' % n)
        pusher.run_once()
    env.write_bytes(b'{"type":"FeatureCollection","features":[],"n":10}')
    pusher.run_once()  # a transient failure in between
    env.write_bytes(b'{"type":"FeatureCollection","features":[],"n":11}')
    assert pusher.run_once() == const.PUSH_WAITING_RETRY_SECONDS  # counting began again


@pytest.mark.parametrize("returned", [None, 12.5])
def test_a_normal_run_passes_through_the_guard(env, returned):
    pusher = make(Opener())
    pusher.run_once = lambda: returned
    assert pusher._guarded_run() == returned


class Recorder:
    """A local web server that records what reaches it, answering with a fixed reply."""

    def __init__(self, status=200, location=None, body=b"{}"):
        import http.server
        import threading

        outer = self
        outer.seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def handle_any(self):
                outer.seen.append(dict(self.headers))
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_POST = do_GET = handle_any

            def log_message(self, *_args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/x"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def servers():
    made = []

    def make_one(**kw):
        made.append(Recorder(**kw))
        return made[-1]

    yield make_one
    for each in made:
        each.close()


def test_a_redirect_is_not_followed_and_the_token_goes_nowhere_else(env, monkeypatch, servers):
    elsewhere = servers()
    redirecting = servers(status=302, location=elsewhere.url)
    monkeypatch.setattr(const, "PUSH_ENDPOINT", redirecting.url)
    pusher = push.Pusher({}, etag_for=etag_for, token=TOKEN, opener=push.OPEN)
    assert pusher.run_once() == pytest.approx(2.0, abs=0.5)  # a retry, never a delivery
    assert pusher.status()["state"] == push.RETRYING
    assert len(redirecting.seen) == 1
    assert elsewhere.seen == []


def test_the_redirect_check_can_fail(servers):
    """Control: plain urlopen carries the token to the redirect target."""
    elsewhere = servers()
    redirecting = servers(status=302, location=elsewhere.url)
    request = urllib.request.Request(
        redirecting.url, data=b"{}", method="POST", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    urllib.request.urlopen(request, timeout=5)
    assert any(h.get("Authorization") == f"Bearer {TOKEN}" for h in elsewhere.seen)


def test_the_opener_ignores_environment_proxies_and_refuses_redirects(monkeypatch):
    monkeypatch.setenv("http_proxy", "http://203.0.113.9:3128")
    monkeypatch.setenv("https_proxy", "http://203.0.113.9:3128")
    opener = push.build_opener()
    # An empty ProxyHandler is dropped by urllib; the default one, built from
    # the environment, would be present and would route the token to the proxy.
    assert [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)] == []
    redirects = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
    assert [type(h).__name__ for h in redirects] == ["_NoRedirect"]


# --- persisted state ------------------------------------------------------


@pytest.mark.parametrize(
    "stored",
    ["not json", "[1]", '{"delivered_etag": 5, "delivered_at": ["x"]}', "{}"],
)
def test_a_damaged_state_file_means_nothing_delivered_yet(env, stored):
    with open(const.PUSH_STATE_FILE, "w") as f:
        f.write(stored)
    status = make(Opener()).status()
    assert status["in_sync"] is False
    assert status["delivered_at"] is None


def test_a_missing_state_file_means_nothing_delivered_yet(env):
    assert make(Opener()).status()["delivered_at"] is None


def test_failing_to_record_a_delivery_is_not_fatal(env, monkeypatch, caplog):
    def broken(*_a, **_k):
        raise OSError("read-only")

    monkeypatch.setattr(push, "atomic_write_json", broken)
    pusher = make(Opener((200, b"{}")))
    with caplog.at_level(logging.WARNING, logger=push.__name__):
        pusher.run_once()
    assert pusher.status()["state"] == push.DELIVERED
    assert "sent again on restart" in caplog.text


# --- wired into the app ---------------------------------------------------


class FakePusher:
    def __init__(self):
        self.wakes = 0

    def wake(self):
        self.wakes += 1

    def status(self):
        return {"state": push.OFF, "detail": None, "delivered_at": None, "in_sync": False}


@pytest.fixture
def app_with(tmp_zones_file):
    import main

    def build(pusher=None, options=None):
        return main.generate_app(options or {}, pusher=pusher)

    return build


VALID = {"type": "FeatureCollection", "features": []}


def test_a_save_wakes_the_pusher(app_with):
    fake = FakePusher()
    client = TestClient(app_with(fake), client=INGRESS)
    assert client.post("/save_zones", json=VALID).status_code == 200
    assert fake.wakes == 1


def test_a_refused_save_does_not_wake_the_pusher(app_with):
    fake = FakePusher()
    client = TestClient(app_with(fake), client=INGRESS)
    assert client.post("/save_zones", json={"type": "nope"}).status_code == 422
    assert fake.wakes == 0


def test_push_status_reports_the_pusher_and_is_never_cached(app_with):
    client = TestClient(app_with(FakePusher()), client=INGRESS)
    reply = client.get("/push_status")
    assert reply.status_code == 200
    assert reply.json() == {"state": "off", "detail": None, "delivered_at": None, "in_sync": False}
    assert reply.headers["cache-control"] == "no-store"


def test_push_status_is_gated_like_the_other_data(app_with):
    outsider = TestClient(app_with(FakePusher(), {"save_token": "s3cret"}))
    assert outsider.get("/push_status").status_code == 403
    assert outsider.get("/push_status", headers={"X-Save-Token": "s3cret"}).status_code == 200


def test_an_app_built_without_a_pusher_reports_off(app_with, monkeypatch):
    monkeypatch.delenv(const.SUPERVISOR_TOKEN_ENV, raising=False)
    client = TestClient(app_with(), client=INGRESS)
    assert client.get("/push_status").json()["state"] == "off"


# --- the limits the integration also enforces -----------------------------


def polygon(vertices, name, offset=0.0):
    ring = [[offset + i * 1e-6, (i % 2) * 1e-6] for i in range(vertices - 1)]
    ring.append(ring[0])
    return {
        "type": "Feature",
        "properties": {"name": name},
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


def collection(*sizes):
    return {
        "type": "FeatureCollection",
        "features": [polygon(n, f"z{i}") for i, n in enumerate(sizes)],
    }


def post(client, body):
    return client.post("/save_zones", json=body)


def test_exactly_500_zones_save_and_501_do_not(app_with):
    client = TestClient(app_with(), client=INGRESS)
    assert post(client, collection(*[4] * 500)).status_code == 200
    refused = post(client, collection(*[4] * 501))
    assert refused.status_code == 422
    assert "501 zones exceeds the cap of 500" in refused.json()["detail"]


def test_exactly_10000_vertices_in_total_save_and_10001_do_not(app_with):
    client = TestClient(app_with(), client=INGRESS)
    at_cap = collection(*([1000] * 9 + [996, 4]))
    assert post(client, at_cap).status_code == 200
    over = post(client, collection(*([1000] * 9 + [996, 5])))
    assert over.status_code == 422
    assert "more than 10000 vertices in total" in over.json()["detail"]


# --- sequences the reviews found ------------------------------------------


def test_the_loop_clears_first_so_a_save_during_a_send_is_not_lost(env):
    import time

    pusher = make(Opener())
    pusher._event.set()  # a save before this turn
    pusher.run_once = lambda: 0.2
    started = time.monotonic()
    pusher._cycle()
    assert time.monotonic() - started >= 0.15  # cleared, so it really waited

    def save_during_send():
        pusher.wake()
        return 5.0

    pusher.run_once = save_during_send
    started = time.monotonic()
    pusher._cycle()
    assert time.monotonic() - started < 2.0  # the wake that landed mid-send ended the wait


def test_reverting_to_refused_zones_shows_the_refusal_again(env):
    body = json.dumps({"message": "too many vertices"}).encode()
    pusher = make(Opener((422, body), (502, b"")))
    refused = env.read_bytes()
    pusher.run_once()
    env.write_bytes(b'{"type":"FeatureCollection","features":[],"b":1}')
    pusher.run_once()
    assert pusher.status()["state"] == push.RETRYING
    env.write_bytes(refused)
    assert pusher.run_once() is None
    assert pusher.status()["state"] == push.REJECTED
    assert pusher.status()["detail"] == "too many vertices"


def test_a_missing_integration_entry_cancels_the_last_delivery(env):
    pusher = make(Opener((200, b"{}"), (409, b"")))
    delivered = env.read_bytes()
    pusher.run_once()
    env.write_bytes(b'{"type":"FeatureCollection","features":[],"b":1}')
    pusher.run_once()
    assert pusher.status()["state"] == push.WAITING
    env.write_bytes(delivered)
    assert pusher.status()["state"] != push.DELIVERED
    assert pusher.status()["in_sync"] is False


def test_going_back_to_what_the_integration_still_holds_is_sent(env):
    opener = Opener((200, b"{}"), (502, b""))
    pusher = make(opener)
    first = env.read_bytes()
    pusher.run_once()
    env.write_bytes(b'{"type":"FeatureCollection","features":[],"b":1}')
    pusher.run_once()
    assert pusher.status()["state"] == push.RETRYING
    env.write_bytes(first)
    assert pusher.run_once() == pytest.approx(const.PUSH_RESYNC_SECONDS, rel=0.01)
    assert len(opener.requests) == 2  # nothing more to send: it holds these already
    assert pusher.status()["state"] == push.DELIVERED


@pytest.mark.parametrize("code", [200, 201, 202, 204, 299])
def test_any_2xx_counts_as_delivered(code):
    assert push.classify(code, b"").state == push.DELIVERED


@pytest.mark.parametrize("code", [199, 300, 302, 399])
def test_codes_outside_2xx_never_count(code):
    assert push.classify(code, b"").state == push.RETRYING


def test_waiting_slows_down_after_ten_asks_and_a_delivery_resets_it(env):
    opener = Opener(*[(409, b"")] * 12, (200, b"{}"), (409, b""))
    pusher = make(opener)
    delays = []
    for n in range(12):
        env.write_bytes(b'{"type":"FeatureCollection","features":[],"n":%d}' % n)
        delays.append(pusher.run_once())
    assert delays[:10] == [const.PUSH_WAITING_RETRY_SECONDS] * 10
    assert delays[10:] == [const.PUSH_WAITING_SLOW_RETRY_SECONDS] * 2
    env.write_bytes(b'{"type":"FeatureCollection","features":[],"n":99}')
    pusher.run_once()  # delivered
    env.write_bytes(b'{"type":"FeatureCollection","features":[],"n":100}')
    assert pusher.run_once() == const.PUSH_WAITING_RETRY_SECONDS  # fast again
