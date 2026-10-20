"""Who a request is trusted as, and which requests may write.

The boot tests run main.py as __main__ with uvicorn.run captured, then load
the captured app through a real uvicorn.Config, so they exercise whatever
uvicorn itself would wrap around the app (its ProxyHeadersMiddleware when
proxy_headers is on), not a hand-assembled stack.
"""

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
        {"allow_all_ips": True, "save_token": TOKEN, "trusted_proxies": PROXY_RANGE},
        HOST_IN_PROXY_RANGE,
    )
    xff = {"X-Forwarded-For": INGRESS}

    assert client.post("/save_zones", json=_valid_payload(), headers=xff).status_code == 403
    assert client.get("/zones.json", headers=xff).status_code == 403
    assert client.get("/trackers.json", headers=xff).status_code == 403


def test_real_ingress_peer_is_still_ingress_with_proxies_configured(boot):
    client = _served_client(
        boot,
        {"allow_all_ips": True, "save_token": TOKEN, "trusted_proxies": PROXY_RANGE},
        (INGRESS, 5000),
    )

    assert client.post("/save_zones", json=_valid_payload()).status_code == 200
    assert client.get("/zones.json").status_code == 200
    assert client.get("/trackers.json").status_code == 200


def test_trusted_proxy_still_resolves_the_forwarded_client(boot, caplog):
    client = _served_client(
        boot,
        {"allow_all_ips": True, "save_token": TOKEN, "trusted_proxies": PROXY_RANGE},
        HOST_IN_PROXY_RANGE,
    )
    with caplog.at_level(logging.WARNING):
        r = client.get("/zones.json", headers={"X-Forwarded-For": "10.9.8.7"})

    assert r.status_code == 403
    # Rate limiting and logging key on the resolved client, not the proxy.
    assert "Rejected /zones.json read from 10.9.8.7" in caplog.text
    assert "192.168.1.50" not in caplog.text

    r = client.get("/zones.json", headers={"X-Forwarded-For": "10.9.8.7", "X-Save-Token": TOKEN})
    assert r.status_code == 200


def test_forwarded_header_ignored_without_trusted_proxies(boot):
    client = _served_client(boot, {"save_token": TOKEN}, HOST_IN_PROXY_RANGE)
    assert client.get("/zones.json", headers={"X-Forwarded-For": INGRESS}).status_code == 403


def test_uvicorn_adds_no_proxy_handling_of_its_own(boot):
    # The boot runs main.py afresh, so its class is not main's: compare names.
    served = boot({"trusted_proxies": PROXY_RANGE})
    assert type(served).__name__ == "TransportPeerMiddleware"


def test_app_without_peer_record_is_never_ingress(tmp_zones_file):
    """The Starlette app alone, without the outer layer, fails closed."""
    import main

    served, _ = main.generate_app({"save_token": TOKEN})
    client = TestClient(served.app, client=(INGRESS, 5000))
    assert client.get("/zones.json").status_code == 403


def test_lifespan_passes_through_the_peer_layer(tmp_zones_file):
    import main

    served, _ = main.generate_app({"trusted_proxies": "10.0.0.5"})
    with TestClient(served, client=(INGRESS, 5000)) as client:
        assert client.get("/zones.json").status_code == 200


# --- Cross-site writes to /save_zones ----------------------------------------


@pytest.fixture
def open_client(tmp_zones_file):
    import main

    main._save_failures.clear()
    app, _ = main.generate_app({"allow_all_ips": True})
    yield TestClient(app)
    main._save_failures.clear()


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

    main._save_failures.clear()
    app, _ = main.generate_app({"save_token": TOKEN})
    client = TestClient(app)
    headers = {"Sec-Fetch-Site": "cross-site", "X-Save-Token": TOKEN}
    for _ in range(main._SAVE_FAILURE_LIMIT + 1):
        assert client.post("/save_zones", json=_valid_payload(), headers=headers).status_code == 403
    # Refusals of browser cross-site requests must not lock the client out.
    r = client.post("/save_zones", json=_valid_payload(), headers={"X-Save-Token": TOKEN})
    assert r.status_code == 200
    main._save_failures.clear()


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


# --- Startup warning ----------------------------------------------------------


def _warnings(caplog):
    return " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


def test_open_access_warning_names_reads_and_trackers(boot, caplog):
    with caplog.at_level(logging.INFO):
        boot({"allow_all_ips": True, "overlay_entities": ["device_tracker.car"]})
    text = _warnings(caplog)
    assert "/save_zones" in text
    assert "/zones.json" in text
    assert "/trackers.json" in text


def test_open_access_warning_omits_trackers_when_none_configured(boot, caplog):
    with caplog.at_level(logging.INFO):
        boot({"allow_all_ips": True})
    text = _warnings(caplog)
    assert "/zones.json" in text
    assert "/trackers.json" not in text


def test_no_open_access_warning_with_token(boot, caplog):
    with caplog.at_level(logging.INFO):
        boot({"allow_all_ips": True, "save_token": TOKEN})
    assert "/zones.json" not in _warnings(caplog)
    assert "require X-Save-Token" in caplog.text


@pytest.mark.parametrize(
    ("options", "level", "expected"),
    [
        ({}, None, None),
        ({"allow_all_ips": True, "save_token": TOKEN}, logging.INFO, "/trackers.json"),
        ({"allow_all_ips": True}, logging.WARNING, "/zones.json is readable"),
        (
            {"allow_all_ips": True, "overlay_entities": ["device_tracker.car"]},
            logging.WARNING,
            "/zones.json and /trackers.json are readable",
        ),
    ],
)
def test_log_open_access(caplog, options, level, expected):
    import main

    with caplog.at_level(logging.INFO):
        main.log_open_access(options)
    if expected is None:
        assert caplog.records == []
    else:
        assert [r.levelno for r in caplog.records] == [level]
        assert expected in caplog.text


def test_peer_layer_records_none_without_a_client():
    import asyncio

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


def test_rotating_forwarded_clients_share_the_proxy_peer_budget(boot):
    options = {"trusted_proxies": PROXY_RANGE, "save_token": TOKEN}
    client = _served_client(boot, options, HOST_IN_PROXY_RANGE)
    codes = [
        client.get(
            "/zones.json",
            headers={"X-Forwarded-For": f"192.0.2.{i}", "X-Save-Token": "wrong"},
        ).status_code
        for i in range(1, 40)
    ]
    assert 429 in codes


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


def test_cross_site_reads_spend_no_failure_budget(boot):
    options = {"save_token": TOKEN, "allow_all_ips": True}
    client = _served_client(boot, options, ("192.168.7.7", 5000))
    for path in ("/zones.json", "/trackers.json"):
        for _ in range(15):
            assert client.get(path, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/zones.json", headers={"X-Save-Token": TOKEN}).status_code == 200
