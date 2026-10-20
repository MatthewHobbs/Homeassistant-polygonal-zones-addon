"""Who a request is trusted as, and which requests may write.

The boot tests run main.py as __main__ with uvicorn.run captured, then load
the captured app through a real uvicorn.Config, so they exercise whatever
uvicorn itself would wrap around the app (its ProxyHeadersMiddleware when
proxy_headers is on), not a hand-assembled stack.
"""

import asyncio
import json
import logging
import runpy

import pytest
from starlette.testclient import TestClient
import uvicorn

from tests.conftest import APP_DIR

INGRESS = "172.30.32.2"
PROXY_RANGE = "192.168.1.0/24"
HOST_IN_PROXY_RANGE = ("192.168.1.50", 5000)
TOKEN = "s3cret-token-value"


def _valid_payload():
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "Home"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
                },
            }
        ],
    }


@pytest.fixture
def boot(tmp_path, tmp_zones_file, tmp_options_file, monkeypatch):
    """Run main.py's startup with the given options; return the served app."""
    import const

    monkeypatch.setattr(const, "DATA_FOLDER", str(tmp_path))
    root = logging.getLogger()
    # main.py's configure_logging changes the root logger; undo it afterwards.
    monkeypatch.setattr(root, "level", root.level)
    monkeypatch.setattr(root, "handlers", list(root.handlers))

    def _boot(options: dict):
        tmp_options_file.write_text(json.dumps(options))
        captured = {}

        def fake_run(app, **kwargs):
            captured["app"] = app
            captured["kwargs"] = kwargs

        monkeypatch.setattr(uvicorn, "run", fake_run)
        runpy.run_path(str(APP_DIR / "main.py"), run_name="__main__")
        _boot.run_kwargs = captured["kwargs"]

        kwargs = {
            k: v
            for k, v in captured["kwargs"].items()
            if k in ("proxy_headers", "forwarded_allow_ips")
        }
        config = uvicorn.Config(captured["app"], log_config=None, **kwargs)
        config.load()
        return config.loaded_app

    return _boot


def _served_client(boot, options, client):
    return TestClient(boot(options), client=client)


# --- Ingress is decided by the transport peer --------------------------------


def test_forwarded_header_from_trusted_range_is_not_ingress(boot):
    client = _served_client(
        boot,
        {"save_token": TOKEN, "trusted_proxies": PROXY_RANGE},
        HOST_IN_PROXY_RANGE,
    )
    xff = {"X-Forwarded-For": INGRESS}

    assert client.post("/save_zones", json=_valid_payload(), headers=xff).status_code == 403
    assert client.get("/zones.json", headers=xff).status_code == 403
    assert client.get("/trackers.json", headers=xff).status_code == 403


def test_real_ingress_peer_is_still_ingress_with_proxies_configured(boot):
    client = _served_client(
        boot,
        {"save_token": TOKEN, "trusted_proxies": PROXY_RANGE},
        (INGRESS, 5000),
    )

    assert client.post("/save_zones", json=_valid_payload()).status_code == 200
    assert client.get("/zones.json").status_code == 200
    assert client.get("/trackers.json").status_code == 200


def test_trusted_proxy_still_resolves_the_forwarded_client(boot, caplog):
    client = _served_client(
        boot,
        {"save_token": TOKEN, "trusted_proxies": PROXY_RANGE},
        HOST_IN_PROXY_RANGE,
    )
    with caplog.at_level(logging.WARNING):
        r = client.get("/zones.json", headers={"X-Forwarded-For": "10.9.8.7"})

    assert r.status_code == 403
    # Rate limiting and logging key on the resolved client, not the proxy.
    assert "Rejected /zones.json from 10.9.8.7" in caplog.text
    assert "192.168.1.50" not in caplog.text

    r = client.get("/zones.json", headers={"X-Forwarded-For": "10.9.8.7", "X-Save-Token": TOKEN})
    assert r.status_code == 200


def test_forwarded_header_ignored_without_trusted_proxies(boot):
    client = _served_client(boot, {"save_token": TOKEN}, HOST_IN_PROXY_RANGE)
    assert client.get("/zones.json", headers={"X-Forwarded-For": INGRESS}).status_code == 403


def test_uvicorn_access_log_is_off(boot):
    # It would log every client's IP and request line at INFO.
    boot({})
    assert boot.run_kwargs["access_log"] is False
    assert "log_config" not in boot.run_kwargs


def test_uvicorn_adds_no_proxy_handling_of_its_own(boot):
    # The boot runs main.py afresh, so its class is not main's: compare names.
    served = boot({"trusted_proxies": PROXY_RANGE})
    assert type(served).__name__ == "TransportPeerMiddleware"


def test_app_without_peer_record_is_never_ingress(tmp_zones_file):
    """The Starlette app alone, without the outer layer, fails closed."""
    import main

    served = main.generate_app({"save_token": TOKEN})
    client = TestClient(served.app, client=(INGRESS, 5000))
    assert client.get("/zones.json").status_code == 403


def test_lifespan_passes_through_the_peer_layer(tmp_zones_file):
    import main

    served = main.generate_app({"trusted_proxies": "10.0.0.5"})
    with TestClient(served, client=(INGRESS, 5000)) as client:
        assert client.get("/zones.json").status_code == 200


# --- Cross-site writes to /save_zones ----------------------------------------


@pytest.fixture
def open_client(tmp_zones_file):
    import main

    return TestClient(main.generate_app({}), client=(INGRESS, 5000))


def test_save_refuses_non_json_media_type(open_client, tmp_zones_file):
    original = tmp_zones_file.read_text()
    r = open_client.post(
        "/save_zones",
        content=json.dumps(_valid_payload()),
        headers={"Content-Type": "text/plain"},
    )
    assert r.status_code == 415
    assert tmp_zones_file.read_text() == original


def test_save_refuses_missing_content_type(open_client, tmp_zones_file):
    r = open_client.post("/save_zones", content=json.dumps(_valid_payload()))
    assert r.status_code == 415


def test_save_media_type_checked_before_body_is_read(open_client, monkeypatch):
    from starlette.requests import Request

    async def must_not_read(self):
        raise AssertionError("body read before media type check")

    monkeypatch.setattr(Request, "body", must_not_read)
    r = open_client.post(
        "/save_zones",
        content=b"{}",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 415


@pytest.mark.parametrize(
    "content_type", ["application/json", "application/json; charset=utf-8", "Application/JSON"]
)
def test_save_accepts_json_media_type_with_parameters(open_client, content_type):
    r = open_client.post(
        "/save_zones",
        content=json.dumps(_valid_payload()),
        headers={"Content-Type": content_type},
    )
    assert r.status_code == 200


@pytest.mark.parametrize("site", ["cross-site", "same-site", ""])
def test_save_refuses_requests_not_from_the_same_origin(open_client, tmp_zones_file, site):
    original = tmp_zones_file.read_text()
    r = open_client.post("/save_zones", json=_valid_payload(), headers={"Sec-Fetch-Site": site})
    assert r.status_code == 403
    assert r.json() == {"error": "not authorised"}
    assert r.headers["cache-control"] == "no-store"
    assert tmp_zones_file.read_text() == original


@pytest.mark.parametrize("site", ["same-origin", "none"])
def test_save_accepts_same_origin_and_user_initiated(open_client, site):
    r = open_client.post("/save_zones", json=_valid_payload(), headers={"Sec-Fetch-Site": site})
    assert r.status_code == 200


def test_cross_site_refusal_ignores_a_valid_token_and_spends_no_budget(tmp_zones_file):
    import main

    app = main.generate_app({"save_token": TOKEN})
    client = TestClient(app)
    headers = {"Sec-Fetch-Site": "cross-site", "X-Save-Token": TOKEN}
    for _ in range(main._AUTH_FAILURE_LIMIT + 1):
        assert client.post("/save_zones", json=_valid_payload(), headers=headers).status_code == 403
    # Refusals of browser cross-site requests must not lock the client out.
    r = client.post("/save_zones", json=_valid_payload(), headers={"X-Save-Token": TOKEN})
    assert r.status_code == 200


# --- Startup log --------------------------------------------------------------


def test_startup_options_log_is_an_allowlist(boot, caplog):
    options = {
        "allow_all_ips": False,
        "zone_colour": "purple",
        "log_level": "info",
        "theme": "dark",
        "tracker_refresh_seconds": 60,
        "save_token": TOKEN,
        "overlay_entities": ["device_tracker.private_car", "person.someone"],
        "trusted_proxies": "10.0.0.5, 10.0.0.6",
        "unexpected_extra_option": "value-that-must-not-be-logged",
    }
    with caplog.at_level(logging.INFO):
        boot(options)

    text = caplog.text
    assert "Loaded options" in text
    for secret in (
        TOKEN,
        "unexpected_extra_option",
        "value-that-must-not-be-logged",
        "device_tracker.private_car",
        "person.someone",
        "10.0.0.5",
        "10.0.0.6",
    ):
        assert secret not in text, secret


def test_loggable_options_shape():
    import main

    assert main.loggable_options(
        {
            "allow_all_ips": True,
            "zone_colour": "purple",
            "save_token": TOKEN,
            "overlay_entities": "device_tracker.a, device_tracker.b,\n",
            "trusted_proxies": "10.0.0.5, ,10.0.0.6",
            "extra": "x",
        }
    ) == {
        "allow_all_ips": True,
        "zone_colour": "purple",
        "save_token": "***",
        "overlay_entities": 2,
        "trusted_proxies": 2,
    }
    assert main.loggable_options(
        {"save_token": "  ", "overlay_entities": ["a.b", " ", "c.d"], "trusted_proxies": None}
    ) == {"save_token": "not set", "overlay_entities": 2, "trusted_proxies": 0}
    assert main.loggable_options({"overlay_entities": {"not": "a list"}})["overlay_entities"] == 0


def test_first_boot_seeds_the_zones_file_atomically(boot, tmp_zones_file, monkeypatch):
    import helpers

    real_write = helpers.atomic_write_json
    seen = []
    monkeypatch.setattr(
        helpers, "atomic_write_json", lambda path, data: seen.append(path) or real_write(path, data)
    )
    tmp_zones_file.unlink()
    boot({})
    assert seen == [str(tmp_zones_file)]
    assert json.loads(tmp_zones_file.read_text()) == {"type": "FeatureCollection", "features": []}


# --- Startup log for the retired allow_all_ips --------------------------------


@pytest.mark.parametrize("token", ["", TOKEN])
def test_allow_all_ips_logs_one_error_at_startup(boot, caplog, token):
    with caplog.at_level(logging.INFO):
        boot({"allow_all_ips": True, "save_token": token})
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert "allow_all_ips no longer grants LAN access" in errors[0]
    assert "save_token" in errors[0]


@pytest.mark.parametrize("options", [{}, {"allow_all_ips": False}])
def test_no_allow_all_ips_error_when_it_is_off(boot, caplog, options):
    with caplog.at_level(logging.INFO):
        boot(options)
    assert "allow_all_ips" not in " ".join(
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    )


def test_peer_layer_records_none_without_a_client():
    import main

    seen = {}

    async def inner(scope, receive, send):
        seen.update(scope)

    asyncio.run(main.TransportPeerMiddleware(inner)({"type": "http"}, None, None))
    assert seen[main.TRANSPORT_PEER_SCOPE_KEY] is None


@pytest.mark.parametrize("path", ["/zones.json", "/trackers.json"])
def test_forged_ingress_failures_cannot_rate_limit_real_ingress(boot, path):
    options = {"trusted_proxies": PROXY_RANGE, "save_token": TOKEN}
    forged = _served_client(boot, options, HOST_IN_PROXY_RANGE)
    for _ in range(12):
        assert forged.get(path, headers={"X-Forwarded-For": INGRESS}).status_code in (403, 429)
    ingress = TestClient(forged.app, client=(INGRESS, 5000))
    assert ingress.get(path).status_code == 200


def test_expired_failure_records_are_swept(tmp_zones_file, monkeypatch):
    import main

    app = main.generate_app({"trusted_proxies": PROXY_RANGE, "save_token": TOKEN})
    client = TestClient(app, client=HOST_IN_PROXY_RANGE)
    monkeypatch.setattr(main, "_AUTH_FAILURE_MAX_KEYS", 5)
    clock = [1_000_000.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: clock[0])
    for i in range(1, 30):
        client.get("/zones.json", headers={"X-Forwarded-For": f"192.0.2.{i}"})
    assert len(main._auth_failures) == 29
    clock[0] += 61
    client.get("/zones.json", headers={"X-Forwarded-For": "192.0.2.200"})
    # The 29 expired clients are gone; only the newest failure remains.
    assert list(main._auth_failures) == ["192.0.2.200"]


def test_failure_record_sweep_runs_at_most_once_a_second(request_factory, monkeypatch):
    import main

    monkeypatch.setattr(main, "_AUTH_FAILURE_MAX_KEYS", 2)
    clock = [1_000_000.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: clock[0])
    sweeps = []
    real_prune = main._prune
    monkeypatch.setattr(
        main, "_prune", lambda key, cutoff: sweeps.append(clock[0]) or real_prune(key, cutoff)
    )

    for i in range(50):
        main._record_auth_failure(request_factory(f"192.0.2.{i}"))
    # Past the key limit from the fourth failure on, but swept once: one pass
    # over the three keys held at that point.
    assert sweeps == [1_000_000.0] * 3

    clock[0] += 0.5
    main._record_auth_failure(request_factory("192.0.2.200"))
    assert len(sweeps) == 3

    clock[0] += 0.5
    main._record_auth_failure(request_factory("192.0.2.201"))
    assert sweeps[3:] == [1_000_001.0] * 51


def test_real_ingress_with_forwarded_header_is_still_ingress(boot):
    options = {"trusted_proxies": PROXY_RANGE, "save_token": TOKEN}
    ingress = _served_client(boot, options, (INGRESS, 5000))
    assert ingress.get("/zones.json", headers={"X-Forwarded-For": "192.168.1.9"}).status_code == 200


def test_host_bit_cidr_is_applied_as_its_network(boot, caplog):
    options = {"trusted_proxies": "192.168.1.5/24", "save_token": TOKEN}
    client = _served_client(boot, options, HOST_IN_PROXY_RANGE)
    with caplog.at_level(logging.WARNING):
        client.get("/zones.json", headers={"X-Forwarded-For": "192.0.2.1"})
    # Trusted, so the forwarded client is the one logged, not the proxy.
    assert "from 192.0.2.1" in caplog.text


def test_cross_site_reads_never_test_a_token_or_spend_budget(boot):
    options = {"save_token": TOKEN}
    client = _served_client(boot, options, ("192.168.7.7", 5000))
    cross = {"Sec-Fetch-Site": "cross-site"}
    for path in ("/zones.json", "/trackers.json"):
        for _ in range(15):
            assert client.get(path, headers={**cross, "X-Save-Token": "guess"}).status_code == 403
        # Even the right token is refused when a browser marks it cross-site.
        assert client.get(path, headers={**cross, "X-Save-Token": TOKEN}).status_code == 403
    assert client.get("/zones.json", headers={"X-Save-Token": TOKEN}).status_code == 200


def test_host_bit_cidr_normalisation_is_logged(boot, caplog):
    with caplog.at_level(logging.WARNING):
        boot({"trusted_proxies": "192.168.1.5/24"})
    assert "host bits set" in caplog.text


def _parse_warnings(caplog, trusted_proxies):
    import main

    with caplog.at_level(logging.WARNING):
        kept = main._parse_trusted_proxies({"trusted_proxies": trusted_proxies})
    return kept, [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_each_range_in_trusted_proxies_warns_once(caplog):
    kept, warnings = _parse_warnings(
        caplog, "10.0.0.0/24, 192.168.1.1, 10.1.0.1/32, 2001:db8::/32, 2001:db8::1/128"
    )
    assert kept == ["10.0.0.0/24", "192.168.1.1", "10.1.0.1/32", "2001:db8::/32", "2001:db8::1/128"]
    ranges = [w for w in warnings if "every address in the range" in w]
    assert len(ranges) == 2
    # Constant text: the entry itself is never logged.
    assert not any("10.0.0.0" in w or "2001:db8" in w for w in warnings)


def test_non_canonical_ipv6_spelling_is_not_a_host_bit_entry(caplog):
    _, warnings = _parse_warnings(caplog, "2001:DB8::/32")
    assert not any("host bits set" in w for w in warnings)


def test_host_bit_entry_is_still_flagged(caplog):
    kept, warnings = _parse_warnings(caplog, "192.168.1.5/24")
    assert kept == ["192.168.1.0/24"]
    assert sum("host bits set" in w for w in warnings) == 1


def _send_raw(app, path):
    """The response status for ``path`` sent as a raw ASGI scope, which the
    test client would normalise."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"addon")],
        "client": ("192.168.7.7", 5000),
        "server": ("addon", 8000),
    }
    asyncio.run(app(scope, receive, send))
    return sent[0]["status"]


@pytest.mark.parametrize(
    "path", ["/healthz#/../index.html", "/zones.json?/../js/storage.js", "/healthz?/../index.html"]
)
def test_an_encoded_hash_or_query_does_not_dodge_the_gate(boot, path):
    # uvicorn decodes %23 and %3F into scope["path"], where a URL re-parse
    # reads them as a fragment or query and sees an exempt path.
    assert _send_raw(boot({"save_token": TOKEN}), path) == 403


def test_a_decoded_newline_in_the_path_cannot_forge_a_log_line(boot, caplog):
    # uvicorn decodes %0a into scope["path"], so GET /%0aFAKE arrives as this.
    app = boot({"save_token": TOKEN})
    with caplog.at_level(logging.WARNING):
        statuses = [_send_raw(app, "/\nFAKE") for _ in range(11)]
    assert statuses[-1] == 429
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("Rejected /") for m in messages)
    assert any(m.startswith("Rate limit hit on /") for m in messages)
    assert not any("\n" in m or "\r" in m for m in messages)


def test_cross_site_static_loads_never_test_a_token_or_spend_budget(boot):
    client = _served_client(boot, {"save_token": TOKEN}, ("192.168.7.7", 5000))
    cross = {"Sec-Fetch-Site": "cross-site"}
    for _ in range(15):
        assert client.get("/", headers={**cross, "X-Save-Token": "guess"}).status_code == 403
    assert client.get("/", headers={**cross, "X-Save-Token": TOKEN}).status_code == 403
    assert client.get("/zones.json", headers={"X-Save-Token": TOKEN}).status_code == 200


def test_ingress_loads_the_editor_from_a_cross_site_link(boot):
    client = _served_client(boot, {}, (INGRESS, 5000))
    assert client.get("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200


@pytest.mark.parametrize(
    ("body", "status"),
    [
        (
            b'{"type":"FeatureCollection","features":[{"type":"Feature","properties":{},'
            b'"geometry":{"type":"Polygon","coordinates":[[[1'
            + b"0" * 400
            + b",0],[1,0],[1,1],[1"
            + b"0" * 400
            + b",0]]]}}]}",
            422,
        ),
        (b"1" * 5000, 400),
        (b"[" * 100_000, 400),
    ],
    ids=["huge-int", "too-many-digits", "too-deep"],
)
def test_a_save_body_python_cannot_hold_is_refused_not_a_500(boot, body, status):
    client = _served_client(boot, {"save_token": TOKEN}, (INGRESS, 5000))
    r = client.post("/save_zones", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == status


def test_a_refusal_carries_the_security_headers(boot):
    client = _served_client(boot, {"save_token": TOKEN}, ("192.168.7.7", 5000))
    r = client.get("/")
    assert r.status_code == 403
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("attrs", "accuracy"),
    [
        ({"latitude": 10**400, "longitude": 0}, "dropped"),
        ({"latitude": 91, "longitude": 0}, "dropped"),
        ({"latitude": 1, "longitude": 2, "gps_accuracy": 10**400}, None),
    ],
)
def test_an_out_of_range_tracker_is_dropped_not_a_crash(attrs, accuracy):
    import main

    point = main._overlay_point("device_tracker.x", {"attributes": attrs})
    if accuracy == "dropped":
        assert point is None
    else:
        assert point["gps_accuracy"] is accuracy
