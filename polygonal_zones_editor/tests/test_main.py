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


def test_zones_json_returns_empty_collection(allow_all_client, tmp_zones_file):
    response = allow_all_client.get("/zones.json")
    assert response.status_code == 200
    assert response.json() == {"type": "FeatureCollection", "features": []}


def test_zones_json_sets_cache_and_cors_headers(allow_all_client):
    response = allow_all_client.get("/zones.json")
    assert response.headers["cache-control"] == "no-cache, no-store, must-revalidate"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["expires"] == "0"
    assert response.headers["access-control-allow-origin"] == "*"


def test_save_zones_persists_posted_body(allow_all_client, tmp_zones_file):
    payload = {"type": "FeatureCollection", "features": [{"id": 1}]}
    response = allow_all_client.post("/save_zones", json=payload)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert json.loads(tmp_zones_file.read_text()) == payload


def test_save_zones_blocks_unauthorized_client(restricted_client, tmp_zones_file):
    original = tmp_zones_file.read_text()
    response = restricted_client.post("/save_zones", json={"evil": True})

    assert response.status_code == 403
    assert response.text == "not allowed"
    assert tmp_zones_file.read_text() == original


def test_index_substitutes_zone_colour(allow_all_client):
    response = allow_all_client.get("/", params={"colour_check": 1})
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


def test_index_blocks_unauthorized_client(restricted_client):
    response = restricted_client.get("/")
    assert response.status_code == 403
    assert response.text == "not allowed"


def test_referrer_policy_header_is_applied(allow_all_client):
    response = allow_all_client.get("/zones.json")
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
