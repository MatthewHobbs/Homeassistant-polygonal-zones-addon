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


def test_zones_json_last_modified_comes_from_the_file_it_read(
    ingress_client, tmp_zones_file, monkeypatch
):
    # A save that lands between the open and a path stat would pair these
    # bytes with the next save's date; stat by path stands in for that save.
    os.utime(tmp_zones_file, (1_000_000_000, 1_000_000_000))
    real_stat = os.stat

    def _stat(path, *args, **kwargs):
        if os.fspath(path) == str(tmp_zones_file):
            return os.stat_result((0,) * 8 + (2_000_000_000, 0))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", _stat)
    response = ingress_client.get("/zones.json")
    assert response.status_code == 200
    assert response.headers["last-modified"] == formatdate(1_000_000_000, usegmt=True)


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


def test_save_with_if_match_wildcard_succeeds(ingress_client, tmp_zones_file):
    # RFC 9110 13.1.1: "*" matches whenever the resource exists.
    r = ingress_client.post("/save_zones", json=_valid_payload(), headers={"If-Match": "*"})
    assert r.status_code == 200
    assert [
        f["properties"]["name"] for f in json.loads(tmp_zones_file.read_text())["features"]
    ] == ["Home"]


def test_save_with_if_match_star_inside_a_list_is_not_a_wildcard(ingress_client, tmp_zones_file):
    # "*" is only valid on its own; inside a quoted tag or a list it is not.
    for value in ('"a,*,b"', '"stale", *'):
        r = ingress_client.post("/save_zones", json=_valid_payload(), headers={"If-Match": value})
        assert r.status_code == 412, value


def test_save_with_if_match_list_containing_current_succeeds(ingress_client, tmp_zones_file):
    etag = ingress_client.get("/zones.json").headers["etag"]
    r = ingress_client.post(
        "/save_zones",
        json=_valid_payload(),
        headers={"If-Match": f'"stale-one", {etag}'},
    )
    assert r.status_code == 200


def test_save_with_if_match_list_without_current_returns_412(ingress_client, tmp_zones_file):
    original = tmp_zones_file.read_text()
    r = ingress_client.post(
        "/save_zones",
        json=_valid_payload(),
        headers={"If-Match": '"stale-one", "stale-two"'},
    )
    assert r.status_code == 412
    assert tmp_zones_file.read_text() == original


def test_save_with_weak_if_match_returns_412(ingress_client, tmp_zones_file):
    # If-Match uses strong comparison, so a weak validator never matches.
    etag = ingress_client.get("/zones.json").headers["etag"]
    r = ingress_client.post("/save_zones", json=_valid_payload(), headers={"If-Match": f"W/{etag}"})
    assert r.status_code == 412


def test_save_with_if_match_wildcard_and_missing_file_returns_412(ingress_client, tmp_zones_file):
    tmp_zones_file.unlink()
    r = ingress_client.post("/save_zones", json=_valid_payload(), headers={"If-Match": "*"})
    assert r.status_code == 412
    assert not tmp_zones_file.exists()


def test_concurrent_saves_with_same_if_match_let_only_one_through(app_factory, tmp_zones_file):
    import asyncio

    import httpx

    app = app_factory({})
    etag = TestClient(app, client=INGRESS).get("/zones.json").headers["etag"]

    async def _race():
        transport = httpx.ASGITransport(app=app, client=INGRESS)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            return await asyncio.gather(
                *(
                    c.post("/save_zones", json=_valid_payload(name), headers={"If-Match": etag})
                    for name in ("One", "Two")
                )
            )

    responses = asyncio.run(_race())
    assert sorted(r.status_code for r in responses) == [200, 412]


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
    # companion integration reads, NOT bare top-level.
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


def test_save_zones_backfills_a_missing_id(ingress_client, tmp_zones_file):
    """A feature with no id gets a fresh one, so automations can bind to it."""
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "First"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
                },
            },
        ],
    }
    r = ingress_client.post("/save_zones", json=payload)
    assert r.status_code == 200
    stored = json.loads(tmp_zones_file.read_text())
    assert stored["features"][0]["properties"]["id"]


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
        "features": [{"type": "Feature", "properties": {"name": "Zone"}, "geometry": geom}],
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
            "longitude must be a finite number",
        ),
        (
            _feature(_polygon([[["oops", 0], [1, 0], [1, 1], ["oops", 0]]])),
            "longitude must be a finite number",
        ),
        # A third value is altitude; anything else in a position is junk.
        (
            _feature(_polygon([[[0, 0, "x"], [1, 0], [1, 1], [0, 0, "x"]]])),
            "altitude must be a finite number",
        ),
        (
            _feature(_polygon([[[0, 0, True], [1, 0], [1, 1], [0, 0, True]]])),
            "altitude must be a finite number",
        ),
        (
            _feature(_polygon([[[0, 0, 1, 2, 3], [1, 0], [1, 1], [0, 0, 1, 2, 3]]])),
            "position must be a list of 2 or 3 numbers",
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
            "properties: required, and must be an object",
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


def test_save_zones_accepts_altitude_and_stores_two_values(ingress_client, tmp_zones_file):
    ring = [[0.0, 0.0, 12.5], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0, 12.5]]
    r = ingress_client.post("/save_zones", json=_feature(_polygon([ring])))
    assert r.status_code == 200
    stored = json.loads(tmp_zones_file.read_text())
    assert stored["features"][0]["geometry"]["coordinates"] == [[p[:2] for p in ring]]


def test_save_zones_drops_altitude_in_multipolygons(ingress_client, tmp_zones_file):
    ring = [[0.0, 0.0, 1], [1.0, 0.0, 1], [1.0, 1.0, 1], [0.0, 0.0, 1]]
    feature = {
        "type": "Feature",
        "properties": {"name": "m"},
        "geometry": {"type": "MultiPolygon", "coordinates": [[ring]]},
    }
    r = ingress_client.post(
        "/save_zones", json={"type": "FeatureCollection", "features": [feature]}
    )
    assert r.status_code == 200
    stored = json.loads(tmp_zones_file.read_text())
    assert stored["features"][0]["geometry"]["coordinates"] == [[[p[:2] for p in ring]]]


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
    assert "longitude must be a finite number" in r.json()["detail"]


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


def test_save_zones_refuses_a_zone_with_no_name(ingress_client, tmp_zones_file):
    """Without a name the integration refuses the whole file, so a save that
    would produce one is refused here instead."""
    geom = _polygon([[[0, 0], [1, 0], [1, 1], [0, 0]]])
    payload = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {}, "geometry": geom},
            {"type": "Feature", "properties": {"name": None}, "geometry": geom},
        ],
    }
    r = ingress_client.post("/save_zones", json=payload)
    assert r.status_code == 422
    assert "must not be blank" in r.json()["detail"]


def test_save_zones_accepts_multipolygon(ingress_client, tmp_zones_file):
    """MultiPolygon is an accepted geometry type alongside Polygon."""
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


def test_save_zones_refuses_null_properties(ingress_client, tmp_zones_file):
    """A zone needs a name, and a name lives in `properties`."""
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
    assert r.status_code == 422
    assert "must be an object" in r.json()["detail"]


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
    """Every authorisation failure on /zones.json,
    /trackers.json and /save_zones gets the same status, body and headers,
    whatever the reason (IP not allowed, token missing, token wrong) and
    whichever endpoint — so a client can't infer from the response alone
    whether a token is even configured. Covers all three endpoints, with a
    token set and with none."""
    import main

    expected_status = 403
    expected_body = {"error": "not authorised"}
    expected_cache_control = "no-store"

    def requests_for(client):
        yield "GET /zones.json", lambda **kw: client.get("/zones.json", **kw)
        yield "GET /trackers.json", lambda **kw: client.get("/trackers.json", **kw)
        yield "GET /trackers/here_since", lambda **kw: client.get(HERE_SINCE_URL, **kw)
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

    # Each phase is a fresh client; reset the per-client failure budget.
    main._auth_failures.clear()
    # Token configured, none presented.
    client = TestClient(app_factory({"save_token": "sekrit"}))
    for name, do in requests_for(client):
        r = do()
        assert r.status_code == expected_status, name
        assert r.json() == expected_body, name
        assert r.headers.get("cache-control") == expected_cache_control, name

    # Each phase is a fresh client; reset the per-client failure budget.
    main._auth_failures.clear()
    # Token configured, wrong one presented.
    client = TestClient(app_factory({"save_token": "sekrit"}))
    for name, do in requests_for(client):
        r = do(headers={"X-Save-Token": "wrong"})
        assert r.status_code == expected_status, name
        assert r.json() == expected_body, name
        assert r.headers.get("cache-control") == expected_cache_control, name


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
    # OSM, CARTO and Esri hosts must stay in img-src or those basemaps break
    # (#31). The Esri check compares split tokens with `==` rather than `in`:
    # CodeQL's py/incomplete-url-substring-sanitization fires on any URL literal
    # in a containment check, and `==` is exact and still clears it.
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
    this, a client hitting the limit would be locked out forever.
    """
    import main

    # Freeze time at t0, record 10 failures, advance past the window, then
    # a fresh attempt should succeed (old failures get popped).
    t = [1000.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: t[0])

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
# The overlay reaches HA through the Supervisor proxy, so these tests care most
# about what it refuses: fetching an entity that was not opted in, trusting an
# id from config, or handing positions to a client the zones would not go to.


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
        _ha_state("device_tracker.a", 51.50076543210987, -0.12467654321098, 2.34),
    )
    assert p == {
        "entity_id": "device_tracker.a",
        "name": "Thing",
        "state": "home",
        "latitude": 51.5008,
        "longitude": -0.1247,
        "gps_accuracy": 2.3,
        "battery_level": 42,
    }
    assert "secret_note" not in p


@pytest.mark.parametrize(
    "battery, expected",
    [
        (42, 42),
        (0, 0),
        (100, 100),
        (99.6, 100),
        (None, None),
        (-1, None),
        (101, None),
        (True, None),
        ("42", None),
        (float("nan"), None),
    ],
)
def test_overlay_point_battery(battery, expected):
    import main

    attrs = {"latitude": 51.5, "longitude": -0.12}
    if battery is not None:
        attrs["battery_level"] = battery
    p = main._overlay_point("device_tracker.a", {"attributes": attrs})
    assert p["battery_level"] == expected


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "not a dict",
        {},
        {"attributes": "not a dict"},
        {"attributes": {}},
        {"attributes": {"latitude": 1}},
        {"attributes": {"latitude": "51.5", "longitude": -0.12}},
        {"attributes": {"latitude": True, "longitude": -0.12}},
        {"attributes": {"latitude": float("nan"), "longitude": -0.12}},
        {"attributes": {"latitude": float("inf"), "longitude": -0.12}},
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
            "attributes": {"latitude": 51.5, "longitude": -0.12, "gps_accuracy": acc},
        },
    )
    assert p is not None and p["gps_accuracy"] is None


def test_overlay_point_falls_back_to_entity_id_for_name():
    import main

    p = main._overlay_point(
        "device_tracker.a",
        {
            "attributes": {"latitude": 51.5, "longitude": -0.12},
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
        main, "_fetch_state", lambda eid, _t: _ha_state(eid, 51.5007654, -0.1246765)
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
    same uniform rejection."""
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
        return _ha_state(entity_id, 51.5007654, -0.1246765, 5, name=entity_id)

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
    # Only the opted-in ids, once each, with the add-on's token. Fetches run
    # concurrently on a shared pool, so assert counts, not order — and not a
    # set, which would hide a duplicate-fetch regression.
    assert Counter(a[0] for a in asked) == Counter(["device_tracker.car", "sensor.not_a_tracker"])
    assert {a[1] for a in asked} == {"stub-token"}


def test_trackers_json_warns_once_about_an_unreadable_entity(app_factory, monkeypatch, caplog):
    import logging

    import main

    monkeypatch.setattr(main, "_overlay_unreadable", set())
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    # A 404: the entity does not exist, or the app may not read it. At DEBUG
    # this was invisible and looked like a tracker with no fix.
    monkeypatch.setattr(
        main.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("404"))
    )
    client = _ingress(app_factory({"overlay_entities": ["device_tracker.gone"]}))

    with caplog.at_level(logging.WARNING):
        assert client.get("/trackers.json").json()["unavailable"] == ["device_tracker.gone"]
        client.get("/trackers.json")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len([r for r in warnings if "device_tracker.gone" in r.getMessage()]) == 1


def test_trackers_json_warns_once_about_a_tracker_with_no_position(
    app_factory, monkeypatch, caplog
):
    """A zone-only tracker is the common fresh install: `device_tracker.phone` from a
    router knows which zone it is in, not where it is. It was dropped in silence, which
    looked exactly like a mistyped id, so say which entity and what to point at."""
    import logging

    import main

    monkeypatch.setattr(main, "_overlay_no_fix", set())
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    # Readable, and no latitude/longitude: the shape a router integration returns.
    monkeypatch.setattr(
        main,
        "_fetch_state",
        lambda eid, _t: {
            "entity_id": eid,
            "state": "home",
            "attributes": {"source_type": "router"},
        },
    )
    client = _ingress(app_factory({"overlay_entities": ["device_tracker.phone"]}))

    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            body = client.get("/trackers.json").json()
            assert body["trackers"] == [] and body["unavailable"] == []

    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len([m for m in warned if "device_tracker.phone" in m]) == 1
    assert any("latitude/longitude" in m for m in warned)


def test_a_tracker_that_gains_a_position_is_warned_about_again_if_it_loses_it(
    app_factory, monkeypatch, caplog
):
    import logging

    import main

    monkeypatch.setattr(main, "_overlay_no_fix", set())
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    fix = {"value": None}

    def fake_fetch(eid, _t):
        if fix["value"] is None:
            return {"entity_id": eid, "state": "home", "attributes": {}}
        return _ha_state(eid, *fix["value"])

    monkeypatch.setattr(main, "_fetch_state", fake_fetch)
    client = _ingress(app_factory({"overlay_entities": ["device_tracker.car"]}))

    with caplog.at_level(logging.WARNING):
        client.get("/trackers.json")
        fix["value"] = (51.5007654, -0.1246765)
        assert (
            client.get("/trackers.json").json()["trackers"][0]["entity_id"] == "device_tracker.car"
        )
        fix["value"] = None
        client.get("/trackers.json")

    warned = [m for m in (r.getMessage() for r in caplog.records) if "latitude/longitude" in m]
    assert len(warned) == 2


def test_trackers_json_skips_entities_that_fail_to_resolve(app_factory, monkeypatch):
    """One unreachable entity must not empty the whole overlay."""
    import main

    monkeypatch.setenv("SUPERVISOR_TOKEN", "stub-token")

    def fake_fetch(entity_id, token):
        if entity_id.endswith("broken"):
            return None
        return _ha_state(entity_id, 51.5007654, -0.1246765)

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
        return _ha_state(entity_id, 51.5007654, -0.1246765)

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
        return _ha_state(entity_id, 51.5007654, -0.1246765)

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
        return _ha_state(entity_id, 51.5007654, -0.1246765)

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
        return _ha_state(entity_id, 51.5007654, -0.1246765)

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
        def read(self, _limit=-1):
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
        outcome = json.dumps(_ha_state(f"device_tracker.{name}", 51.5, -0.12)).encode()
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


# --- Here since (/trackers/here_since) ----------------------------------------

HERE_SINCE_URL = "/trackers/here_since?entity_id=device_tracker.car"
HERE = (51.5, -0.12)
# 0.0001° of latitude is about 11 m.
NEAR = (51.50015, -0.12)  # ~17 m
FAR = (51.501, -0.12)  # ~111 m


def _window_start():
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) - timedelta(hours=24)


def _hist(*points, start=None):
    """An oldest-first history reply for one entity, an hour apart from `start`
    (default: the start of the 24 h window)."""
    from datetime import timedelta

    start = start or _window_start()
    return [
        [
            {
                "state": "not_home",
                "attributes": {"latitude": lat, "longitude": lon, "gps_accuracy": acc},
                "last_updated": (start + timedelta(hours=i)).isoformat(),
            }
            for i, (lat, lon, acc) in enumerate(points)
        ]
    ]


def _ts(reply, i):
    return reply[0][i]["last_updated"]


WS = _window_start()


def test_here_since_a_mover_arrived_at_the_first_state_of_the_run():
    import main

    reply = _hist((*FAR, 10), (*FAR, 10), (*HERE, 10), (*NEAR, 10), (*HERE, 10), start=WS)
    assert main._here_since(reply, WS) == (_ts(reply, 2), False)


def test_here_since_stationary_all_window_is_over_window():
    import main

    assert main._here_since(_hist((*HERE, 10), (*HERE, 10), start=WS), WS) == (None, True)


def test_here_since_history_that_begins_inside_the_window_shows_its_start():
    from datetime import timedelta

    import main

    reply = _hist((*HERE, 10), (*HERE, 10), start=WS + timedelta(hours=3))
    assert main._here_since(reply, WS) == (_ts(reply, 0), False)


def test_here_since_jitter_on_a_precise_fix_does_not_break_the_run():
    import main

    # 2 x 1 m would be 2 m; the 20 m floor keeps the ~17 m jitter in the run.
    reply = _hist((*FAR, 1), (*NEAR, 1), (*HERE, 1), start=WS)
    assert main._here_since(reply, WS) == (_ts(reply, 1), False)


def test_here_since_threshold_grows_with_the_anchor_accuracy():
    import main

    # 2 x 60 m = 120 m reaches the ~111 m point.
    assert main._here_since(_hist((*FAR, 60), (*HERE, 60), start=WS), WS) == (None, True)
    reply = _hist((*FAR, 50), (*HERE, 50), start=WS)
    assert main._here_since(reply, WS) == (_ts(reply, 1), False)


def test_a_reply_over_the_size_cap_is_refused(monkeypatch):
    import main

    monkeypatch.setattr(main, "MAX_HA_REPLY_BYTES", 10)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    # A long number: its truncated prefix still parses, so only the cap refuses it.
    monkeypatch.setattr(main.urllib.request, "urlopen", lambda *a, **k: _Body(b"1" * 20))
    assert main._get_json("states/x", "t") is None
    monkeypatch.setattr(main, "MAX_HA_REPLY_BYTES", 100)
    assert main._get_json("states/x", "t") == int("1" * 20)


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"latitude": float("nan"), "longitude": HERE[1]},
        {"latitude": True, "longitude": HERE[1]},
        {"latitude": HERE[0], "longitude": False},
        "not a dict",
    ],
)
def test_here_since_skips_a_state_without_valid_coordinates(bad):
    import main

    # A gap (the tracker unavailable, say) is not a move: the run carries on past it.
    reply = _hist((*FAR, 10), (*HERE, 10), (*HERE, 10), (*HERE, 10), start=WS)
    reply[0][2]["attributes"] = bad
    assert main._here_since(reply, WS) == (_ts(reply, 1), False)
    reply[0][2] = "not a state"
    assert main._here_since(reply, WS) == (_ts(reply, 1), False)


def test_here_since_compares_each_fix_with_the_newest_not_its_neighbour():
    import main

    # ~15 m steps: each is within 20 m of the next, but the first is ~30 m out.
    reply = _hist((51.50027, -0.12, 1), (51.500135, -0.12, 1), (*HERE, 1), start=WS)
    assert main._here_since(reply, WS) == (_ts(reply, 1), False)


def test_here_since_threshold_comes_from_the_newest_fix_accuracy():
    import main

    # The newest fix's 2 x 60 m reaches the ~111 m point; the older fix's 5 m would not.
    assert main._here_since(_hist((*FAR, 5), (*HERE, 60), start=WS), WS) == (None, True)


def test_here_since_window_start_allows_a_minute_of_slack():
    from datetime import timedelta

    import main

    reply = _hist((*HERE, 10), start=WS + timedelta(seconds=30))
    assert main._here_since(reply, WS) == (None, True)


def test_here_since_a_run_that_starts_after_the_oldest_state_is_not_over_window():
    from datetime import timedelta

    import main

    # The run begins at state 1, 30 s into the window, after a far state at its start.
    reply = _hist((*FAR, 10), (*HERE, 10), start=WS)
    reply[0][1]["last_updated"] = (WS + timedelta(seconds=30)).isoformat()
    assert main._here_since(reply, WS) == (_ts(reply, 1), False)


def test_here_since_reads_a_timestamp_without_a_zone_as_utc():
    import main

    reply = _hist((*HERE, 10), start=WS)
    reply[0][0]["last_updated"] = WS.replace(tzinfo=None).isoformat()
    assert main._here_since(reply, WS) == (None, True)


def test_here_since_anchors_on_the_newest_state_with_coordinates():
    import main

    reply = _hist((*FAR, 10), (*HERE, 10), (*HERE, 10), start=WS)
    reply[0][2]["attributes"] = {}
    assert main._here_since(reply, WS) == (_ts(reply, 1), False)


@pytest.mark.parametrize("reply", [None, [], [[]], {"a": 1}, ["x"], _hist((None, None, None))])
def test_here_since_empty_or_unusable_history_is_unknown(reply):
    import main

    assert main._here_since(reply, WS) == (None, False)


@pytest.mark.parametrize("stamp", [12345, "not a time"])
def test_here_since_an_unreadable_timestamp_is_unknown(stamp):
    import main

    reply = _hist((*HERE, 10), start=WS)
    reply[0][0]["last_updated"] = stamp
    assert main._here_since(reply, WS) == (None, False)


def _history_client(app_factory, monkeypatch, outcome, options=None):
    """An ingress client whose Supervisor raises `outcome`, or answers with it."""
    import main

    monkeypatch.setenv("SUPERVISOR_TOKEN", "stub-token")
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append((req.full_url, req.headers.get("Authorization"), timeout))
        if isinstance(outcome, BaseException):
            raise outcome
        return _Body(outcome if isinstance(outcome, bytes) else json.dumps(outcome).encode())

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)
    if options is None:
        options = {"overlay_entities": ["device_tracker.car"]}
    app = app_factory(options)
    return _ingress(app), calls


def test_here_since_endpoint_asks_for_one_entity_over_24_hours(app_factory, monkeypatch):
    from datetime import UTC, datetime, timedelta
    import urllib.parse

    import main

    reply = _hist((*FAR, 10), (*HERE, 10))
    client, calls = _history_client(app_factory, monkeypatch, reply)
    r = client.get(HERE_SINCE_URL)
    assert r.status_code == 200
    assert r.json() == {"since": _ts(reply, 1), "over_window": False, "reason": "ok"}
    assert r.headers["cache-control"] == "no-store"

    [(url, auth, timeout)] = calls
    assert auth == "Bearer stub-token" and timeout == main.OVERLAY_TIMEOUT_SECONDS
    prefix = f"{main.SUPERVISOR_API}/history/period/"
    assert url.startswith(prefix)
    path, query = url[len(prefix) :].split("?")
    # A stationary tracker only has unchanged rows, which Home Assistant omits
    # by default; without this the lookup always answers unknown.
    assert query == "filter_entity_id=device_tracker.car&significant_changes_only=0"
    start = datetime.fromisoformat(urllib.parse.unquote(path))
    expected = datetime.now(UTC) - timedelta(hours=24)
    assert abs((start - expected).total_seconds()) < 60


def test_here_since_endpoint_over_window(app_factory, monkeypatch):
    client, _ = _history_client(app_factory, monkeypatch, _hist((*HERE, 10), (*HERE, 10)))
    assert client.get(HERE_SINCE_URL).json() == {
        "since": None,
        "over_window": True,
        "reason": "over_window",
    }


@pytest.mark.parametrize("outcome", list(_FETCH_FAILURES.values()))
def test_here_since_endpoint_a_failed_read_says_so(app_factory, monkeypatch, outcome):
    client, _ = _history_client(app_factory, monkeypatch, outcome)
    r = client.get(HERE_SINCE_URL)
    assert r.status_code == 200
    # Not "no history": the read itself failed, which the popup now says.
    assert r.json() == {"since": None, "over_window": False, "reason": "lookup_failed"}


@pytest.mark.parametrize("outcome", [[], [[]]])
def test_here_since_endpoint_empty_history_says_no_history(app_factory, monkeypatch, outcome):
    client, _ = _history_client(app_factory, monkeypatch, outcome)
    r = client.get(HERE_SINCE_URL)
    assert r.status_code == 200
    assert r.json() == {"since": None, "over_window": False, "reason": "no_history"}


def test_here_since_answers_unknown_at_once_when_lookups_are_backed_up(app_factory, monkeypatch):
    import main

    max_ = main._HISTORY_MAX_PENDING
    ingress, calls = _history_client(app_factory, monkeypatch, _hist((*HERE, 10)))
    seen = []
    fake = main.urllib.request.urlopen
    monkeypatch.setattr(
        main.urllib.request,
        "urlopen",
        lambda *a, **k: (seen.append(main._history_pending), fake(*a, **k))[1],
    )
    lan = TestClient(
        app_factory({"save_token": "sekrit", "overlay_entities": ["device_tracker.car"]})
    )
    unknown = {"since": None, "over_window": False, "reason": "busy"}

    # At the cap a token holder gets unknown at once; ingress has one slot more.
    monkeypatch.setattr(main, "_history_pending", max_)
    assert lan.get(HERE_SINCE_URL, headers={"X-Save-Token": "sekrit"}).json() == unknown
    assert calls == []
    ingress.get(HERE_SINCE_URL)
    assert len(calls) == 1 and seen == [max_ + 1], "a running lookup counts as pending"
    assert main._history_pending == max_, "the count is released"
    monkeypatch.setattr(main, "_history_pending", max_ + 1)
    assert ingress.get(HERE_SINCE_URL).json() == unknown
    assert len(calls) == 1


@pytest.mark.parametrize(
    "query",
    [
        "?entity_id=device_tracker.other",
        "?entity_id=person.me",
        "?entity_id=device_tracker.car/../../states",
        "?entity_id=",
        "",
    ],
)
def test_here_since_never_fetches_an_entity_not_in_the_list(app_factory, monkeypatch, query):
    client, calls = _history_client(app_factory, monkeypatch, _hist((*HERE, 10)))
    r = client.get(f"/trackers/here_since{query}")
    assert r.status_code == 404
    assert r.headers["cache-control"] == "no-store"
    assert calls == []


def test_here_since_with_no_overlay_entities_is_404(app_factory, monkeypatch):
    client, calls = _history_client(app_factory, monkeypatch, _hist((*HERE, 10)), options={})
    assert client.get(HERE_SINCE_URL).status_code == 404
    assert calls == []


@pytest.mark.parametrize("headers", [{}, {"X-Save-Token": "wrong"}])
def test_here_since_without_the_token_is_refused_before_any_fetch(
    app_factory, monkeypatch, headers
):
    import main

    calls = []
    monkeypatch.setattr(main.urllib.request, "urlopen", lambda *a, **_k: calls.append(a))
    client = TestClient(
        app_factory({"save_token": "sekrit", "overlay_entities": ["device_tracker.car"]})
    )
    r = client.get(HERE_SINCE_URL, headers=headers)
    assert r.status_code == 403
    assert calls == []


def test_here_since_with_the_token_is_served(app_factory, monkeypatch):
    _, calls = _history_client(app_factory, monkeypatch, _hist((*HERE, 10)))
    client = TestClient(
        app_factory({"save_token": "sekrit", "overlay_entities": ["device_tracker.car"]})
    )
    r = client.get(HERE_SINCE_URL, headers={"X-Save-Token": "sekrit"})
    assert r.json() == {"since": None, "over_window": True, "reason": "over_window"}
    assert len(calls) == 1


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


@pytest.mark.parametrize(
    "path", ["/save_zones", "/zones.json", "/trackers.json", "/trackers/here_since", "/"]
)
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


def test_save_zones_refuses_a_blank_name(ingress_client, tmp_zones_file):
    """Whitespace is not a name; the integration refuses a file that has one."""
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "   "},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
                },
            }
        ],
    }
    r = ingress_client.post("/save_zones", json=payload)
    assert r.status_code == 422
    assert "must not be blank" in r.json()["detail"]


def test_save_zones_trims_a_padded_name(ingress_client, tmp_zones_file):
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "  Home  "},
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
    assert stored["features"][0]["properties"]["name"] == "Home"


def test_save_zones_refuses_a_non_integer_priority(ingress_client, tmp_zones_file):
    """`priority` decides the winner between overlapping zones; the integration
    refuses a file it cannot read it from."""
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "Home", "priority": "high"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
                },
            }
        ],
    }
    r = ingress_client.post("/save_zones", json=payload)
    assert r.status_code == 422
    assert "priority" in r.json()["detail"]


def test_save_zones_keeps_unknown_properties(ingress_client, tmp_zones_file):
    """Properties this editor does not model belong to whoever wrote them."""
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "Home", "priority": 3, "polygonal_zones_ext": {"x": 1}},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
                },
            }
        ],
    }
    r = ingress_client.post("/save_zones", json=payload)
    assert r.status_code == 200
    props = json.loads(tmp_zones_file.read_text())["features"][0]["properties"]
    assert props["priority"] == 3
    assert props["polygonal_zones_ext"] == {"x": 1}


def test_save_zones_reports_the_reload_outcome(ingress_client, tmp_zones_file, monkeypatch):
    """The save response carries what Home Assistant said, so the editor does
    not need a second status endpoint (or a poll)."""
    import main

    monkeypatch.setattr(main, "notify_saved", lambda: ("older", "update the integration"))
    payload = {
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
    body = ingress_client.post("/save_zones", json=payload).json()
    assert body["reload"] == {"state": "older", "note": "update the integration"}


def _named_ring(name: str, points: int = 4) -> dict:
    ring = [[round(i / 1000, 3), 0.0] for i in range(points - 1)]
    ring.append(ring[0])
    return {
        "type": "Feature",
        "properties": {"name": name},
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


def test_save_zones_rejects_more_features_than_the_cap(ingress_client, tmp_zones_file):
    from const import MAX_FEATURES_PER_COLLECTION

    features = [_named_ring(f"z{i}") for i in range(MAX_FEATURES_PER_COLLECTION + 1)]
    r = ingress_client.post("/save_zones", json={"type": "FeatureCollection", "features": features})
    assert r.status_code == 422
    assert "exceeds the cap" in r.json()["detail"]


def test_save_zones_rejects_more_vertices_in_total_than_the_cap(ingress_client, tmp_zones_file):
    """Each feature is under its own cap; the collection total is not."""
    from const import MAX_FEATURES_PER_COLLECTION, MAX_TOTAL_VERTICES_PER_COLLECTION

    features = [
        _named_ring(f"z{i}", points=1000)
        for i in range(MAX_TOTAL_VERTICES_PER_COLLECTION // 1000 + 1)
    ]
    assert len(features) <= MAX_FEATURES_PER_COLLECTION
    r = ingress_client.post("/save_zones", json={"type": "FeatureCollection", "features": features})
    assert r.status_code == 422
    assert "vertices in total" in r.json()["detail"]


@pytest.mark.parametrize(
    ("reply", "since", "over_window", "reason"),
    [
        (None, None, False, "lookup_failed"),
        ([], None, False, "no_history"),
        ([[]], None, False, "no_history"),
        (_hist((None, None, None)), None, False, "no_coordinates"),
        (_hist((*HERE, 10)), None, False, "unknown"),
        (_hist((*HERE, 10)), _ts(_hist((*HERE, 10)), 0), False, "ok"),
        (None, None, True, "over_window"),
    ],
)
def test_here_since_reason_names_which_unknown(reply, since, over_window, reason):
    import main

    assert main._here_since_reason(reply, since, over_window) == reason
