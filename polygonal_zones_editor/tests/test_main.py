import json
import sys

import pytest
from starlette.testclient import TestClient


@pytest.fixture(autouse=True)
def reset_save_rate_limit():
    """Each test gets a clean rate-limit state. Without this, accumulated
    failures leak between tests and later suites spuriously hit 429."""
    import main
    main._save_failures.clear()
    yield
    main._save_failures.clear()


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
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"type": "FeatureCollection", "features": []}


def test_zones_json_passes_file_bytes_through(allow_all_client, tmp_zones_file):
    # Write a payload with whitespace formatting that JSONResponse would
    # normalise away — the passthrough must preserve the bytes verbatim.
    raw = b'{"type": "FeatureCollection",\n    "features": []\n}'
    tmp_zones_file.write_bytes(raw)
    response = allow_all_client.get("/zones.json")
    assert response.status_code == 200
    assert response.content == raw


def test_zones_json_returns_etag(allow_all_client, tmp_zones_file):
    response = allow_all_client.get("/zones.json")
    assert response.status_code == 200
    etag = response.headers.get("etag")
    assert etag is not None
    # Strong validator: quoted hex sha256.
    assert etag.startswith('"') and etag.endswith('"')
    assert len(etag) == 64 + 2  # sha256 hex + quotes


def test_save_response_includes_new_etag(allow_all_client, tmp_zones_file):
    # Initial GET ETag.
    initial = allow_all_client.get("/zones.json").headers["etag"]
    # POST a new payload.
    r = allow_all_client.post("/save_zones", json=_valid_payload())
    assert r.status_code == 200
    assert "etag" in r.headers
    # The new ETag must be in both the response header and the body, and
    # must differ from the pre-write value.
    assert r.headers["etag"] == r.json()["etag"]
    assert r.headers["etag"] != initial


def test_save_with_matching_if_match_succeeds(allow_all_client, tmp_zones_file):
    etag = allow_all_client.get("/zones.json").headers["etag"]
    r = allow_all_client.post(
        "/save_zones",
        json=_valid_payload(),
        headers={"If-Match": etag},
    )
    assert r.status_code == 200


def test_save_with_stale_if_match_returns_412(allow_all_client, tmp_zones_file):
    # First save advances the ETag.
    initial = allow_all_client.get("/zones.json").headers["etag"]
    allow_all_client.post("/save_zones", json=_valid_payload(name="One"))

    # Now post with the stale ETag — should be refused.
    r = allow_all_client.post(
        "/save_zones",
        json=_valid_payload(name="Two"),
        headers={"If-Match": initial},
    )
    assert r.status_code == 412
    body = r.json()
    assert body["error"] == "precondition failed"
    assert body["current_etag"] != initial
    # The 412 response must surface the current ETag so the client can
    # refetch and recover without a separate GET.
    assert r.headers.get("etag") == body["current_etag"]


def test_save_without_if_match_still_works(allow_all_client, tmp_zones_file):
    # Backwards-compat: clients that don't send If-Match keep working
    # (last-write-wins) so older curl scripts and the integration aren't
    # forced to learn ETags.
    r = allow_all_client.post("/save_zones", json=_valid_payload())
    assert r.status_code == 200


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
    body = response.json()
    assert body["status"] == "ok"
    assert "etag" in body  # new in 0.2.11
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


def test_save_token_required_when_set_and_lan_request(app_factory, tmp_zones_file):
    """When save_token is set, non-ingress requests must present it."""
    app = app_factory({"allow_all_ips": True, "save_token": "s3cret"})
    client = TestClient(app)
    original = tmp_zones_file.read_text()

    # No header.
    r = client.post("/save_zones", json=_valid_payload())
    assert r.status_code == 401
    assert r.json() == {"error": "missing or invalid X-Save-Token"}

    # Wrong token.
    r = client.post("/save_zones", json=_valid_payload(), headers={"X-Save-Token": "wrong"})
    assert r.status_code == 401

    # Correct token.
    r = client.post("/save_zones", json=_valid_payload(), headers={"X-Save-Token": "s3cret"})
    assert r.status_code == 200
    assert tmp_zones_file.read_text() != original


def test_save_token_works_without_allow_all_ips(app_factory, tmp_zones_file):
    """save_token should also unlock LAN access when allow_all_ips is off."""
    app = app_factory({"allow_all_ips": False, "save_token": "s3cret"})
    client = TestClient(app)

    r = client.post("/save_zones", json=_valid_payload(), headers={"X-Save-Token": "s3cret"})
    assert r.status_code == 200

    # Without token, even though allow_all_ips is off, the response is 401
    # (token-required) rather than 403 — server is signalling that auth is
    # available, just not provided.
    r = client.post("/save_zones", json=_valid_payload())
    assert r.status_code == 401


def test_save_token_does_not_affect_zones_json(app_factory, tmp_zones_file):
    """save_token only governs /save_zones; /zones.json still follows the
    coarse IP allowlist + allow_all_ips."""
    app = app_factory({"allow_all_ips": True, "save_token": "s3cret"})
    client = TestClient(app)
    r = client.get("/zones.json")
    assert r.status_code == 200


def test_save_token_ingress_bypass(app_factory, tmp_zones_file):
    """Ingress (172.30.32.2) is always allowed even when a token is set —
    the HA UI Save button must keep working without knowing the token."""
    import main
    from starlette.testclient import TestClient as TC

    app, _ = main.generate_app({"save_token": "s3cret"})
    # Simulate a request from the ingress IP.
    client = TC(app, base_url="http://172.30.32.2")
    # TestClient doesn't expose a way to set request.client.host directly, so
    # we monkeypatch allowed_ip to confirm the bypass works in principle.
    import helpers
    original_allowed_ip = helpers.allowed_ip
    main_allowed_ip = main.allowed_ip
    try:
        # Force allowed_ip to True for this client.
        helpers.allowed_ip = lambda req: True
        main.allowed_ip = lambda req: True
        r = client.post("/save_zones", json=_valid_payload())
        assert r.status_code == 200
    finally:
        helpers.allowed_ip = original_allowed_ip
        main.allowed_ip = main_allowed_ip


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
    assert response.json() == {"zone_colour": "green", "theme": "auto"}


def test_config_json_respects_option(app_factory):
    app = app_factory({"allow_all_ips": True, "zone_colour": "purple"})
    client = TestClient(app)
    response = client.get("/config.json")
    assert response.status_code == 200
    assert response.json() == {"zone_colour": "purple", "theme": "auto"}


def test_config_json_passes_arbitrary_string_safely(app_factory):
    # JSONResponse encodes the value safely — no template injection surface.
    malicious = 'red"; alert(1); //'
    app = app_factory({"allow_all_ips": True, "zone_colour": malicious})
    client = TestClient(app)
    response = client.get("/config.json")
    assert response.status_code == 200
    assert response.json() == {"zone_colour": malicious, "theme": "auto"}


@pytest.mark.parametrize("value", ["auto", "light", "dark"])
def test_config_json_theme_passthrough(app_factory, value):
    app = app_factory({"allow_all_ips": True, "theme": value})
    client = TestClient(app)
    assert client.get("/config.json").json()["theme"] == value


def test_config_json_theme_invalid_falls_back_to_auto(app_factory):
    # HA's schema enforces the enum at the supervisor, but defence in depth:
    # an unexpected value (e.g. someone hand-edited options.json) must not
    # propagate to the frontend as-is.
    app = app_factory({"allow_all_ips": True, "theme": "neon"})
    client = TestClient(app)
    assert client.get("/config.json").json()["theme"] == "auto"


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


def test_zones_json_returns_parseable_geojson_when_populated(allow_all_client, tmp_zones_file):
    """The companion integration's first action on every poll is
    json.loads(body) + a type check. Byte-identity passthrough isn't
    enough — the response must remain parseable GeoJSON with the right
    Content-Type. Previously only byte-identity and empty-file shape
    were tested.
    """
    payload = _valid_payload(name="Home")
    r = allow_all_client.post("/save_zones", json=payload)
    assert r.status_code == 200

    r = allow_all_client.get("/zones.json")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/json"
    data = r.json()
    assert data["type"] == "FeatureCollection"
    assert isinstance(data["features"], list)
    assert data["features"][0]["properties"]["name"] == "Home"


def test_zones_json_returns_503_when_file_unreadable(allow_all_client, tmp_zones_file):
    """Unguarded open() previously produced a 500 with no log line. Now
    returns 503 with a JSON body."""
    tmp_zones_file.unlink()
    response = allow_all_client.get("/zones.json")
    assert response.status_code == 503
    assert response.json() == {"error": "zones file unreadable"}


def test_healthz_returns_503_when_zones_missing(restricted_client, tmp_zones_file):
    """Healthcheck now reflects zones-file readability so the Docker
    HEALTHCHECK surfaces a broken state instead of staying green while
    /zones.json 500s."""
    tmp_zones_file.unlink()
    r = restricted_client.get("/healthz")
    assert r.status_code == 503
    assert r.text == "zones file unreadable"


def test_save_zones_rate_limits_after_10_failures(app_factory, tmp_zones_file):
    """When save_token is set, repeated unauthorised attempts eventually
    return 429 instead of 401 — defends against LAN token-brute-force."""
    app = app_factory({"save_token": "sekrit"})
    client = TestClient(app)

    for attempt in range(10):
        r = client.post("/save_zones", json=_valid_payload())
        assert r.status_code == 401, f"attempt {attempt} unexpectedly {r.status_code}"

    r = client.post("/save_zones", json=_valid_payload())
    assert r.status_code == 429
    assert r.json() == {"error": "too many failed attempts"}


def test_save_zones_rate_limit_lets_correct_token_through_on_first_try(app_factory, tmp_zones_file):
    """Rate limit doesn't count successful saves; a valid token before the
    budget is exhausted always succeeds."""
    app = app_factory({"save_token": "sekrit"})
    client = TestClient(app)

    # Nine failures — within the window of 10.
    for _ in range(9):
        client.post("/save_zones", json=_valid_payload())
    # Tenth request with the correct token.
    r = client.post(
        "/save_zones",
        json=_valid_payload(),
        headers={"X-Save-Token": "sekrit"},
    )
    assert r.status_code == 200


def test_zones_json_requires_ingress_when_save_token_set_and_allow_all_ips_off(
    app_factory, tmp_zones_file,
):
    """Authz matrix gap: save_token governs /save_zones but must not
    accidentally open /zones.json. With allow_all_ips: false and a token
    set, non-ingress GETs should still be rejected."""
    app = app_factory({"allow_all_ips": False, "save_token": "sekrit"})
    client = TestClient(app)
    r = client.get("/zones.json")
    assert r.status_code == 403


def test_parse_trusted_proxies_handles_empty_and_list(app_factory):
    import main
    assert main._parse_trusted_proxies({}) == []
    assert main._parse_trusted_proxies({"trusted_proxies": ""}) == []
    assert main._parse_trusted_proxies({"trusted_proxies": "10.0.0.5"}) == ["10.0.0.5"]
    assert main._parse_trusted_proxies(
        {"trusted_proxies": " 192.168.1.1 ,10.0.0.5 "}
    ) == ["192.168.1.1", "10.0.0.5"]


@pytest.mark.parametrize("dangerous", [
    "*",
    "0.0.0.0",
    "0.0.0.0/0",
    "::",
    "::/0",
    "172.30.32.2",
])
def test_parse_trusted_proxies_drops_dangerous_values(app_factory, caplog, dangerous):
    """Wildcards and the ingress IP must never be handed to uvicorn's
    forwarded_allow_ips — doing so lets any on-path attacker forge
    X-Forwarded-For: 172.30.32.2 and bypass the ingress-IP check on
    /save_zones. The parser drops them and logs an error."""
    import logging
    import main

    with caplog.at_level(logging.ERROR):
        result = main._parse_trusted_proxies({"trusted_proxies": dangerous})

    assert result == []
    assert any(dangerous in rec.message for rec in caplog.records)


def test_parse_trusted_proxies_drops_dangerous_and_keeps_safe(app_factory, caplog):
    """Mixed input: dangerous entries are dropped, safe ones preserved."""
    import logging
    import main

    with caplog.at_level(logging.ERROR):
        result = main._parse_trusted_proxies(
            {"trusted_proxies": "172.30.32.2, 10.0.0.5, *, 192.168.1.1"}
        )

    assert result == ["10.0.0.5", "192.168.1.1"]
    messages = " ".join(rec.message for rec in caplog.records)
    assert "172.30.32.2" in messages
    assert "*" in messages
