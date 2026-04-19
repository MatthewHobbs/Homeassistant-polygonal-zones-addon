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


def test_zones_json_returns_last_modified(allow_all_client, tmp_zones_file):
    response = allow_all_client.get("/zones.json")
    assert response.status_code == 200
    lm = response.headers.get("last-modified")
    assert lm is not None
    # RFC 7231 date: "Sun, 06 Nov 1994 08:49:37 GMT" — just sanity-check
    # the shape, exact value depends on the tmp_zones_file fixture mtime.
    assert lm.endswith("GMT")
    assert "," in lm


def test_zones_json_returns_304_on_matching_if_none_match(allow_all_client, tmp_zones_file):
    """Integration polling idiom (#119): pass the previous ETag via
    If-None-Match; the addon returns 304 with no body so the poll
    doesn't re-parse a FeatureCollection that hasn't changed."""
    etag = allow_all_client.get("/zones.json").headers["etag"]
    r = allow_all_client.get("/zones.json", headers={"If-None-Match": etag})
    assert r.status_code == 304
    # 304 must not carry a body per RFC 7232.
    assert r.content == b""
    # The validator headers MUST be repeated on 304 so clients can refresh
    # their cache state.
    assert r.headers["etag"] == etag
    assert r.headers.get("last-modified")
    assert r.headers["cache-control"] == "no-cache, no-store, must-revalidate"


def test_zones_json_returns_304_on_wildcard_if_none_match(allow_all_client, tmp_zones_file):
    """If-None-Match: * matches any existing resource per RFC 7232."""
    r = allow_all_client.get("/zones.json", headers={"If-None-Match": "*"})
    assert r.status_code == 304


def test_zones_json_returns_200_on_stale_if_none_match(allow_all_client, tmp_zones_file):
    """If the client's cached ETag doesn't match, serve the full body."""
    r = allow_all_client.get(
        "/zones.json", headers={"If-None-Match": '"notarealtag"'}
    )
    assert r.status_code == 200
    assert r.headers.get("etag") != '"notarealtag"'
    assert b"FeatureCollection" in r.content


def test_zones_json_if_none_match_handles_comma_separated_list(allow_all_client, tmp_zones_file):
    """RFC 7232 permits a comma-separated list of candidate validators.
    Match on any one returns 304."""
    etag = allow_all_client.get("/zones.json").headers["etag"]
    r = allow_all_client.get(
        "/zones.json",
        headers={"If-None-Match": f'"stale", {etag}, "alsostale"'},
    )
    assert r.status_code == 304


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
    # As of 0.2.33 the server stamps schema_version and backfills
    # properties.id on write, so the persisted file is a normalised
    # superset of the incoming payload rather than byte-identical.
    stored = json.loads(tmp_zones_file.read_text())
    assert stored["type"] == payload["type"]
    assert len(stored["features"]) == len(payload["features"])
    for sent, got in zip(payload["features"], stored["features"]):
        assert sent["type"] == got["type"]
        assert sent["geometry"] == got["geometry"]
        assert got["properties"]["name"] == sent["properties"]["name"]
        # id was backfilled (uuid4.hex = 32 chars of hex)
        assert isinstance(got["properties"]["id"], str)
        assert len(got["properties"]["id"]) == 32
    # schema_version stamped.
    assert stored["schema_version"] == 1


def test_save_zones_preserves_client_supplied_id(allow_all_client, tmp_zones_file):
    """When the client supplies a stable properties.id (Leaflet-draw path
    where the editor generates a client-side UUID), the server must
    round-trip it verbatim rather than overwriting with a fresh one —
    otherwise automations bound to the id would break on every save."""
    stable_id = "aaaa1111bbbb2222cccc3333dddd4444"
    payload = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"name": "Home", "id": stable_id},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
            },
        }],
    }
    r = allow_all_client.post("/save_zones", json=payload)
    assert r.status_code == 200
    stored = json.loads(tmp_zones_file.read_text())
    assert stored["features"][0]["properties"]["id"] == stable_id


def test_save_zones_backfills_missing_id_and_non_dict_properties(
    allow_all_client, tmp_zones_file,
):
    """Feature with properties=null (allowed on input) gets a fresh
    properties dict carrying a backfilled id. Feature with an empty id
    (treated as falsy) gets re-filled. Both coexist in one payload."""
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": None,
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
                },
            },
            {
                "type": "Feature",
                "properties": {"name": "Second"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[2.0, 2.0], [3.0, 2.0], [3.0, 3.0], [2.0, 2.0]]],
                },
            },
        ],
    }
    r = allow_all_client.post("/save_zones", json=payload)
    assert r.status_code == 200
    stored = json.loads(tmp_zones_file.read_text())
    for feat in stored["features"]:
        assert isinstance(feat["properties"], dict)
        assert len(feat["properties"]["id"]) == 32


def test_save_zones_rejects_non_int_schema_version(allow_all_client, tmp_zones_file):
    payload = {**_valid_payload(), "schema_version": "oops"}
    r = allow_all_client.post("/save_zones", json=payload)
    assert r.status_code == 422
    assert "schema_version" in r.json()["detail"]


def test_save_zones_rejects_bool_schema_version(allow_all_client, tmp_zones_file):
    """bool subclasses int in Python; must be explicitly rejected so a
    consumer doing `schema_version >= 1` doesn't silently pass on True."""
    payload = {**_valid_payload(), "schema_version": True}
    r = allow_all_client.post("/save_zones", json=payload)
    assert r.status_code == 422
    assert "schema_version" in r.json()["detail"]


def test_save_zones_accepts_valid_schema_version(allow_all_client, tmp_zones_file):
    payload = {**_valid_payload(), "schema_version": 1}
    r = allow_all_client.post("/save_zones", json=payload)
    assert r.status_code == 200


def test_save_zones_rejects_non_string_id(allow_all_client, tmp_zones_file):
    payload = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"name": "x", "id": 42},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
            },
        }],
    }
    r = allow_all_client.post("/save_zones", json=payload)
    assert r.status_code == 422
    assert "properties.id" in r.json()["detail"]


def test_save_zones_rejects_empty_id(allow_all_client, tmp_zones_file):
    payload = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"name": "x", "id": ""},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
            },
        }],
    }
    r = allow_all_client.post("/save_zones", json=payload)
    assert r.status_code == 422
    assert "properties.id" in r.json()["detail"]


def test_save_zones_rejects_duplicate_ids(allow_all_client, tmp_zones_file):
    """id is the binding handle — duplicates would defeat the 'stable
    reference' contract since an automation couldn't route an id match
    to a specific zone unambiguously."""
    geom = {"type": "Polygon", "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]]}
    payload = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {"name": "a", "id": "dup"}, "geometry": geom},
            {"type": "Feature", "properties": {"name": "b", "id": "dup"}, "geometry": geom},
        ],
    }
    r = allow_all_client.post("/save_zones", json=payload)
    assert r.status_code == 422
    assert "duplicate" in r.json()["detail"]
    assert "id" in r.json()["detail"]


def test_save_zones_rejects_non_geojson(allow_all_client, tmp_zones_file):
    original = tmp_zones_file.read_text()
    response = allow_all_client.post("/save_zones", json={"evil": True})
    assert response.status_code == 422
    assert tmp_zones_file.read_text() == original


@pytest.mark.parametrize("payload", [
    # Top-level not a FeatureCollection.
    [],
    "string",
    42,
    # Features not a list.
    {"type": "FeatureCollection", "features": "oops"},
    # Feature is not a dict.
    {"type": "FeatureCollection", "features": ["not a dict"]},
    # Feature missing 'type' key (treated as wrong type).
    {"type": "FeatureCollection", "features": [{"geometry": {"type": "Polygon", "coordinates": []}}]},
    # Geometry not a dict.
    {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": "oops"}]},
    # Geometry type not in Polygon/MultiPolygon.
    {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [0, 0]}}]},
    # Coordinates not a list.
    {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "Polygon", "coordinates": "oops"}}]},
    # Properties is a non-None non-dict value.
    {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": "oops", "geometry": {"type": "Polygon", "coordinates": []}}]},
])
def test_save_zones_rejects_malformed_shapes(allow_all_client, tmp_zones_file, payload):
    original = tmp_zones_file.read_text()
    response = allow_all_client.post("/save_zones", json=payload)
    assert response.status_code == 422
    assert tmp_zones_file.read_text() == original


def _feature(geom):
    """Wrap a geometry in a valid Feature shell for parametrized reject tests."""
    return {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {}, "geometry": geom}],
    }


def _polygon(coords):
    return {"type": "Polygon", "coordinates": coords}


@pytest.mark.parametrize("payload,expect_detail_contains", [
    # Ring integrity
    (_feature(_polygon([[[0, 0], [1, 0], [1, 1]]])),
        "at least 4 positions"),
    (_feature(_polygon([[[0, 0], [1, 0], [1, 1], [2, 2]]])),
        "not closed"),
    (_feature(_polygon("oops")),
        "Polygon coordinates must be a list"),
    (_feature(_polygon([])),
        "Polygon must have at least one ring"),
    (_feature(_polygon([[[0, 0], [1, 0], [1, 1], "oops"]])),
        "position must be a list"),
    # Ring itself isn't a list (Polygon.coordinates contains a non-list).
    (_feature(_polygon(["oops"])),
        "ring must be a list"),
    # Coordinate ranges
    (_feature(_polygon([[[181, 0], [1, 0], [1, 1], [181, 0]]])),
        "longitude out of range"),
    (_feature(_polygon([[[0, 91], [1, 0], [1, 1], [0, 91]]])),
        "latitude out of range"),
    # Non-numeric coordinates — bool is explicitly rejected even though it
    # subclasses int in Python (True == 1, False == 0 would otherwise pass).
    (_feature(_polygon([[[True, 0], [1, 0], [1, 1], [True, 0]]])),
        "longitude must be a number"),
    (_feature(_polygon([[["oops", 0], [1, 0], [1, 1], ["oops", 0]]])),
        "longitude must be a number"),
    # MultiPolygon shape
    ({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": {"type": "MultiPolygon", "coordinates": "oops"}}]},
        "MultiPolygon coordinates must be a list"),
    ({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": {"type": "MultiPolygon", "coordinates": []}}]},
        "MultiPolygon must have at least one polygon"),
    # Geometry type
    (_feature({"type": "LineString", "coordinates": [[0, 0], [1, 1]]}),
        "Polygon or MultiPolygon"),
    # Properties shape
    ({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": "oops", "geometry": _polygon([[[0, 0], [1, 0], [1, 1], [0, 0]]])}]},
        "properties: must be a dict"),
])
def test_save_zones_rejects_invalid_geometry_with_detail(
    allow_all_client, tmp_zones_file, payload, expect_detail_contains,
):
    original = tmp_zones_file.read_text()
    r = allow_all_client.post("/save_zones", json=payload)
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "invalid GeoJSON"
    # Detail is index-bearing so a client can point at the offending feature
    # / ring / position without the server echoing any coordinate values.
    assert expect_detail_contains in body["detail"]
    # File must not have been written.
    assert tmp_zones_file.read_text() == original


def test_save_zones_rejects_non_finite_coordinate(allow_all_client, tmp_zones_file):
    """Infinity / NaN are semi-legal in Python's json (allow_nan=True) but
    meaningless for geodetic coordinates. Send as raw bytes — httpx on
    Python 3.14 refuses to serialize float('inf') via json=payload."""
    raw = (
        b'{"type":"FeatureCollection","features":[{"type":"Feature",'
        b'"properties":{},"geometry":{"type":"Polygon",'
        b'"coordinates":[[[Infinity,0],[1,0],[1,1],[Infinity,0]]]}}]}'
    )
    r = allow_all_client.post(
        "/save_zones", content=raw, headers={"content-type": "application/json"}
    )
    assert r.status_code == 422
    assert "must be finite" in r.json()["detail"]


def test_save_zones_rejects_vertex_cap_breach(allow_all_client, tmp_zones_file):
    """Per-feature vertex cap. Build a single Polygon with 1001 vertices —
    the first-last-equal close duplicates one position, so generate 1000
    distinct points and close with the first. Cap is 1000 so this should
    trip at the 1001st total."""
    ring = [[i / 1000.0, 0.0] for i in range(1001)] + [[0.0, 0.0]]
    payload = _feature(_polygon([ring]))
    r = allow_all_client.post("/save_zones", json=payload)
    assert r.status_code == 422
    assert "vertex count" in r.json()["detail"]


def test_save_zones_rejects_duplicate_zone_names(allow_all_client, tmp_zones_file):
    """Two features with the same name would make HA automations ambiguous
    (`state_attr('zone.home', ...)` — which 'home'?). Reject at save time."""
    geom = _polygon([[[0, 0], [1, 0], [1, 1], [0, 0]]])
    payload = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {"name": "Home"}, "geometry": geom},
            {"type": "Feature", "properties": {"name": "Home"}, "geometry": geom},
        ],
    }
    r = allow_all_client.post("/save_zones", json=payload)
    assert r.status_code == 422
    assert "duplicate" in r.json()["detail"]


def test_save_zones_accepts_unnamed_duplicates(allow_all_client, tmp_zones_file):
    """Uniqueness only applies to features that carry a string name.
    Two unnamed features (or features with name=None) are fine — HA won't
    surface them as zone.* entities anyway until a name is set."""
    geom = _polygon([[[0, 0], [1, 0], [1, 1], [0, 0]]])
    payload = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {}, "geometry": geom},
            {"type": "Feature", "properties": {"name": None}, "geometry": geom},
        ],
    }
    r = allow_all_client.post("/save_zones", json=payload)
    assert r.status_code == 200


def test_save_zones_accepts_multipolygon(allow_all_client, tmp_zones_file):
    """MultiPolygon is an accepted geometry type alongside Polygon. Exercises
    the branch in _is_valid_feature_collection that allows MultiPolygon."""
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "Two shapes"},
                "geometry": {
                    "type": "MultiPolygon",
                    "coordinates": [
                        [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
                        [[[2.0, 2.0], [3.0, 2.0], [3.0, 3.0], [2.0, 2.0]]],
                    ],
                },
            }
        ],
    }
    r = allow_all_client.post("/save_zones", json=payload)
    assert r.status_code == 200


def test_save_zones_accepts_null_properties(allow_all_client, tmp_zones_file):
    """`properties` may be absent or null per GeoJSON — the validator only
    rejects non-None non-dict values."""
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": None,
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
                },
            }
        ],
    }
    r = allow_all_client.post("/save_zones", json=payload)
    assert r.status_code == 200


def test_save_zones_rejects_body_over_cap_with_small_content_length(
    allow_all_client, tmp_zones_file, monkeypatch,
):
    """Client lies about Content-Length — header says small, body is large.
    The pre-read gate (content-length check) passes, but the post-read
    length check catches the oversize body and returns 413."""
    import main

    # Force the pre-read gate to pass by temporarily dropping MAX_SAVE_BYTES
    # cap. Then restore it before the post-read check runs.
    # Simpler approach: monkeypatch request.body() to return oversize bytes.
    original_payload = b'{"type": "FeatureCollection", "features": []}'
    huge_body = b"x" * (main.MAX_SAVE_BYTES + 10)

    # Patch request body reading to return huge bytes after header check.
    from starlette.requests import Request
    original_body = Request.body

    async def lying_body(self):
        return huge_body

    monkeypatch.setattr(Request, "body", lying_body)

    # content-length header small, triggering the post-read len(body) > cap.
    r = allow_all_client.post(
        "/save_zones",
        content=original_payload,
        headers={"content-type": "application/json", "content-length": str(len(original_payload))},
    )
    assert r.status_code == 413
    assert r.json() == {"error": "payload too large"}


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


def test_save_token_strips_whitespace_symmetrically(app_factory, tmp_zones_file):
    """Both stored and provided tokens are stripped before compare.
    Previously only the stored token was stripped, so a trailing space
    in the X-Save-Token header would fail an otherwise-correct token."""
    app = app_factory({"save_token": "sekrit"})
    client = TestClient(app)
    # Trailing whitespace on the provided token should be tolerated.
    r = client.post(
        "/save_zones",
        json=_valid_payload(),
        headers={"X-Save-Token": "sekrit "},
    )
    assert r.status_code == 200


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


def test_zones_json_requires_token_when_set_and_lan_request(app_factory, tmp_zones_file):
    """When save_token is set, non-ingress GET /zones.json requests must
    present the same X-Save-Token as /save_zones. Previously (before
    #113) /zones.json was reachable on LAN without a token once
    allow_all_ips was on — zone geometry was less protected than the
    less-sensitive write action."""
    app = app_factory({"allow_all_ips": True, "save_token": "s3cret"})
    client = TestClient(app)

    # No header.
    r = client.get("/zones.json")
    assert r.status_code == 401
    assert r.json() == {"error": "missing or invalid X-Save-Token"}

    # Wrong token.
    r = client.get("/zones.json", headers={"X-Save-Token": "wrong"})
    assert r.status_code == 401

    # Correct token.
    r = client.get("/zones.json", headers={"X-Save-Token": "s3cret"})
    assert r.status_code == 200
    assert r.headers.get("etag")


def test_zones_json_unchanged_when_no_save_token(app_factory, tmp_zones_file):
    """Without save_token, reads remain gated only by the IP allowlist /
    allow_all_ips. No regression for the common LAN-backup workflow."""
    app = app_factory({"allow_all_ips": True})
    client = TestClient(app)
    r = client.get("/zones.json")
    assert r.status_code == 200


def test_zones_json_rate_limit_shared_with_save_failures(app_factory, tmp_zones_file):
    """Failed /zones.json reads and failed /save_zones writes share one
    rate-limit bucket per IP. Without this, an attacker could double
    their per-window guess budget by rotating between GET and POST.

    Uses allow_all_ips: true so both paths reach their token check
    (otherwise IPAllowMiddleware blocks GETs at 403 before the handler
    runs, and failures recorded only from /save_zones wouldn't prove
    the shared-bucket property)."""
    app = app_factory({"allow_all_ips": True, "save_token": "sekrit"})
    client = TestClient(app)

    # Five bad POSTs.
    for _ in range(5):
        r = client.post("/save_zones", json=_valid_payload())
        assert r.status_code == 401
    # Five bad GETs — total failures now at the limit.
    for _ in range(5):
        r = client.get("/zones.json")
        assert r.status_code == 401

    # Next GET is rate-limited — and so is the next POST, because the
    # counter is shared.
    r = client.get("/zones.json")
    assert r.status_code == 429
    r = client.post("/save_zones", json=_valid_payload())
    assert r.status_code == 429


def test_zones_json_ingress_bypasses_token(app_factory, tmp_zones_file):
    """Ingress (172.30.32.2) reads remain unauthenticated even when a
    token is set — the HA UI's zones.json fetch must keep working."""
    import main

    app, _ = main.generate_app({"save_token": "sekrit"})
    client = TestClient(app)
    import helpers
    original_allowed_ip_helpers = helpers.allowed_ip
    original_allowed_ip_main = main.allowed_ip
    try:
        helpers.allowed_ip = lambda req: True
        main.allowed_ip = lambda req: True
        r = client.get("/zones.json")
        assert r.status_code == 200
    finally:
        helpers.allowed_ip = original_allowed_ip_helpers
        main.allowed_ip = original_allowed_ip_main


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


def test_security_headers_applied(allow_all_client):
    """SecurityHeadersMiddleware sets Referrer-Policy, nosniff,
    X-Frame-Options, and a CSP on every response."""
    response = allow_all_client.get("/zones.json")
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    csp = response.headers["content-security-policy"]
    # Key invariants of the CSP. Full string is intentionally not pinned
    # so we don't have to update every test when a directive is tuned.
    assert "default-src 'self'" in csp
    # frame-ancestors: only 'self' (for HA ingress same-origin) plus Nabu
    # Casa remote access. *.home-assistant.io was removed in 0.2.32 (#128)
    # — it was HA's marketing domain, not an iframe-embedding origin.
    assert "frame-ancestors 'self' https://*.ui.nabu.casa" in csp
    assert "https://*.home-assistant.io" not in csp
    # 'unsafe-inline' removed from script-src in 0.2.32 (#128) after the
    # Save button's inline onclick moved to addEventListener. style-src
    # still needs it for Leaflet's injected tile transforms.
    assert "script-src 'self';" in csp
    assert "'unsafe-inline'" not in csp.split("style-src")[0]
    # unpkg.com dropped once Leaflet + Leaflet-Draw went self-hosted in
    # 0.2.32 (#122). Guard against anyone re-adding a CDN dep silently.
    assert "unpkg.com" not in csp
    assert "object-src 'none'" in csp
    # OSM, CARTO, and Esri World Imagery tile hosts must remain in img-src
    # or the corresponding basemap options (#31) break. The Esri origin
    # assertion uses `any(t == url ...)` on tokens rather than a plain
    # `url in str` / `url in list` check: CodeQL's
    # py/incomplete-url-substring-sanitization rule fires on any URL-literal
    # used in a `in` / containment check, regardless of what's on the right
    # side (string, list, set). `==` on split tokens is exact-match and
    # clears the rule without weakening the assertion.
    assert "https://*.tile.openstreetmap.org" in csp
    assert "https://*.basemaps.cartocdn.com" in csp
    img_src_sources = next(
        d.strip().split()
        for d in csp.split(";")
        if d.strip().startswith("img-src")
    )
    assert any(t == "https://server.arcgisonline.com" for t in img_src_sources)


def test_security_headers_applied_to_static_files(allow_all_client):
    """Middleware runs on static assets too — clickjacking / CSP apply
    to index.html and the JS bundles."""
    response = allow_all_client.get("/")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert "frame-ancestors" in response.headers["content-security-policy"]


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


def test_save_zones_rate_limit_window_expires(app_factory, tmp_zones_file, monkeypatch):
    """Failures older than the window are evicted on the next check. Without
    this, a client hitting the limit would be locked out forever. Exercises
    the `failures.popleft()` branch in `_rate_limit_exceeded`.
    """
    import main

    # Freeze time at t0, record 10 failures, advance past the window, then
    # a fresh attempt should succeed (old failures get popped).
    t = [1000.0]
    monkeypatch.setattr(main.time, "time", lambda: t[0])

    app = app_factory({"save_token": "sekrit"})
    client = TestClient(app)

    for _ in range(10):
        client.post("/save_zones", json=_valid_payload())

    # 11th attempt is rate-limited.
    r = client.post("/save_zones", json=_valid_payload())
    assert r.status_code == 429

    # Advance time beyond the window.
    t[0] += main._SAVE_FAILURE_WINDOW_SECONDS + 1

    # Now a request with the correct token should succeed, and the old
    # failures should have been popped during the check.
    r = client.post(
        "/save_zones",
        json=_valid_payload(),
        headers={"X-Save-Token": "sekrit"},
    )
    assert r.status_code == 200


def test_current_zones_etag_returns_none_when_file_unreadable(tmp_zones_file):
    """_current_zones_etag returns None when the file can't be opened —
    used by save_zones during If-Match comparison. Covers the OSError
    branch that tests hitting /save_zones with If-Match on a missing
    file would otherwise not reach directly."""
    import main

    tmp_zones_file.unlink()
    assert main._current_zones_etag() is None


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


def test_zones_json_returns_401_when_save_token_set_and_no_header(
    app_factory, tmp_zones_file,
):
    """With save_token set and allow_all_ips off, a non-ingress GET with
    no X-Save-Token header returns 401 (token required) rather than 403
    (coarse block). This mirrors the /save_zones behaviour so a user
    who sets save_token without also flipping allow_all_ips gets a
    useful "auth is available, provide it" signal rather than a
    flat forbidden."""
    app = app_factory({"allow_all_ips": False, "save_token": "sekrit"})
    client = TestClient(app)
    r = client.get("/zones.json")
    assert r.status_code == 401
    assert r.json() == {"error": "missing or invalid X-Save-Token"}


def test_zones_json_token_unlocks_lan_without_allow_all_ips(
    app_factory, tmp_zones_file,
):
    """Mirror of test_save_token_works_without_allow_all_ips for reads.
    save_token is the stronger signal; setting it should unlock LAN
    access without requiring allow_all_ips to also be flipped."""
    app = app_factory({"allow_all_ips": False, "save_token": "sekrit"})
    client = TestClient(app)
    r = client.get("/zones.json", headers={"X-Save-Token": "sekrit"})
    assert r.status_code == 200
    assert r.headers.get("etag")


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


@pytest.mark.parametrize("supernet", [
    "172.30.32.0/24",   # direct containing /24
    "172.30.0.0/16",    # broader
    "172.0.0.0/8",      # even broader
    "128.0.0.0/1",      # upper half of IPv4 (172.30.32.2 lives here)
])
def test_parse_trusted_proxies_drops_ingress_supernets(app_factory, caplog, supernet):
    """A CIDR that contains the ingress IP (172.30.32.2) is the same
    mistake as listing the ingress IP directly — the attacker forges
    XFF from any IP inside the range. Previously only exact-string
    matches were blocked; ipaddress-based containment check catches
    the supernet bypass."""
    import logging
    import main

    with caplog.at_level(logging.ERROR):
        result = main._parse_trusted_proxies({"trusted_proxies": supernet})

    assert result == []
    assert any("CIDR covers" in rec.message for rec in caplog.records)


def test_parse_trusted_proxies_accepts_non_ingress_cidrs(app_factory, caplog):
    """A CIDR that does NOT contain the ingress IP is kept. Common case:
    a real reverse proxy on the LAN."""
    import logging
    import main

    with caplog.at_level(logging.ERROR):
        result = main._parse_trusted_proxies(
            {"trusted_proxies": "10.0.0.0/24, 192.168.1.1, 172.31.0.0/16"}
        )

    assert result == ["10.0.0.0/24", "192.168.1.1", "172.31.0.0/16"]
    # No error logs for these.
    assert not any(rec.levelname == "ERROR" for rec in caplog.records)


def test_parse_trusted_proxies_rejects_unparseable_entries(app_factory, caplog):
    """Hostnames and garbage strings are rejected with a typed error —
    previously they'd silently slip through to uvicorn's
    forwarded_allow_ips, where behaviour is undefined."""
    import logging
    import main

    with caplog.at_level(logging.ERROR):
        result = main._parse_trusted_proxies(
            {"trusted_proxies": "proxy.local, not an ip, 300.0.0.1"}
        )

    assert result == []
    assert any("unparseable" in rec.message for rec in caplog.records)


def test_save_with_if_match_on_missing_file_omits_current_etag(
    app_factory, tmp_zones_file,
):
    """When the zones file is unreadable at etag-check time, the 412
    body omits `current_etag` entirely (rather than returning
    `"current_etag": null` which forces clients into defensive
    null-handling for a field that semantically means "resource
    missing"). The ETag response header is also absent."""
    app = app_factory({"allow_all_ips": True})
    client = TestClient(app)
    tmp_zones_file.unlink()

    r = client.post(
        "/save_zones",
        json=_valid_payload(),
        headers={"If-Match": '"stale-etag"'},
    )
    assert r.status_code == 412
    body = r.json()
    assert body == {"error": "precondition failed"}
    assert "current_etag" not in body
    assert "etag" not in r.headers
