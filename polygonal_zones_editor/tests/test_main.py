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


def test_index_substitutes_zone_colour(allow_all_client):
    response = allow_all_client.get("/")
    assert response.status_code == 200
    # ZONE_COLOUR default is "green" when the option is absent.
    assert '"green"' in response.text
    assert "{{ ZONE_COLOUR }}" not in response.text


def test_index_respects_zone_colour_option(app_factory):
    app = app_factory({"allow_all_ips": True, "zone_colour": "purple"})
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert '"purple"' in response.text


def test_index_escapes_malicious_zone_colour(app_factory):
    malicious = 'red"; alert(1); //'
    app = app_factory({"allow_all_ips": True, "zone_colour": malicious})
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    # Quote must be escaped so the injected payload stays inside the JS string literal.
    assert '"red\\"; alert(1); //"' in response.text or '"red\\u0022; alert(1); //"' in response.text


def test_index_blocks_unauthorized_client(restricted_client):
    response = restricted_client.get("/")
    assert response.status_code == 403
    assert response.text == "not allowed"


def test_healthz_returns_ok_without_authz(restricted_client):
    response = restricted_client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"


def test_referrer_policy_header_is_applied(allow_all_client):
    response = allow_all_client.get("/zones.json")
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
