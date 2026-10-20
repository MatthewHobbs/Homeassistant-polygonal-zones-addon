from collections import Counter
from email.utils import formatdate
import hashlib
import http.client
import io
import json
import os
import re
import threading
import time
import urllib.error

import pytest
from starlette.testclient import TestClient


@pytest.fixture(autouse=True)
def reset_save_rate_limit():
    """Each test gets a clean rate-limit state. Without this, accumulated
    failures leak between tests and later suites spuriously hit 429."""
    import main

    main._auth_failures.clear()
    yield
    main._auth_failures.clear()


@pytest.fixture
def app_factory(tmp_zones_file):
    import main

    def _build(options=None):
        app = main.generate_app(options or {})
        return app

    return _build


INGRESS = ("172.30.32.2", 5000)


def _ingress(app):
    return TestClient(app, client=INGRESS)


@pytest.fixture
def ingress_client(app_factory):
    return _ingress(app_factory({}))


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


def test_zones_json_returns_empty_collection(ingress_client, tmp_zones_file):
    response = ingress_client.get("/zones.json")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"type": "FeatureCollection", "features": []}


def test_zones_json_passes_file_bytes_through(ingress_client, tmp_zones_file):
    # Write a payload with whitespace formatting that JSONResponse would
    # normalise away — the passthrough must preserve the bytes verbatim.
    raw = b'{"type": "FeatureCollection",\n    "features": []\n}'
    tmp_zones_file.write_bytes(raw)
    response = ingress_client.get("/zones.json")
    assert response.status_code == 200
    assert response.content == raw


def test_zones_json_returns_etag(ingress_client, tmp_zones_file):
    response = ingress_client.get("/zones.json")
    assert response.status_code == 200
    # Strong validator: the quoted hex sha256 of the bytes served.
    expected = hashlib.sha256(tmp_zones_file.read_bytes()).hexdigest()
    assert response.headers["etag"] == f'"{expected}"'


def test_zones_json_returns_last_modified(ingress_client, tmp_zones_file):
    response = ingress_client.get("/zones.json")
    assert response.status_code == 200
    # RFC 7231 IMF-fixdate of the file's mtime, e.g. "Sun, 06 Nov 1994 08:49:37 GMT".
    expected = formatdate(os.stat(tmp_zones_file).st_mtime, usegmt=True)
    assert response.headers["last-modified"] == expected
    assert re.fullmatch(
        r"[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2} GMT",
        response.headers["last-modified"],
    )


def test_zones_json_returns_304_on_matching_if_none_match(ingress_client, tmp_zones_file):
    """Integration polling idiom (#119): pass the previous ETag via
    If-None-Match; the addon returns 304 with no body so the poll
    doesn't re-parse a FeatureCollection that hasn't changed."""
    etag = ingress_client.get("/zones.json").headers["etag"]
    r = ingress_client.get("/zones.json", headers={"If-None-Match": etag})
    assert r.status_code == 304
    # 304 must not carry a body per RFC 7232.
    assert r.content == b""
    # The validator headers MUST be repeated on 304 so clients can refresh
    # their cache state.
    assert r.headers["etag"] == etag
    assert r.headers.get("last-modified")
    assert r.headers["cache-control"] == "no-cache, no-store, must-revalidate"


def test_zones_json_returns_304_on_wildcard_if_none_match(ingress_client, tmp_zones_file):
    """If-None-Match: * matches any existing resource per RFC 7232."""
    r = ingress_client.get("/zones.json", headers={"If-None-Match": "*"})
    assert r.status_code == 304


def test_zones_json_returns_200_on_stale_if_none_match(ingress_client, tmp_zones_file):
    """If the client's cached ETag doesn't match, serve the full body."""
    r = ingress_client.get("/zones.json", headers={"If-None-Match": '"notarealtag"'})
    assert r.status_code == 200
    assert r.headers.get("etag") != '"notarealtag"'
    assert b"FeatureCollection" in r.content


def test_zones_json_if_none_match_handles_comma_separated_list(ingress_client, tmp_zones_file):
    """RFC 7232 permits a comma-separated list of candidate validators.
    Match on any one returns 304."""
    etag = ingress_client.get("/zones.json").headers["etag"]
    r = ingress_client.get(
        "/zones.json",
        headers={"If-None-Match": f'"stale", {etag}, "alsostale"'},
    )
    assert r.status_code == 304


def test_save_response_includes_new_etag(ingress_client, tmp_zones_file):
    # Initial GET ETag.
    initial = ingress_client.get("/zones.json").headers["etag"]
    # POST a new payload.
    r = ingress_client.post("/save_zones", json=_valid_payload())
    assert r.status_code == 200
    assert "etag" in r.headers
    # The new ETag must be in both the response header and the body, and
    # must differ from the pre-write value.
    assert r.headers["etag"] == r.json()["etag"]
    assert r.headers["etag"] != initial


def test_save_with_matching_if_match_succeeds(ingress_client, tmp_zones_file):
    etag = ingress_client.get("/zones.json").headers["etag"]
    r = ingress_client.post(
        "/save_zones",
        json=_valid_payload(),
        headers={"If-Match": etag},
    )
    assert r.status_code == 200


def test_save_with_stale_if_match_returns_412(ingress_client, tmp_zones_file):
    # First save advances the ETag.
    initial = ingress_client.get("/zones.json").headers["etag"]
    ingress_client.post("/save_zones", json=_valid_payload(name="One"))

    # Now post with the stale ETag — should be refused.
    r = ingress_client.post(
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


def test_save_without_if_match_still_works(ingress_client, tmp_zones_file):
    # Backwards-compat: clients that don't send If-Match keep working
    # (last-write-wins) so older curl scripts and the integration aren't
    # forced to learn ETags.
    r = ingress_client.post("/save_zones", json=_valid_payload())
    assert r.status_code == 200


def test_zones_json_sets_cache_headers(ingress_client):
    response = ingress_client.get("/zones.json")
    assert response.headers["cache-control"] == "no-cache, no-store, must-revalidate"
    # Cache-Control: no-store already says it; the HTTP/1.0 headers add nothing.
    assert "pragma" not in response.headers
    assert "expires" not in response.headers


def test_zones_json_no_wildcard_cors(ingress_client):
    response = ingress_client.get("/zones.json")
    assert response.headers.get("access-control-allow-origin") != "*"


def test_zones_json_blocks_unauthorized_client(restricted_client):
    response = restricted_client.get("/zones.json")
    assert response.status_code == 403
    assert response.json() == {"error": "not authorised"}


def test_save_zones_persists_valid_geojson(ingress_client, tmp_zones_file):
    payload = _valid_payload()
    response = ingress_client.post("/save_zones", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "etag" in body
    # The server stamps schema_version and backfills properties.id on write,
    # so the persisted file is a normalised superset of the incoming payload
    # rather than byte-identical.
    stored = json.loads(tmp_zones_file.read_text())
    assert stored["type"] == payload["type"]
    assert len(stored["features"]) == len(payload["features"])
    for sent, got in zip(payload["features"], stored["features"], strict=True):
        assert sent["type"] == got["type"]
        assert sent["geometry"] == got["geometry"]
        assert got["properties"]["name"] == sent["properties"]["name"]
        # id was backfilled (uuid4.hex = 32 chars of hex)
        assert isinstance(got["properties"]["id"], str)
        assert len(got["properties"]["id"]) == 32
    # schema_version stamped in the canonical foreign-member location the
    # companion integration reads (docs/ZONES_FORMAT.md), NOT bare top-level.
    assert stored["polygonal_zones"]["schema_version"] == 1
    assert "schema_version" not in stored


def test_save_zones_preserves_client_supplied_id(ingress_client, tmp_zones_file):
    """When the client supplies a stable properties.id (a drawn zone, for
    which the editor generates a client-side UUID), the server must
    round-trip it verbatim rather than overwriting with a fresh one —
    otherwise automations bound to the id would break on every save."""
    stable_id = "aaaa1111bbbb2222cccc3333dddd4444"
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "Home", "id": stable_id},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
                },
            }
        ],
    }
    r = ingress_client.post("/save_zones", json=payload)
    assert r.status_code == 200
    stored = json.loads(tmp_zones_file.read_text())
    assert stored["features"][0]["properties"]["id"] == stable_id


def test_save_zones_backfills_missing_id_and_non_dict_properties(
    ingress_client,
    tmp_zones_file,
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
    r = ingress_client.post("/save_zones", json=payload)
    assert r.status_code == 200
    stored = json.loads(tmp_zones_file.read_text())
    for feat in stored["features"]:
        assert isinstance(feat["properties"], dict)
        assert len(feat["properties"]["id"]) == 32


def test_save_zones_rejects_non_int_schema_version(ingress_client, tmp_zones_file):
    payload = {**_valid_payload(), "schema_version": "oops"}
    r = ingress_client.post("/save_zones", json=payload)
    assert r.status_code == 422
    assert "schema_version" in r.json()["detail"]


def test_save_zones_rejects_bool_schema_version(ingress_client, tmp_zones_file):
    """bool subclasses int in Python; must be explicitly rejected so a
    consumer doing `schema_version >= 1` doesn't silently pass on True."""
    payload = {**_valid_payload(), "schema_version": True}
    r = ingress_client.post("/save_zones", json=payload)
    assert r.status_code == 422
    assert "schema_version" in r.json()["detail"]


def test_save_zones_accepts_valid_schema_version(ingress_client, tmp_zones_file):
    payload = {**_valid_payload(), "schema_version": 1}
    r = ingress_client.post("/save_zones", json=payload)
    assert r.status_code == 200


def test_save_zones_accepts_nested_schema_version(ingress_client, tmp_zones_file):
    """The canonical input location is the foreign member; a nested value is
    validated and re-stamped nested (never duplicated top-level)."""
    payload = {**_valid_payload(), "polygonal_zones": {"schema_version": 1}}
    r = ingress_client.post("/save_zones", json=payload)
    assert r.status_code == 200
    stored = json.loads(tmp_zones_file.read_text())
    assert stored["polygonal_zones"]["schema_version"] == 1
    assert "schema_version" not in stored


def test_save_zones_rejects_non_int_nested_schema_version(ingress_client, tmp_zones_file):
    """A bad value in the canonical nested location is rejected, not ignored."""
    payload = {**_valid_payload(), "polygonal_zones": {"schema_version": "oops"}}
    r = ingress_client.post("/save_zones", json=payload)
    assert r.status_code == 422
    assert "schema_version" in r.json()["detail"]


def test_save_zones_rejects_non_string_id(ingress_client, tmp_zones_file):
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "x", "id": 42},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
                },
            }
        ],
    }
    r = ingress_client.post("/save_zones", json=payload)
    assert r.status_code == 422
    assert "properties.id" in r.json()["detail"]


def test_save_zones_rejects_empty_id(ingress_client, tmp_zones_file):
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "x", "id": ""},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
                },
            }
        ],
    }
    r = ingress_client.post("/save_zones", json=payload)
    assert r.status_code == 422
    assert "properties.id" in r.json()["detail"]


def test_save_zones_rejects_duplicate_ids(ingress_client, tmp_zones_file):
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
    r = ingress_client.post("/save_zones", json=payload)
    assert r.status_code == 422
    assert "duplicate" in r.json()["detail"]
    assert "id" in r.json()["detail"]


def test_save_zones_rejects_non_geojson(ingress_client, tmp_zones_file):
    original = tmp_zones_file.read_text()
    response = ingress_client.post("/save_zones", json={"evil": True})
    assert response.status_code == 422
    assert tmp_zones_file.read_text() == original


@pytest.mark.parametrize(
    "payload",
    [
        # Top-level not a FeatureCollection.
        [],
        "string",
        42,
        # Features not a list.
        {"type": "FeatureCollection", "features": "oops"},
        # Feature is not a dict.
        {"type": "FeatureCollection", "features": ["not a dict"]},
        # Feature missing 'type' key (treated as wrong type).
        {
            "type": "FeatureCollection",
            "features": [{"geometry": {"type": "Polygon", "coordinates": []}}],
        },
        # Geometry not a dict.
        {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": "oops"}]},
        # Geometry type not in Polygon/MultiPolygon.
        {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [0, 0]}}],
        },
        # Coordinates not a list.
        {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": "oops"}}
            ],
        },
        # Properties is a non-None non-dict value.
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": "oops",
                    "geometry": {"type": "Polygon", "coordinates": []},
                }
            ],
        },
    ],
)
def test_save_zones_rejects_malformed_shapes(ingress_client, tmp_zones_file, payload):
    original = tmp_zones_file.read_text()
    response = ingress_client.post("/save_zones", json=payload)
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


@pytest.mark.parametrize(
    "payload,expect_detail_contains",
    [
        # Ring integrity
        (_feature(_polygon([[[0, 0], [1, 0], [1, 1]]])), "at least 4 positions"),
        (_feature(_polygon([[[0, 0], [1, 0], [1, 1], [2, 2]]])), "not closed"),
        (_feature(_polygon("oops")), "Polygon coordinates must be a list"),
        (_feature(_polygon([])), "Polygon must have at least one ring"),
        (_feature(_polygon([[[0, 0], [1, 0], [1, 1], "oops"]])), "position must be a list"),
        # Ring itself isn't a list (Polygon.coordinates contains a non-list).
        (_feature(_polygon(["oops"])), "ring must be a list"),
        # Coordinate ranges
        (_feature(_polygon([[[181, 0], [1, 0], [1, 1], [181, 0]]])), "longitude out of range"),
        (_feature(_polygon([[[0, 91], [1, 0], [1, 1], [0, 91]]])), "latitude out of range"),
        # Non-numeric coordinates — bool is explicitly rejected even though it
        # subclasses int in Python (True == 1, False == 0 would otherwise pass).
        (
            _feature(_polygon([[[True, 0], [1, 0], [1, 1], [True, 0]]])),
            "longitude must be a number",
        ),
        (
            _feature(_polygon([[["oops", 0], [1, 0], [1, 1], ["oops", 0]]])),
            "longitude must be a number",
        ),
        # MultiPolygon shape
        (
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {"type": "MultiPolygon", "coordinates": "oops"},
                    }
                ],
            },
            "MultiPolygon coordinates must be a list",
        ),
        (
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {"type": "MultiPolygon", "coordinates": []},
                    }
                ],
            },
            "MultiPolygon must have at least one polygon",
        ),
        # Geometry type
        (
            _feature({"type": "LineString", "coordinates": [[0, 0], [1, 1]]}),
            "Polygon or MultiPolygon",
        ),
        # Properties shape
        (
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": "oops",
                        "geometry": _polygon([[[0, 0], [1, 0], [1, 1], [0, 0]]]),
                    }
                ],
            },
            "properties: must be a dict",
        ),
    ],
)
def test_save_zones_rejects_invalid_geometry_with_detail(
    ingress_client,
    tmp_zones_file,
    payload,
    expect_detail_contains,
):
    original = tmp_zones_file.read_text()
    r = ingress_client.post("/save_zones", json=payload)
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "invalid GeoJSON"
    # Detail is index-bearing so a client can point at the offending feature
    # / ring / position without the server echoing any coordinate values.
    assert expect_detail_contains in body["detail"]
    # File must not have been written.
    assert tmp_zones_file.read_text() == original


def test_save_zones_rejects_non_finite_coordinate(ingress_client, tmp_zones_file):
    """Infinity / NaN are semi-legal in Python's json (allow_nan=True) but
    meaningless for geodetic coordinates. Send as raw bytes — httpx on
    Python 3.14 refuses to serialize float('inf') via json=payload."""
    raw = (
        b'{"type":"FeatureCollection","features":[{"type":"Feature",'
        b'"properties":{},"geometry":{"type":"Polygon",'
        b'"coordinates":[[[Infinity,0],[1,0],[1,1],[Infinity,0]]]}}]}'
    )
    r = ingress_client.post(
        "/save_zones", content=raw, headers={"content-type": "application/json"}
    )
    assert r.status_code == 422
    assert "must be finite" in r.json()["detail"]


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
def test_save_zones_refuses_a_non_finite_number_anywhere(ingress_client, tmp_zones_file, constant):
    # Python's parser accepts these, but the editor's JSON.parse cannot load them back.
    original = tmp_zones_file.read_text()
    raw = (
        b'{"type":"FeatureCollection","features":[{"type":"Feature",'
        b'"properties":{"name":"x","weight":' + constant + b"},"
        b'"geometry":{"type":"Polygon","coordinates":[[[0,0],[1,0],[1,1],[0,0]]]}}]}'
    )
    r = ingress_client.post(
        "/save_zones", content=raw, headers={"content-type": "application/json"}
    )
    assert r.status_code == 422
    assert tmp_zones_file.read_text() == original


def test_save_etag_is_the_hash_of_the_bytes_written(ingress_client, tmp_zones_file, monkeypatch):
    # The file vanishing straight after the write must not turn the ETag into null.
    import main

    real_write = main.atomic_write_json
    written = {}

    def write_then_lose_it(path, data):
        result = real_write(path, data)
        written["bytes"] = tmp_zones_file.read_bytes()
        tmp_zones_file.unlink()
        return result

    monkeypatch.setattr(main, "atomic_write_json", write_then_lose_it)
    r = ingress_client.post("/save_zones", json=_valid_payload())
    assert r.status_code == 200
    expected = f'"{hashlib.sha256(written["bytes"]).hexdigest()}"'
    assert r.json()["etag"] == expected
    assert r.headers["etag"] == expected


def test_save_zones_rejects_vertex_cap_breach(ingress_client, tmp_zones_file):
    """Per-feature vertex cap. Build a single Polygon with 1001 vertices —
    the first-last-equal close duplicates one position, so generate 1000
    distinct points and close with the first. Cap is 1000 so this should
    trip at the 1001st total."""
    ring = [[i / 1000.0, 0.0] for i in range(1001)] + [[0.0, 0.0]]
    payload = _feature(_polygon([ring]))
    r = ingress_client.post("/save_zones", json=payload)
    assert r.status_code == 422
    assert "vertex count" in r.json()["detail"]


def test_save_zones_rejects_duplicate_zone_names(ingress_client, tmp_zones_file):
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
    r = ingress_client.post("/save_zones", json=payload)
    assert r.status_code == 422
    assert "duplicate" in r.json()["detail"]


def test_save_zones_accepts_unnamed_duplicates(ingress_client, tmp_zones_file):
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
    r = ingress_client.post("/save_zones", json=payload)
    assert r.status_code == 200


def test_save_zones_accepts_multipolygon(ingress_client, tmp_zones_file):
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
    r = ingress_client.post("/save_zones", json=payload)
    assert r.status_code == 200


def test_save_zones_accepts_null_properties(ingress_client, tmp_zones_file):
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
    r = ingress_client.post("/save_zones", json=payload)
    assert r.status_code == 200


def test_save_zones_rejects_body_over_cap_with_small_content_length(
    ingress_client,
    tmp_zones_file,
    monkeypatch,
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

    async def lying_body(self):
        return huge_body

    monkeypatch.setattr(Request, "body", lying_body)

    # content-length header small, triggering the post-read len(body) > cap.
    r = ingress_client.post(
        "/save_zones",
        content=original_payload,
        headers={"content-type": "application/json", "content-length": str(len(original_payload))},
    )
    assert r.status_code == 413
    assert r.json() == {"error": "payload too large"}


def test_save_zones_rejects_non_string_name(ingress_client, tmp_zones_file):
    payload = _valid_payload()
    payload["features"][0]["properties"]["name"] = {"x": 1}
    original = tmp_zones_file.read_text()
    response = ingress_client.post("/save_zones", json=payload)
    assert response.status_code == 422
    assert tmp_zones_file.read_text() == original


def test_save_zones_rejects_invalid_json(ingress_client, tmp_zones_file):
    original = tmp_zones_file.read_text()
    response = ingress_client.post(
        "/save_zones",
        content=b"not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    assert tmp_zones_file.read_text() == original


def test_save_zones_rejects_oversize_payload(ingress_client, tmp_zones_file):
    original = tmp_zones_file.read_text()
    huge = "x" * (512 * 1024 + 1)
    response = ingress_client.post(
        "/save_zones",
        content=huge.encode(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert tmp_zones_file.read_text() == original


def test_save_zones_returns_500_on_write_failure(ingress_client, tmp_zones_file, monkeypatch):
    import main

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(main, "atomic_write_json", boom)
    response = ingress_client.post("/save_zones", json=_valid_payload())
    assert response.status_code == 500


def test_save_zones_blocks_unauthorized_client(restricted_client, tmp_zones_file):
    original = tmp_zones_file.read_text()
    response = restricted_client.post("/save_zones", json={"evil": True})

    assert response.status_code == 403
    assert response.json() == {"error": "not authorised"}
    assert tmp_zones_file.read_text() == original


def test_save_token_strips_whitespace_symmetrically(app_factory, tmp_zones_file):
    """Both stored and provided tokens are stripped before compare, so a
    trailing space in the X-Save-Token header can't fail a correct token."""
    app = app_factory({"save_token": "sekrit"})
    client = TestClient(app)
    # Trailing whitespace on the provided token should be tolerated.
    r = client.post(
        "/save_zones",
        json=_valid_payload(),
        headers={"X-Save-Token": "sekrit "},
    )
    assert r.status_code == 200


def test_zones_json_rate_limit_shared_with_save_failures(app_factory, tmp_zones_file):
    """Failed /zones.json reads and failed /save_zones writes share one
    rate-limit bucket per IP. Without this, an attacker could double
    their per-window guess budget by rotating between GET and POST."""
    app = app_factory({"save_token": "sekrit"})
    client = TestClient(app)

    # Five bad POSTs.
    for _ in range(5):
        r = client.post("/save_zones", json=_valid_payload())
        assert r.status_code == 403
    # Five bad GETs — total failures now at the limit.
    for _ in range(5):
        r = client.get("/zones.json")
        assert r.status_code == 403

    # Next GET is rate-limited — and so is the next POST, because the
    # counter is shared.
    r = client.get("/zones.json")
    assert r.status_code == 429
    r = client.post("/save_zones", json=_valid_payload())
    assert r.status_code == 429


def test_uniform_auth_rejection_across_all_three_endpoints(app_factory, tmp_zones_file):
    """ADR 0002 row 3: every authorisation failure on /zones.json,
    /trackers.json and /save_zones gets the same status, body and headers,
    whatever the reason (IP not allowed, token missing, token wrong) and
    whichever endpoint — so a client can't infer from the response alone
    whether a token is even configured. Covers all three endpoints, with a
    token set and with none, as the row itself asks for."""
    expected_status = 403
    expected_body = {"error": "not authorised"}
    expected_cache_control = "no-store"

    def requests_for(client):
        yield "GET /zones.json", lambda **kw: client.get("/zones.json", **kw)
        yield "GET /trackers.json", lambda **kw: client.get("/trackers.json", **kw)
        yield (
            "POST /save_zones",
            lambda **kw: client.post("/save_zones", json=_valid_payload(), **kw),
        )

    # No token configured.
    client = TestClient(app_factory({}))
    for name, do in requests_for(client):
        r = do()
        assert r.status_code == expected_status, name
        assert r.json() == expected_body, name
        assert r.headers.get("cache-control") == expected_cache_control, name

    # Token configured, none presented.
    client = TestClient(app_factory({"save_token": "sekrit"}))
    for name, do in requests_for(client):
        r = do()
        assert r.status_code == expected_status, name
        assert r.json() == expected_body, name
        assert r.headers.get("cache-control") == expected_cache_control, name

    # Token configured, wrong one presented.
    client = TestClient(app_factory({"save_token": "sekrit"}))
    for name, do in requests_for(client):
        r = do(headers={"X-Save-Token": "wrong"})
        assert r.status_code == expected_status, name
        assert r.json() == expected_body, name
        assert r.headers.get("cache-control") == expected_cache_control, name


def test_zones_json_ingress_bypasses_token(app_factory, tmp_zones_file):
    """Ingress (172.30.32.2) reads remain unauthenticated even when a
    token is set — the HA UI's zones.json fetch must keep working."""
    import main

    app = main.generate_app({"save_token": "sekrit"})
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
    from starlette.testclient import TestClient as TC

    import main

    app = main.generate_app({"save_token": "s3cret"})
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


def test_index_serves_static_html(ingress_client):
    response = ingress_client.get("/")
    assert response.status_code == 200
    # index.html is served as a static file, not a template: the colour
    # comes from /config.json, so no placeholder may survive in the page.
    assert "<title>Polygonal zones: Edit zones</title>" in response.text
    assert "{{ ZONE_COLOUR }}" not in response.text
    assert "ZONE_COLOUR" not in response.text


def test_config_json_default(ingress_client):
    response = ingress_client.get("/config.json")
    assert response.status_code == 200
    # config.yaml's default, so an options.json without the key looks the same.
    assert response.json() == {"zone_colour": "purple", "theme": "auto"}


def test_config_json_respects_option(app_factory):
    client = TestClient(app_factory({"zone_colour": "red"}), client=INGRESS)
    response = client.get("/config.json")
    assert response.status_code == 200
    assert response.json() == {"zone_colour": "red", "theme": "auto"}


def test_config_json_passes_arbitrary_string_safely(app_factory):
    # JSONResponse encodes the value safely — no template injection surface.
    malicious = 'red"; alert(1); //'
    client = TestClient(app_factory({"zone_colour": malicious}), client=INGRESS)
    response = client.get("/config.json")
    assert response.status_code == 200
    assert response.json() == {"zone_colour": malicious, "theme": "auto"}


@pytest.mark.parametrize("value", ["auto", "light", "dark"])
def test_config_json_theme_passthrough(app_factory, value):
    client = TestClient(app_factory({"theme": value}), client=INGRESS)
    assert client.get("/config.json").json()["theme"] == value


def test_config_json_theme_invalid_falls_back_to_auto(app_factory):
    # HA's schema enforces the enum at the supervisor, but defence in depth:
    # an unexpected value (e.g. someone hand-edited options.json) must not
    # propagate to the frontend as-is.
    client = TestClient(app_factory({"theme": "neon"}), client=INGRESS)
    assert client.get("/config.json").json()["theme"] == "auto"


def test_index_blocks_unauthorized_client(restricted_client):
    response = restricted_client.get("/")
    assert response.status_code == 403
    assert response.json() == {"error": "not authorised"}


def test_config_json_blocks_unauthorized_client(restricted_client):
    response = restricted_client.get("/config.json")
    assert response.status_code == 403


def test_healthz_returns_ok_without_authz(restricted_client):
    response = restricted_client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"


def test_security_headers_applied(ingress_client):
    """SecurityHeadersMiddleware sets Referrer-Policy, nosniff,
    X-Frame-Options, and a CSP on every response."""
    response = ingress_client.get("/zones.json")
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    csp = response.headers["content-security-policy"]
    # Key invariants of the CSP. Full string is intentionally not pinned
    # so we don't have to update every test when a directive is tuned.
    assert "default-src 'self'" in csp
    # frame-ancestors: only 'self' (for HA ingress same-origin) plus Nabu
    # Casa remote access; *.home-assistant.io is HA's docs domain, not an
    # iframe-embedding origin.
    assert "frame-ancestors 'self' https://*.ui.nabu.casa" in csp
    assert "https://*.home-assistant.io" not in csp
    # The editor has no inline script, so script-src needs no
    # 'unsafe-inline'. style-src still needs it for Leaflet's injected tile
    # transforms.
    assert "script-src 'self';" in csp
    assert "'unsafe-inline'" not in csp.split("style-src")[0]
    # The map libraries are vendored; guard against a CDN dep creeping back.
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
        d.strip().split() for d in csp.split(";") if d.strip().startswith("img-src")
    )
    assert any(t == "https://server.arcgisonline.com" for t in img_src_sources)


def test_security_headers_applied_to_static_files(ingress_client):
    """Middleware runs on static assets too — clickjacking / CSP apply
    to index.html and the JS bundles."""
    response = ingress_client.get("/")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert "frame-ancestors" in response.headers["content-security-policy"]


def test_static_file_served_to_authorized_client(ingress_client):
    response = ingress_client.get("/js/map.js")
    assert response.status_code == 200
    assert "generate_map" in response.text


def test_static_file_blocked_for_unauthorized_client(restricted_client):
    response = restricted_client.get("/js/map.js")
    assert response.status_code == 403


def test_static_traversal_attempt_rejected(ingress_client):
    # StaticFiles normalises and rejects paths that escape the root directory.
    response = ingress_client.get("/..%2F..%2Fapp%2Fconst.py")
    assert response.status_code == 404


def test_zones_json_returns_parseable_geojson_when_populated(ingress_client, tmp_zones_file):
    """The companion integration's first action on every poll is
    json.loads(body) + a type check. Byte-identity passthrough isn't
    enough — the response must remain parseable GeoJSON with the right
    Content-Type.
    """
    payload = _valid_payload(name="Home")
    r = ingress_client.post("/save_zones", json=payload)
    assert r.status_code == 200

    r = ingress_client.get("/zones.json")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/json"
    data = r.json()
    assert data["type"] == "FeatureCollection"
    assert isinstance(data["features"], list)
    assert data["features"][0]["properties"]["name"] == "Home"


def test_zones_json_returns_503_when_file_unreadable(ingress_client, tmp_zones_file):
    """An unreadable file is a 503 with a JSON body and a log line, not a
    bare 500."""
    tmp_zones_file.unlink()
    response = ingress_client.get("/zones.json")
    assert response.status_code == 503
    assert response.json() == {"error": "zones file unreadable"}


def test_healthz_returns_503_when_zones_missing(restricted_client, tmp_zones_file):
    """Healthcheck reflects zones-file readability so the Docker
    HEALTHCHECK surfaces a broken state instead of staying green while
    /zones.json fails."""
    tmp_zones_file.unlink()
    r = restricted_client.get("/healthz")
    assert r.status_code == 503
    assert r.text == "zones file unreadable"


def test_save_zones_rate_limits_after_10_failures(app_factory, tmp_zones_file):
    """When save_token is set, repeated unauthorised attempts eventually
    return 429 instead of 403 — defends against LAN token-brute-force."""
    app = app_factory({"save_token": "sekrit"})
    client = TestClient(app)

    for attempt in range(10):
        r = client.post("/save_zones", json=_valid_payload())
        assert r.status_code == 403, f"attempt {attempt} unexpectedly {r.status_code}"

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
    t[0] += main._AUTH_FAILURE_WINDOW_SECONDS + 1

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


def test_parse_trusted_proxies_handles_empty_and_list(app_factory):
    import main

    assert main._parse_trusted_proxies({}) == []
    assert main._parse_trusted_proxies({"trusted_proxies": ""}) == []
    assert main._parse_trusted_proxies({"trusted_proxies": "10.0.0.5"}) == ["10.0.0.5"]
    assert main._parse_trusted_proxies({"trusted_proxies": " 192.168.1.1 ,10.0.0.5 "}) == [
        "192.168.1.1",
        "10.0.0.5",
    ]


@pytest.mark.parametrize(
    "dangerous",
    [
        "*",
        "0.0.0.0",
        "0.0.0.0/0",
        "::",
        "::/0",
        "172.30.32.2",
    ],
)
def test_parse_trusted_proxies_drops_dangerous_values(app_factory, caplog, dangerous):
    """Wildcards and the ingress IP are never trusted as proxies: a wildcard
    lets any client choose the address it is rate-limited as. The parser
    drops them and logs an error."""
    import logging

    import main

    with caplog.at_level(logging.ERROR):
        result = main._parse_trusted_proxies({"trusted_proxies": dangerous})

    assert result == []
    # The message names the rule that fired, not just any rule.
    expected = "covers the HA ingress IP" if dangerous == "172.30.32.2" else "means every address"
    assert any(expected in rec.message for rec in caplog.records)


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


@pytest.mark.parametrize(
    "supernet",
    [
        "172.30.32.0/24",  # direct containing /24
        "172.30.0.0/16",  # broader
        "172.0.0.0/8",  # even broader
        "128.0.0.0/1",  # upper half of IPv4 (172.30.32.2 lives here)
    ],
)
def test_parse_trusted_proxies_drops_ingress_supernets(app_factory, caplog, supernet):
    """A CIDR that contains the ingress IP (172.30.32.2) is the same
    mistake as listing the ingress IP directly: a containment check, not a
    string match, so every supernet is caught."""
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
    """Hostnames and garbage strings are rejected with a logged error rather
    than handed to uvicorn's forwarded_allow_ips, where behaviour is
    undefined."""
    import logging

    import main

    with caplog.at_level(logging.ERROR):
        result = main._parse_trusted_proxies(
            {"trusted_proxies": "proxy.local, not an ip, 300.0.0.1"}
        )

    assert result == []
    assert any("unparseable" in rec.message for rec in caplog.records)


def test_save_with_if_match_on_missing_file_omits_current_etag(
    app_factory,
    tmp_zones_file,
):
    """When the zones file is unreadable at etag-check time, the 412
    body omits `current_etag` entirely (rather than returning
    `"current_etag": null` which forces clients into defensive
    null-handling for a field that semantically means "resource
    missing"). The ETag response header is also absent."""
    client = TestClient(app_factory({}), client=INGRESS)
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


# --- Tracker overlay (/trackers.json) --------------------------------------
#
# The overlay plots live tracker positions so a user can see *why* a zone
# misclassifies a device. It reaches Home Assistant through the Supervisor
# proxy, so the tests below care most about what it refuses to do: fetch
# anything not explicitly opted in, trust an entity id from config, or hand
# positions to a client the zones file itself wouldn't be handed to.


def _ha_state(entity_id, lat, lon, acc=8, name="Thing", state="home"):
    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": {
            "latitude": lat,
            "longitude": lon,
            "gps_accuracy": acc,
            "friendly_name": name,
            # A real state object carries far more than the map needs; the
            # route must drop all of it rather than pass it through.
            "battery_level": 42,
            "source_type": "gps",
            "secret_note": "private",
        },
    }


def test_overlay_entities_empty_by_default():
    """Opt-in: with no option set, nothing is ever requested or returned."""
    import main

    assert main.overlay_entities({}) == []


@pytest.mark.parametrize(
    "raw,expected",
    [
        (["device_tracker.a"], ["device_tracker.a"]),
        ("device_tracker.a, device_tracker.b", ["device_tracker.a", "device_tracker.b"]),
        ("device_tracker.a\ndevice_tracker.b", ["device_tracker.a", "device_tracker.b"]),
        (["device_tracker.a", "device_tracker.a"], ["device_tracker.a"]),
        (["  device_tracker.a  ", ""], ["device_tracker.a"]),
    ],
)
def test_overlay_entities_accepts_list_or_string(raw, expected):
    import main

    assert main.overlay_entities({"overlay_entities": raw}) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "../../secret",
        "device_tracker.a/../b",
        "device_tracker.a?x=1",
        "DEVICE_TRACKER.A",
        "nodot",
        "device_tracker.",
        ".a",
        "device_tracker.a b",
        "device_tracker.a#frag",
        "http://evil/x",
    ],
)
def test_overlay_entities_rejects_anything_not_an_entity_id(bad):
    """These ids end up in a URL path — only HA's own shape is accepted."""
    import main

    assert main.overlay_entities({"overlay_entities": [bad]}) == []


def test_overlay_entities_rejects_non_list_option():
    import main

    assert main.overlay_entities({"overlay_entities": {"a": 1}}) == []


def test_overlay_entities_capped():
    import main

    many = [f"device_tracker.d{i}" for i in range(main.MAX_OVERLAY_ENTITIES + 5)]
    assert len(main.overlay_entities({"overlay_entities": many})) == main.MAX_OVERLAY_ENTITIES


def test_overlay_point_keeps_only_map_fields_and_rounds():
    """Everything the map doesn't need is dropped before it crosses the wire."""
    import main

    p = main._overlay_point(
        "device_tracker.a",
        _ha_state("device_tracker.a", 51.947133802662975, -0.6274616792121955, 2.34),
    )
    assert p == {
        "entity_id": "device_tracker.a",
        "name": "Thing",
        "state": "home",
        "latitude": 51.9471,
        "longitude": -0.6275,
        "gps_accuracy": 2.3,
    }
    assert "battery_level" not in p and "secret_note" not in p


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "not a dict",
        {},
        {"attributes": "not a dict"},
        {"attributes": {}},
        {"attributes": {"latitude": 1}},
        {"attributes": {"latitude": "51.9", "longitude": -0.6}},
        {"attributes": {"latitude": True, "longitude": -0.6}},
        {"attributes": {"latitude": float("nan"), "longitude": -0.6}},
        {"attributes": {"latitude": float("inf"), "longitude": -0.6}},
    ],
)
def test_overlay_point_rejects_unusable_payloads(payload):
    import main

    assert main._overlay_point("device_tracker.a", payload) is None


@pytest.mark.parametrize("acc", [None, "8", True, float("nan")])
def test_overlay_point_tolerates_bad_accuracy(acc):
    """A missing or nonsense accuracy must not discard an otherwise good fix."""
    import main

    p = main._overlay_point(
        "device_tracker.a",
        {
            "state": "home",
            "attributes": {"latitude": 51.9, "longitude": -0.6, "gps_accuracy": acc},
        },
    )
    assert p is not None and p["gps_accuracy"] is None


def test_overlay_point_falls_back_to_entity_id_for_name():
    import main

    p = main._overlay_point(
        "device_tracker.a",
        {
            "attributes": {"latitude": 51.9, "longitude": -0.6},
        },
    )
    assert p["name"] == "device_tracker.a" and p["state"] == "unknown"


def test_trackers_json_reports_not_configured_by_default(ingress_client):
    r = ingress_client.get("/trackers.json")
    assert r.status_code == 200
    assert r.json() == {"configured": False, "trackers": []}
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, 60),
        (60, 60),
        (0, 0),
        (10, 10),
        (3600, 3600),
        (1, 10),
        (9, 10),
        (86400, 3600),
    ],
)
def test_tracker_refresh_seconds(raw, expected):
    import main

    options = {} if raw is None else {"tracker_refresh_seconds": raw}
    assert main.tracker_refresh_seconds(options) == expected


def test_tracker_refresh_seconds_explicit_null_uses_default():
    import main

    assert main.tracker_refresh_seconds({"tracker_refresh_seconds": None}) == 60


@pytest.mark.parametrize("bad", [True, False, -5, 30.5, "60", [60]])
def test_tracker_refresh_seconds_rejects_non_seconds(bad, caplog):
    """`true` is not one second and "60" is not sixty: both fall back, loudly."""
    import logging

    import main

    with caplog.at_level(logging.WARNING):
        assert main.tracker_refresh_seconds({"tracker_refresh_seconds": bad}) == 60
    assert any("tracker_refresh_seconds" in rec.message for rec in caplog.records)


def test_tracker_refresh_seconds_logs_when_clamped(caplog):
    import logging

    import main

    with caplog.at_level(logging.WARNING):
        assert main.tracker_refresh_seconds({"tracker_refresh_seconds": 2}) == 10
    assert any("outside 10-3600" in rec.message for rec in caplog.records)


def test_trackers_json_tells_the_editor_how_often_to_poll(app_factory, monkeypatch):
    """The editor learns the interval from the same response it polls, and the
    response must not be cached or every later poll sees the first position."""
    import main

    monkeypatch.setenv("SUPERVISOR_TOKEN", "stub-token")
    monkeypatch.setattr(
        main, "_fetch_state", lambda eid, _t: _ha_state(eid, 51.9471338, -0.6274617)
    )
    client = _ingress(
        app_factory(
            {
                "overlay_entities": ["device_tracker.car"],
                "tracker_refresh_seconds": 15,
            }
        )
    )
    r = client.get("/trackers.json")
    assert r.json()["refresh_seconds"] == 15
    assert r.headers["cache-control"] == "no-store"


def test_a_bad_overlay_entity_is_warned_about_once_not_on_every_poll(app_factory, caplog):
    import logging

    client = _ingress(app_factory({"overlay_entities": ["not an entity"]}))
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert client.get("/trackers.json").json()["configured"] is False
    assert sum("Ignoring overlay entity" in r.getMessage() for r in caplog.records) == 1


def test_trackers_json_blocks_unauthorized_client(restricted_client):
    """Positions are at least as sensitive as the zones file — same gate,
    same uniform rejection (ADR 0002 row 3)."""
    r = restricted_client.get("/trackers.json")
    assert r.status_code == 403
    assert r.json() == {"error": "not authorised"}


def test_trackers_json_without_supervisor_token(app_factory, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    client = _ingress(
        app_factory(
            {
                "overlay_entities": ["device_tracker.a"],
            }
        )
    )
    body = client.get("/trackers.json").json()
    assert body == {"configured": True, "trackers": [], "error": "no_supervisor_token"}


def test_trackers_json_returns_only_opted_in_entities(app_factory, monkeypatch):
    import main

    monkeypatch.setenv("SUPERVISOR_TOKEN", "stub-token")
    asked = []

    def fake_fetch(entity_id, token):
        asked.append((entity_id, token))
        return _ha_state(entity_id, 51.9471338, -0.6274617, 5, name=entity_id)

    monkeypatch.setattr(main, "_fetch_state", fake_fetch)
    client = _ingress(
        app_factory(
            {
                "overlay_entities": ["device_tracker.car", "sensor.not_a_tracker"],
            }
        )
    )
    body = client.get("/trackers.json").json()
    assert body["configured"] is True
    assert [t["entity_id"] for t in body["trackers"]] == [
        "device_tracker.car",
        "sensor.not_a_tracker",
    ]
    # Only the opted-in ids were ever asked for, exactly once each, with the
    # add-on's own token. Fetches run concurrently on a shared thread pool
    # (_OVERLAY_POOL), so the order fake_fetch is actually called in isn't
    # guaranteed to match submission order — assert counts, not order (a
    # plain set would also hide a duplicate-fetch regression).
    assert Counter(a[0] for a in asked) == Counter(["device_tracker.car", "sensor.not_a_tracker"])
    assert {a[1] for a in asked} == {"stub-token"}


def test_trackers_json_skips_entities_that_fail_to_resolve(app_factory, monkeypatch):
    """One unreachable entity must not empty the whole overlay."""
    import main

    monkeypatch.setenv("SUPERVISOR_TOKEN", "stub-token")

    def fake_fetch(entity_id, token):
        if entity_id.endswith("broken"):
            return None
        return _ha_state(entity_id, 51.9471338, -0.6274617)

    monkeypatch.setattr(main, "_fetch_state", fake_fetch)
    client = _ingress(
        app_factory(
            {
                "overlay_entities": ["device_tracker.broken", "device_tracker.ok"],
            }
        )
    )
    body = client.get("/trackers.json").json()
    assert [t["entity_id"] for t in body["trackers"]] == ["device_tracker.ok"]
    # ...and is named, so a polling editor keeps its last position instead of
    # reading its absence as "no position now".
    assert body["unavailable"] == ["device_tracker.broken"]


def test_trackers_json_drops_entities_that_report_no_position(app_factory, monkeypatch):
    """Reachable but without coordinates is a real answer, not an outage."""
    import main

    monkeypatch.setenv("SUPERVISOR_TOKEN", "stub-token")

    def fake_fetch(entity_id, token):
        if entity_id.endswith("indoors"):
            return {"entity_id": entity_id, "state": "home", "attributes": {}}
        return _ha_state(entity_id, 51.9471338, -0.6274617)

    monkeypatch.setattr(main, "_fetch_state", fake_fetch)
    client = _ingress(
        app_factory(
            {
                "overlay_entities": ["device_tracker.indoors", "device_tracker.ok"],
            }
        )
    )
    body = client.get("/trackers.json").json()
    assert [t["entity_id"] for t in body["trackers"]] == ["device_tracker.ok"]
    assert body["unavailable"] == []


def test_trackers_json_holds_the_whole_overlay_to_one_deadline(app_factory, monkeypatch):
    """A stalled entity costs one timeout for the whole poll, not one per entity,
    and is reported unavailable rather than making the fast ones wait."""
    import main

    monkeypatch.setenv("SUPERVISOR_TOKEN", "stub-token")
    monkeypatch.setattr(main, "OVERLAY_TIMEOUT_SECONDS", 0.2)
    release = threading.Event()
    stalled_returned = threading.Event()

    def fake_fetch(entity_id, token):
        if entity_id.endswith("stalled"):
            # Held until the test has its response: if the route waited for
            # this fetch, the response could only come after it returned.
            release.wait(timeout=10)
            stalled_returned.set()
        return _ha_state(entity_id, 51.9471338, -0.6274617)

    monkeypatch.setattr(main, "_fetch_state", fake_fetch)
    client = _ingress(
        app_factory(
            {
                "overlay_entities": ["device_tracker.stalled", "device_tracker.ok"],
            }
        )
    )
    try:
        body = client.get("/trackers.json").json()
        assert not stalled_returned.is_set(), "the response waited for the stalled fetch"
    finally:
        release.set()
    assert [t["entity_id"] for t in body["trackers"]] == ["device_tracker.ok"]
    assert body["unavailable"] == ["device_tracker.stalled"]


def test_trackers_json_fetches_entities_concurrently(app_factory, monkeypatch):
    """Every fetch waits at a barrier that only a full pool of concurrent
    fetches can pass, so a sequential gather reports them all unavailable.
    Keeps the response order of the option."""
    import main

    monkeypatch.setenv("SUPERVISOR_TOKEN", "stub-token")
    barrier = threading.Barrier(main.OVERLAY_FETCH_WORKERS, timeout=2)

    def fake_fetch(entity_id, token):
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            return None
        return _ha_state(entity_id, 51.9471338, -0.6274617)

    monkeypatch.setattr(main, "_fetch_state", fake_fetch)
    entities = [f"device_tracker.t{i:02d}" for i in range(2 * main.OVERLAY_FETCH_WORKERS)]
    client = _ingress(app_factory({"overlay_entities": entities}))
    body = client.get("/trackers.json").json()
    assert body["unavailable"] == []
    assert [t["entity_id"] for t in body["trackers"]] == entities


def test_trackers_json_caps_fetches_in_flight_across_all_polls(app_factory, monkeypatch):
    """Ten editors polling at once share one pool: the Supervisor never sees
    more than OVERLAY_FETCH_WORKERS calls in flight, whatever the tab count."""
    from concurrent.futures import ThreadPoolExecutor as _Clients
    import threading

    import main

    monkeypatch.setenv("SUPERVISOR_TOKEN", "stub-token")
    lock = threading.Lock()
    in_flight = {"now": 0, "peak": 0}

    def fake_fetch(entity_id, token):
        with lock:
            in_flight["now"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
        time.sleep(0.05)
        with lock:
            in_flight["now"] -= 1
        return _ha_state(entity_id, 51.9471338, -0.6274617)

    monkeypatch.setattr(main, "_fetch_state", fake_fetch)
    entities = [f"device_tracker.t{i}" for i in range(8)]
    client = _ingress(app_factory({"overlay_entities": entities}))
    with _Clients(max_workers=10) as clients:
        bodies = list(clients.map(lambda _: client.get("/trackers.json").json(), range(10)))
    assert all(len(b["trackers"]) == 8 and b["unavailable"] == [] for b in bodies)
    assert in_flight["peak"] <= main.OVERLAY_FETCH_WORKERS


def test_fetch_state_returns_none_on_transport_error(monkeypatch):
    import main

    def boom(*_a, **_k):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(main.urllib.request, "urlopen", boom)
    assert main._fetch_state("device_tracker.a", "tok") is None


def test_fetch_state_parses_a_good_response(monkeypatch):
    import main

    class _Resp:
        def read(self):
            return json.dumps({"state": "home", "attributes": {}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)
    assert main._fetch_state("device_tracker.a", "tok") == {"state": "home", "attributes": {}}
    assert captured["url"] == f"{main.SUPERVISOR_API}/states/device_tracker.a"
    assert captured["auth"] == "Bearer tok"
    assert captured["timeout"] == main.OVERLAY_TIMEOUT_SECONDS


class _Body(io.BytesIO):
    """A urlopen response: a readable body that is its own context manager."""


def _http_error(code):
    return urllib.error.HTTPError("http://supervisor", code, "err", {}, io.BytesIO(b"{}"))


# What the Supervisor proxy can answer for one entity, by the entity's name.
_FETCH_FAILURES = {
    "unauthorised": _http_error(401),
    "server_error": _http_error(500),
    "not_json": b"<html>Bad gateway</html>",
    "timed_out": TimeoutError("timed out"),
    "bad_status_line": http.client.BadStatusLine("garbage"),
    "cut_short": http.client.IncompleteRead(b"{", 10),
}


def _fake_urlopen(req, timeout=None):
    name = req.full_url.rsplit(".", 1)[1]
    outcome = _FETCH_FAILURES.get(name)
    if isinstance(outcome, BaseException):
        raise outcome
    if outcome is None:
        outcome = json.dumps(_ha_state(f"device_tracker.{name}", 51.9, -0.6)).encode()
    return _Body(outcome)


@pytest.mark.parametrize("name", list(_FETCH_FAILURES))
def test_fetch_state_returns_none_on_each_failure(monkeypatch, name):
    import main

    monkeypatch.setattr(main.urllib.request, "urlopen", _fake_urlopen)
    assert main._fetch_state(f"device_tracker.{name}", "tok") is None


def test_trackers_json_survives_every_fetch_failure(app_factory, monkeypatch):
    import main

    monkeypatch.setenv("SUPERVISOR_TOKEN", "stub-token")
    monkeypatch.setattr(main.urllib.request, "urlopen", _fake_urlopen)
    failing = [f"device_tracker.{name}" for name in _FETCH_FAILURES]
    client = _ingress(app_factory({"overlay_entities": [*failing, "device_tracker.ok"]}))
    r = client.get("/trackers.json")
    assert r.status_code == 200
    assert [t["entity_id"] for t in r.json()["trackers"]] == ["device_tracker.ok"]
    assert r.json()["unavailable"] == failing


def test_trackers_json_shares_the_rate_limit_bucket(app_factory, tmp_zones_file):
    """A failed /trackers.json read counts toward the same per-IP bucket as
    /save_zones and /zones.json, and is refused once the bucket is full.

    Without this the overlay would be a fresh guess budget for the same token:
    an attacker locked out of the other two paths could keep trying here.
    """
    app = app_factory(
        {
            "save_token": "sekrit",
            "overlay_entities": ["device_tracker.a"],
        }
    )
    client = TestClient(app)

    import main

    # Exhaust the bucket on the write path with wrong-token POSTs.
    for _ in range(main._AUTH_FAILURE_LIMIT):
        client.post("/save_zones", json=_valid_payload(), headers={"X-Save-Token": "wrong"})

    # Correct token now, but the bucket is full — the overlay is refused too.
    r = client.get("/trackers.json", headers={"X-Save-Token": "sekrit"})
    assert r.status_code == 429
    assert r.json() == {"error": "too many failed attempts"}


@pytest.mark.parametrize("path", ["/save_zones", "/zones.json", "/trackers.json", "/"])
def test_rate_limit_refusal_is_logged_on_every_gated_route(app_factory, caplog, path):
    import logging

    import main

    client = TestClient(app_factory({"save_token": "sekrit"}))
    for _ in range(main._AUTH_FAILURE_LIMIT):
        client.get("/zones.json")
    with caplog.at_level(logging.WARNING):
        r = client.request("POST" if path == "/save_zones" else "GET", path, json={})
    assert r.status_code == 429
    assert r.json() == {"error": "too many failed attempts"}
    # A cached 429 would keep refusing after the window has passed.
    assert r.headers.get("cache-control") == "no-store"
    assert f"Rate limit hit on {path} for testclient" in caplog.text


def test_healthz_returns_503_when_zones_file_cannot_be_read(restricted_client, tmp_zones_file):
    """A file that exists but can't be opened (ownership drift) is unhealthy too."""
    if os.geteuid() == 0:
        pytest.skip("root reads through file modes")
    tmp_zones_file.chmod(0)
    try:
        r = restricted_client.get("/healthz")
    finally:
        tmp_zones_file.chmod(0o644)
    assert r.status_code == 503


@pytest.mark.parametrize("entry", ["0::/0", "::0/0", "0.0.0.0/00", "0.0.0.0", "::"])
def test_parse_trusted_proxies_refuses_any_spelling_of_everything(app_factory, entry, caplog):
    import logging

    import main

    with caplog.at_level(logging.ERROR):
        assert main._parse_trusted_proxies({"trusted_proxies": entry}) == []
    assert any("means every address" in rec.message for rec in caplog.records)


def test_save_without_credentials_is_403_not_415(restricted_client):
    r = restricted_client.post("/save_zones", content=b"{}", headers={"Content-Type": "text/plain"})
    assert r.status_code == 403


def test_parse_trusted_proxies_keeps_a_range_that_starts_at_zero(app_factory):
    import main

    assert main._parse_trusted_proxies({"trusted_proxies": "0.0.0.0/8"}) == ["0.0.0.0/8"]


@pytest.mark.parametrize("path", ["/", "/js/map.js", "/css/style.css"])
def test_static_files_are_revalidated_so_a_client_fix_reaches_browsers(ingress_client, path):
    r = ingress_client.get(path)
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-cache"


def test_routes_that_forbid_caching_keep_their_header(ingress_client):
    assert (
        ingress_client.get("/zones.json").headers["cache-control"]
        == "no-cache, no-store, must-revalidate"
    )


# --- LAN access needs save_token; allow_all_ips grants nothing ---------------

LAN_ROUTES = [
    ("GET", "/"),
    ("GET", "/zones.json"),
    ("GET", "/trackers.json"),
    ("POST", "/save_zones"),
]


def _send(client, method, path, **kw):
    if method == "POST":
        return client.post(path, json=_valid_payload(), **kw)
    return client.get(path, **kw)


@pytest.mark.parametrize("options", [{}, {"allow_all_ips": True}])
@pytest.mark.parametrize(("method", "path"), LAN_ROUTES)
def test_lan_request_without_a_configured_token_is_refused(app_factory, options, method, path):
    r = _send(TestClient(app_factory(options)), method, path)
    assert r.status_code == 403
    assert r.json() == {"error": "not authorised"}


@pytest.mark.parametrize(
    ("method", "path"), [*LAN_ROUTES, ("GET", "/config.json"), ("GET", "/js/map.js")]
)
def test_lan_request_needs_the_token_when_one_is_configured(app_factory, method, path):
    client = TestClient(app_factory({"save_token": "s3cret"}))
    assert _send(client, method, path).status_code == 403
    assert _send(client, method, path, headers={"X-Save-Token": "wrong"}).status_code == 403
    assert _send(client, method, path, headers={"X-Save-Token": "s3cret"}).status_code == 200


@pytest.mark.parametrize("options", [{}, {"save_token": "s3cret"}, {"allow_all_ips": True}])
@pytest.mark.parametrize(("method", "path"), [*LAN_ROUTES, ("GET", "/config.json")])
def test_ingress_needs_no_token(app_factory, options, method, path):
    client = TestClient(app_factory(options), client=INGRESS)
    assert _send(client, method, path).status_code == 200


def test_a_link_from_another_site_still_opens_the_editor(app_factory):
    client = TestClient(app_factory({"save_token": "s3cret"}))
    headers = {"Sec-Fetch-Site": "cross-site", "X-Save-Token": "s3cret"}
    assert client.get("/", headers=headers).status_code == 200


def test_static_files_share_the_failure_budget(app_factory):
    # Otherwise "/" would be an unlimited oracle for guessing the token.
    import main

    client = TestClient(app_factory({"save_token": "s3cret"}))
    for _ in range(main._AUTH_FAILURE_LIMIT):
        assert client.get("/", headers={"X-Save-Token": "guess"}).status_code == 403
    assert client.get("/", headers={"X-Save-Token": "s3cret"}).status_code == 429
    assert client.get("/zones.json", headers={"X-Save-Token": "s3cret"}).status_code == 429


@pytest.mark.parametrize(
    ("options", "headers", "expected"),
    [
        ({"allow_all_ips": True}, {}, "not ingress, and no save_token is configured"),
        ({"save_token": "  "}, {}, "not ingress, and no save_token is configured"),
        ({"save_token": "x"}, {}, "missing X-Save-Token"),
        ({"save_token": "x"}, {"x-save-token": "y"}, "wrong X-Save-Token"),
        ({"save_token": "x"}, {"x-save-token": "x"}, None),
    ],
)
def test_authorise_returns_the_log_text(request_factory, options, headers, expected):
    import main

    request = request_factory("10.0.0.9")
    request.headers = headers
    assert main.authorise(options, request) == expected
