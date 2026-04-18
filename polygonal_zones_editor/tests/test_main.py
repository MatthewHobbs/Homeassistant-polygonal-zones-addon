import json
import sys

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def app_factory(tmp_zones_file, monkeypatch):
    # Keep sys.argv clean so allow_all_ips doesn't flip via CLI flags.
    monkeypatch.setattr(sys, "argv", ["main.py"])

    import main

    def _build(options=None):
        app, _ = main.generate_app(options or {})
        return app

    return _build


@pytest.fixture
def allow_all_client(app_factory):
    app = app_factory({"allow_all_ips": True})
    return TestClient(app)


@pytest.fixture
def restricted_client(app_factory):
    app = app_factory({})
    return TestClient(app)


def _valid_payload(name="Home"):
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": name},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
                },
            }
        ],
    }


def test_zones_json_returns_empty_collection(allow_all_client, tmp_zones_file):
    response = allow_all_client.get("/zones.json")
    assert response.status_code == 200
    assert response.json() == {"type": "FeatureCollection", "features": []}


def test_zones_json_sets_cache_headers(allow_all_client):
    response = allow_all_client.get("/zones.json")
    assert response.headers["cache-control"] == "no-cache, no-store, must-revalidate"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["expires"] == "0"


def test_zones_json_no_wildcard_cors(allow_all_client):
    response = allow_all_client.get("/zones.json")
    assert response.headers.get("access-control-allow-origin") != "*"


def test_zones_json_blocks_unauthorized_client(restricted_client):
    response = restricted_client.get("/zones.json")
    assert response.status_code == 403
    assert response.text == "not allowed"


def test_save_zones_persists_valid_geojson(allow_all_client, tmp_zones_file):
    payload = _valid_payload()
    response = allow_all_client.post("/save_zones", json=payload)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert json.loads(tmp_zones_file.read_text()) == payload


def test_save_zones_rejects_non_geojson(allow_all_client, tmp_zones_file):
    original = tmp_zones_file.read_text()
    response = allow_all_client.post("/save_zones", json={"evil": True})
    assert response.status_code == 422
    assert tmp_zones_file.read_text() == original


@pytest.mark.parametrize("payload", [[], "string", 42,
                                      {"type": "FeatureCollection", "features": "oops"},
                                      {"type": "FeatureCollection",
                                       "features": [{"type": "Feature",
                                                     "geometry": {"type": "Point",
                                                                  "coordinates": [0, 0]}}]}])
def test_save_zones_rejects_malformed_shapes(allow_all_client, tmp_zones_file, payload):
    original = tmp_zones_file.read_text()
    response = allow_all_client.post("/save_zones", json=payload)
    assert response.status_code == 422
    assert tmp_zones_file.read_text() == original


def test_save_zones_rejects_non_string_name(allow_all_client, tmp_zones_file):
    payload = _valid_payload()
    payload["features"][0]["properties"]["name"] = {"x": 1}
    original = tmp_zones_file.read_text()
    response = allow_all_client.post("/save_zones", json=payload)
    assert response.status_code == 422
    assert tmp_zones_file.read_text() == original


def test_save_zones_rejects_invalid_json(allow_all_client, tmp_zones_file):
    original = tmp_zones_file.read_text()
    response = allow_all_client.post(
        "/save_zones", content=b"not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    assert tmp_zones_file.read_text() == original


def test_save_zones_rejects_oversize_payload(allow_all_client, tmp_zones_file):
    original = tmp_zones_file.read_text()
    huge = "x" * (512 * 1024 + 1)
    response = allow_all_client.post(
        "/save_zones", content=huge.encode(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert tmp_zones_file.read_text() == original


def test_save_zones_returns_500_on_write_failure(allow_all_client, tmp_zones_file, monkeypatch):
    import main

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(main, "atomic_write_json", boom)
    response = allow_all_client.post("/save_zones", json=_valid_payload())
    assert response.status_code == 500


def test_save_zones_blocks_unauthorized_client(restricted_client, tmp_zones_file):
    original = tmp_zones_file.read_text()
    response = restricted_client.post("/save_zones", json={"evil": True})

    assert response.status_code == 403
    assert response.text == "not allowed"
    assert tmp_zones_file.read_text() == original


def test_index_serves_static_html(allow_all_client):
    response = allow_all_client.get("/")
    assert response.status_code == 200
    # index.html is now served by StaticFiles (html=True) — no template
    # rendering, no per-request file open.
    assert "<title>Polygonal zones: Edit zones</title>" in response.text
    # Placeholder must be gone now that ZONE_COLOUR comes from /config.json.
    assert "{{ ZONE_COLOUR }}" not in response.text
    assert "ZONE_COLOUR" not in response.text


def test_config_json_default(allow_all_client):
    response = allow_all_client.get("/config.json")
    assert response.status_code == 200
    assert response.json() == {"zone_colour": "green"}


def test_config_json_respects_option(app_factory):
    app = app_factory({"allow_all_ips": True, "zone_colour": "purple"})
    client = TestClient(app)
    response = client.get("/config.json")
    assert response.status_code == 200
    assert response.json() == {"zone_colour": "purple"}


def test_config_json_passes_arbitrary_string_safely(app_factory):
    # JSONResponse encodes the value safely — no template injection surface.
    malicious = 'red"; alert(1); //'
    app = app_factory({"allow_all_ips": True, "zone_colour": malicious})
    client = TestClient(app)
    response = client.get("/config.json")
    assert response.status_code == 200
    assert response.json() == {"zone_colour": malicious}


def test_index_blocks_unauthorized_client(restricted_client):
    response = restricted_client.get("/")
    assert response.status_code == 403
    assert response.text == "not allowed"


def test_config_json_blocks_unauthorized_client(restricted_client):
    response = restricted_client.get("/config.json")
    assert response.status_code == 403


def test_healthz_returns_ok_without_authz(restricted_client):
    response = restricted_client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"


def test_referrer_policy_header_is_applied(allow_all_client):
    response = allow_all_client.get("/zones.json")
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_static_file_served_to_authorized_client(allow_all_client):
    response = allow_all_client.get("/js/map.js")
    assert response.status_code == 200
    assert "generate_map" in response.text


def test_static_file_blocked_for_unauthorized_client(restricted_client):
    response = restricted_client.get("/js/map.js")
    assert response.status_code == 403


def test_static_traversal_attempt_rejected(allow_all_client):
    # StaticFiles normalises and rejects paths that escape the root directory.
    response = allow_all_client.get("/..%2F..%2Fapp%2Fconst.py")
    assert response.status_code in (400, 404)


def test_parse_trusted_proxies_handles_empty_and_list(app_factory):
    import main
    assert main._parse_trusted_proxies({}) == []
    assert main._parse_trusted_proxies({"trusted_proxies": ""}) == []
    assert main._parse_trusted_proxies({"trusted_proxies": "172.30.32.2"}) == ["172.30.32.2"]
    assert main._parse_trusted_proxies(
        {"trusted_proxies": " 172.30.32.2 ,10.0.0.5 "}
    ) == ["172.30.32.2", "10.0.0.5"]
