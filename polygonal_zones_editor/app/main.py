from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, wait
from email.utils import formatdate
import hashlib
import http.client
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import time
import urllib.request
import uuid

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
import uvicorn
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from const import (
    ALLOWED_IPS,
    DATA_FOLDER,
    DEFAULT_TRACKER_REFRESH_SECONDS,
    MAX_OVERLAY_ENTITIES,
    MAX_SAVE_BYTES,
    MAX_TRACKER_REFRESH_SECONDS,
    MIN_TRACKER_REFRESH_SECONDS,
    OVERLAY_COORD_DECIMALS,
    OVERLAY_FETCH_WORKERS,
    OVERLAY_TIMEOUT_SECONDS,
    SCHEMA_VERSION,
    SUPERVISOR_API,
    SUPERVISOR_TOKEN_ENV,
    TRANSPORT_PEER_SCOPE_KEY,
    ZONES_FILE,
)
from helpers import (
    allowed_ip,
    atomic_write_json,
    configure_logging,
    load_options,
    resolve_log_level,
)

_LOGGER = logging.getLogger(__name__)

# Paths the middleware leaves alone. /healthz stays open for the Docker
# HEALTHCHECK and the Supervisor watchdog; the other three call _gate
# themselves, so their cross-origin refusal comes before the token check.
AUTHZ_EXEMPT_PATHS = frozenset({"/healthz", "/save_zones", "/zones.json", "/trackers.json"})

# Rate limit on authorisation failures, shared by every gated path.
# Protects against brute-forcing save_token when the port is exposed on LAN.
# Only failures count. A request whose TCP peer is ingress is never limited,
# so failures recorded against a forwarded "172.30.32.2" cannot lock the HA
# UI out. Behind a trusted proxy the budget is per forwarded client.
_AUTH_FAILURE_LIMIT = 10
_AUTH_FAILURE_WINDOW_SECONDS = 60
# Above this many tracked clients, expired entries are swept, so forwarded
# addresses that are never seen again don't accumulate. At most one sweep a
# second: a flood of failures would otherwise walk every key on each one.
_AUTH_FAILURE_MAX_KEYS = 1024
_SWEEP_INTERVAL_SECONDS = 1.0
_auth_failures: dict[str, deque] = defaultdict(deque)
_last_sweep = 0.0


def _failure_key(request: Request) -> str:
    host = request.client.host if request.client and request.client.host else "unknown"
    # A forwarded "host" is whatever the header said; cap what we store.
    return host[:64]


def _prune(key: str, cutoff: float) -> int:
    failures = _auth_failures.get(key)
    if failures is None:
        return 0
    while failures and failures[0] < cutoff:
        failures.popleft()
    if not failures:
        del _auth_failures[key]
        return 0
    return len(failures)


def _rate_limit_exceeded(request: Request) -> bool:
    """Whether this request's client has used its failure budget."""
    if allowed_ip(request):
        return False
    cutoff = time.monotonic() - _AUTH_FAILURE_WINDOW_SECONDS
    return _prune(_failure_key(request), cutoff) >= _AUTH_FAILURE_LIMIT


def _record_auth_failure(request: Request) -> None:
    global _last_sweep
    now = time.monotonic()
    if (
        len(_auth_failures) > _AUTH_FAILURE_MAX_KEYS
        and now - _last_sweep >= _SWEEP_INTERVAL_SECONDS
    ):
        _last_sweep = now
        cutoff = now - _AUTH_FAILURE_WINDOW_SECONDS
        for key in list(_auth_failures):
            _prune(key, cutoff)
    _auth_failures[_failure_key(request)].append(now)


def _etag_for_bytes(body: bytes) -> str:
    """Strong ETag derived from the file contents.

    Quoted per RFC 7232. Returned identically by /zones.json (response
    header) and used by /save_zones (If-Match check).
    """
    return f'"{hashlib.sha256(body).hexdigest()}"'


def _current_zones_etag() -> str | None:
    """Compute the ETag for the on-disk zones file, or None if unreadable."""
    try:
        with open(ZONES_FILE, "rb") as f:
            return _etag_for_bytes(f.read())
    except OSError:
        return None


def _loggable(value: str) -> str:
    # For any client-supplied value: escaped, it cannot forge or hide a log line.
    return value.encode("unicode_escape").decode()


def authorise(options: dict, request: Request) -> str | None:
    """None if the request may proceed, otherwise why not, for the log only.

    Ingress is always allowed: the HA UI uses it. Anything else needs a
    configured save_token, presented as X-Save-Token.
    """
    if allowed_ip(request):
        return None
    save_token = (options.get("save_token") or "").strip()
    if not save_token:
        return "not ingress, and no save_token is configured"
    # Stripped on both sides, so stray whitespace can't fail a correct token.
    provided = request.headers.get("x-save-token", "").strip()
    if not provided:
        return "missing X-Save-Token"
    if not secrets.compare_digest(provided.encode(), save_token.encode()):
        return "wrong X-Save-Token"
    return None


# Every authorisation failure gets this one status, body and
# headers, whatever the reason, so a client cannot tell from the response
# alone whether a token is even configured. The precise reason is logged.
def _auth_rejection_response() -> JSONResponse:
    return JSONResponse(
        {"error": "not authorised"},
        status_code=403,
        headers={"Cache-Control": "no-store"},
    )


class TransportPeerMiddleware:
    """Outermost ASGI layer: records the TCP peer before anything rewrites
    scope["client"]. Proxy-header handling sits inside it, so allowed_ip()
    sees the real peer while rate limiting and logging see the resolved client.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "lifespan":
            client = scope.get("client")
            scope = {**scope, TRANSPORT_PEER_SCOPE_KEY: client[0] if client else None}
        await self.app(scope, receive, send)


class AuthoriseMiddleware(BaseHTTPMiddleware):
    """Gates the editor's static files and /config.json like the data routes.

    Ingress gets no cross-origin check: a link from another site to the
    editor is a legitimate page load.
    """

    def __init__(self, app, options: dict):
        super().__init__(app)
        self.options = options

    async def dispatch(self, request: Request, call_next):
        # The routing path, not request.url.path: uvicorn decodes %23 and %3F
        # into it, and a URL re-parse would read "/healthz%23/../x" as /healthz.
        path = request.scope["path"]
        if path not in AUTHZ_EXEMPT_PATHS:
            refusal = _gate(
                self.options, request, _loggable(path), check_origin=not allowed_ip(request)
            )
            if refusal is not None:
                return refusal
        return await call_next(request)


# Content-Security-Policy tailored to the addon's frontend.
#
# Leaflet and Leaflet-Geoman are vendored under app/static/vendor/, so
# script-src and style-src need no CDN. The editor has no inline script, so
# script-src needs no 'unsafe-inline'.
#
# style-src KEEPS 'unsafe-inline': Leaflet injects runtime inline styles
# for tile positioning (transform translate3d on every tile); without
# 'unsafe-inline' the map renders blank. The upstream issue for this is
# leaflet/Leaflet#4430; removing it requires a nonce per request which
# isn't feasible on static-served assets.
#
# img-src covers the three tile providers (OSM, CARTO dark, Esri World
# Imagery) plus data:/blob: for Leaflet's inline-rendered markers.
#
# frame-ancestors permits HA ingress (same-origin under HA's web frontend,
# covered by 'self') and Nabu Casa remote. Not *.home-assistant.io: that is
# HA's docs domain, not an embedding origin, and listing it would let any
# page hosted there frame the editor.
_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' https://*.tile.openstreetmap.org "
    "https://*.basemaps.cartocdn.com https://server.arcgisonline.com "
    "data: blob:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'self' https://*.ui.nabu.casa"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds standard security headers to every response.

    Referrer-Policy is required for OSM tile usage policy compliance
    (https://operations.osmfoundation.org/policies/tiles/). The rest are
    defense-in-depth: X-Content-Type-Options blocks MIME sniffing,
    X-Frame-Options + CSP frame-ancestors block clickjacking, the CSP
    baseline contains XSS to the already-loaded origins.
    """

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Content-Security-Policy"] = _CSP
        # Revalidate (a cheap 304) rather than reuse by guesswork, so a fixed
        # editor script reaches browsers on the next load after an update.
        # Routes that forbid caching set their own header, which wins.
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


# Per-feature vertex cap (mild algorithmic-DoS defense). Any legitimate
# home / work / school zone fits comfortably inside 1000 vertices; a
# malicious payload with e.g. 10 rings of 100 points inside a single
# MultiPolygon is rejected well before the 512KB body cap that the
# Supervisor has to load into memory.
_MAX_VERTICES_PER_FEATURE = 1000

# RFC 7946 §3.1.6: a linear ring must have ≥4 positions AND the first and
# last positions must be identical (i.e. the ring is closed).
_MIN_RING_POSITIONS = 4

# WGS84 bounds. GeoJSON stores [lon, lat].
_LON_MIN, _LON_MAX = -180.0, 180.0
_LAT_MIN, _LAT_MAX = -90.0, 90.0


def _finite_number(value) -> bool:
    # bool subclasses int, but True is not a coordinate. An int is always
    # finite, and math.isfinite raises OverflowError on a huge one.
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (isinstance(value, float) and math.isfinite(value))


def _validate_position(pos, where: str) -> None:
    """Validate a single GeoJSON position `[lon, lat]` (altitude ignored).

    Raises ValueError with a descriptive, index-bearing message on failure.
    """
    if not isinstance(pos, list) or len(pos) < 2:
        raise ValueError(f"{where}: position must be a list of at least 2 numbers")
    lon, lat = pos[0], pos[1]
    for name, value in (("longitude", lon), ("latitude", lat)):
        if not _finite_number(value):
            raise ValueError(f"{where}: {name} must be a finite number")
    if not (_LON_MIN <= lon <= _LON_MAX):
        raise ValueError(f"{where}: longitude out of range [-180, 180]")
    if not (_LAT_MIN <= lat <= _LAT_MAX):
        raise ValueError(f"{where}: latitude out of range [-90, 90]")


def _validate_linear_ring(ring, where: str) -> int:
    """Validate a GeoJSON linear ring. Returns the vertex count on success."""
    if not isinstance(ring, list):
        raise ValueError(f"{where}: ring must be a list of positions")
    if len(ring) < _MIN_RING_POSITIONS:
        raise ValueError(f"{where}: ring must have at least {_MIN_RING_POSITIONS} positions")
    for i, pos in enumerate(ring):
        _validate_position(pos, f"{where}[{i}]")
    if ring[0] != ring[-1]:
        raise ValueError(f"{where}: ring is not closed (first position must equal last)")
    return len(ring)


def _validate_polygon_coordinates(coords, where: str) -> int:
    """Validate a Polygon's `coordinates` (list of rings). Returns vertex total."""
    if not isinstance(coords, list):
        raise ValueError(f"{where}: Polygon coordinates must be a list of rings")
    if not coords:
        raise ValueError(f"{where}: Polygon must have at least one ring")
    total = 0
    for i, ring in enumerate(coords):
        total += _validate_linear_ring(ring, f"{where}[{i}]")
    return total


def _validate_feature_collection(obj) -> None:
    """Validate an incoming /save_zones payload. Raises ValueError on any
    structural, numeric, or uniqueness violation. Returns None on success.

    The error messages are index-bearing (``features[3].geometry.coordinates[0][2]``
    style) so a client that logs the response can pinpoint which zone is at
    fault without needing the server to echo coordinate values back
    (avoiding any PII bounce).

    Self-intersection and winding-order are not enforced — RFC 7946 mandates
    CCW exteriors but most consumers (shapely, Turf, HA's own zone engine)
    are tolerant. Adding those checks would require a geometry dependency
    (shapely) for marginal benefit at this scale.
    """
    if not isinstance(obj, dict) or obj.get("type") != "FeatureCollection":
        raise ValueError("expected a GeoJSON FeatureCollection")

    # schema_version is optional, so a restored unversioned file still saves.
    # It belongs at polygonal_zones.schema_version, where the integration reads
    # it; a bare top-level key is accepted because older files carry one. bool
    # is refused: `schema_version >= 1` must not pass on True.
    pz_in = obj.get("polygonal_zones")
    nested_version = pz_in.get("schema_version") if isinstance(pz_in, dict) else None
    schema_version = nested_version if nested_version is not None else obj.get("schema_version")
    if schema_version is not None and (
        isinstance(schema_version, bool) or not isinstance(schema_version, int)
    ):
        raise ValueError("schema_version: must be an int if present")

    features = obj.get("features")
    if not isinstance(features, list):
        raise ValueError("features must be a list")

    seen_names: set[str] = set()
    seen_ids: set[str] = set()
    for idx, f in enumerate(features):
        where = f"features[{idx}]"
        if not isinstance(f, dict) or f.get("type") != "Feature":
            raise ValueError(f"{where}: not a GeoJSON Feature")

        geom = f.get("geometry")
        if not isinstance(geom, dict):
            raise ValueError(f"{where}.geometry: missing or not a dict")
        gtype = geom.get("type")
        coords = geom.get("coordinates")
        if gtype == "Polygon":
            total_vertices = _validate_polygon_coordinates(coords, f"{where}.geometry.coordinates")
        elif gtype == "MultiPolygon":
            if not isinstance(coords, list):
                raise ValueError(
                    f"{where}.geometry.coordinates: MultiPolygon coordinates must be a list"
                )
            if not coords:
                raise ValueError(
                    f"{where}.geometry.coordinates: MultiPolygon must have at least one polygon"
                )
            total_vertices = 0
            for p_idx, polygon in enumerate(coords):
                total_vertices += _validate_polygon_coordinates(
                    polygon, f"{where}.geometry.coordinates[{p_idx}]"
                )
        else:
            raise ValueError(f"{where}.geometry.type: must be Polygon or MultiPolygon")
        if total_vertices > _MAX_VERTICES_PER_FEATURE:
            raise ValueError(
                f"{where}: vertex count {total_vertices} exceeds cap of {_MAX_VERTICES_PER_FEATURE}"
            )

        props = f.get("properties")
        if props is not None and not isinstance(props, dict):
            raise ValueError(f"{where}.properties: must be a dict or null")
        if isinstance(props, dict):
            name = props.get("name")
            if name is not None and not isinstance(name, str):
                raise ValueError(f"{where}.properties.name: must be a string if present")
            if isinstance(name, str):
                if name in seen_names:
                    raise ValueError(
                        f"{where}.properties.name: duplicate zone name (must be unique)"
                    )
                seen_names.add(name)

            # properties.id is the stable binding handle automations bind to
            # across renames. Optional on input (older clients, curl restores
            # of pre-versioned files); the server backfills one during
            # _normalise_feature_collection if missing. When present it must
            # be a non-empty string AND unique — duplicates would defeat the
            # "stable handle" contract since a consumer couldn't route a
            # matched id to a specific zone unambiguously.
            feature_id = props.get("id")
            if feature_id is not None:
                if not isinstance(feature_id, str) or not feature_id:
                    raise ValueError(
                        f"{where}.properties.id: must be a non-empty string if present"
                    )
                if feature_id in seen_ids:
                    raise ValueError(f"{where}.properties.id: duplicate zone id (must be unique)")
                seen_ids.add(feature_id)


def _normalise_feature_collection(obj: dict) -> dict:
    """Stamp `schema_version` and backfill missing `properties.id`.

    Runs only on a payload that passed validation. Guarantees that every
    persisted zones.json carries a current `schema_version` and that every
    feature has a stable `properties.id` the companion integration can bind
    automations to across renames. Mutates and returns ``obj``.
    """
    # The integration's version guard reads polygonal_zones.schema_version
    # (docs/ZONES_FORMAT.md) and nothing else.
    pz = obj.get("polygonal_zones")
    if not isinstance(pz, dict):
        pz = {}
        obj["polygonal_zones"] = pz
    pz["schema_version"] = SCHEMA_VERSION
    # Drop any legacy top-level key so there is a single source of truth.
    obj.pop("schema_version", None)
    for feature in obj.get("features", []):
        props = feature.get("properties")
        if not isinstance(props, dict):
            props = {}
            feature["properties"] = props
        if not props.get("id"):
            # uuid4 hex (no dashes) is compact and URL-safe for any future
            # id-bearing endpoint.
            props["id"] = uuid.uuid4().hex
    return obj


# Sec-Fetch-Site values a save may carry: the editor's own fetch, or a request
# the user made directly. Absent (curl, scripts) is also accepted.
_SAVE_FETCH_SITES = frozenset({"same-origin", "none"})


def _browser_cross_origin(request: Request) -> bool:
    """Whether a browser marked this request as not from the editor's origin."""
    site = request.headers.get("sec-fetch-site")
    return site is not None and site not in _SAVE_FETCH_SITES


def _gate(options: dict, request: Request, label: str, check_origin=True) -> Response | None:
    """The refusal for a request to a gated route, or None to serve it.

    ``label`` names the route in the log.
    """
    client_host = _loggable(request.client.host or "unknown")
    if _rate_limit_exceeded(request):
        _LOGGER.warning(
            "Rate limit hit on %s for %s (%d failures in %ds). "
            "Further attempts refused for the remainder of the window.",
            label,
            client_host,
            _AUTH_FAILURE_LIMIT,
            _AUTH_FAILURE_WINDOW_SECONDS,
        )
        # no-store: a cached 429 would outlive the window it reports.
        return JSONResponse(
            {"error": "too many failed attempts"},
            status_code=429,
            headers={"Cache-Control": "no-store"},
        )
    # Checked before the token, so a browser-sent cross-site request is
    # refused whatever it carries. Not counted as a failure: the client did
    # not choose to send it, and counting would let any web page lock the
    # user's own browser out.
    if check_origin and _browser_cross_origin(request):
        _LOGGER.warning("Rejected %s from %s: not a same-origin request", label, client_host)
        return _auth_rejection_response()
    reason = authorise(options, request)
    if reason is not None:
        _record_auth_failure(request)
        _LOGGER.warning("Rejected %s from %s: %s", label, client_host, reason)
        return _auth_rejection_response()
    return None


def save_zones_generator(options: dict):
    async def save_zones(request: Request):
        refusal = _gate(options, request, "/save_zones")
        if refusal is not None:
            return refusal

        # A cross-site HTML form can only send form or text/plain bodies, so
        # requiring JSON refuses those even from browsers without Sec-Fetch.
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            return JSONResponse({"error": "Content-Type must be application/json"}, status_code=415)

        content_length = request.headers.get("content-length")
        if (
            content_length is None
            or not content_length.isdigit()
            or int(content_length) > MAX_SAVE_BYTES
        ):
            return JSONResponse(
                {"error": "payload too large or missing Content-Length"}, status_code=413
            )

        body = await request.body()
        if len(body) > MAX_SAVE_BYTES:
            return JSONResponse({"error": "payload too large"}, status_code=413)

        try:
            geo_json = json.loads(body)
        # ValueError also covers an integer too long to convert, and
        # RecursionError a body nested deeper than the parser goes.
        except (ValueError, RecursionError):
            return JSONResponse({"error": "invalid json"}, status_code=400)

        try:
            _validate_feature_collection(geo_json)
        except ValueError as e:
            return JSONResponse(
                {"error": "invalid GeoJSON", "detail": str(e)},
                status_code=422,
            )

        # Optimistic concurrency: when the client provides If-Match, refuse to
        # overwrite if the on-disk file has changed since they read it. Nothing
        # awaits between this check and atomic_write_json, so on the one event
        # loop no other save can slip in between. Keep it that way: moving the
        # write to a thread would open that race.
        if_match = request.headers.get("if-match", "").strip()
        if if_match:
            current = _current_zones_etag()
            if current != if_match:
                _LOGGER.info(
                    "Conflict on save from %s: client If-Match=%s, current=%s",
                    _loggable(request.client.host or "unknown"),
                    _loggable(if_match),
                    current,
                )
                # Omit current_etag from the body when the file is
                # unreadable — a JSON `null` would force clients into
                # defensive null-checks for a field that is semantically
                # "resource missing". Clients that care fall back to GET
                # /zones.json and read the fresh ETag from the response.
                body = {"error": "precondition failed"}
                if current is not None:
                    body["current_etag"] = current
                return JSONResponse(
                    body,
                    status_code=412,
                    headers={"ETag": current} if current else {},
                )

        # Stamp schema_version + backfill ids AFTER the validator has
        # accepted the payload. Rejected payloads aren't mutated.
        _normalise_feature_collection(geo_json)
        try:
            written = atomic_write_json(ZONES_FILE, geo_json)
        except ValueError:
            return JSONResponse(
                {"error": "invalid GeoJSON", "detail": "NaN and Infinity are not valid JSON"},
                status_code=422,
            )
        except OSError:
            _LOGGER.exception("Failed to write %s", ZONES_FILE)
            return JSONResponse({"error": "write failed"}, status_code=500)

        new_etag = _etag_for_bytes(written)
        _LOGGER.info("Saved %d features to zones.json", len(geo_json["features"]))
        return JSONResponse({"status": "ok", "etag": new_etag}, headers={"ETag": new_etag})

    return save_zones


_ALLOWED_THEMES = ("auto", "light", "dark")


def _normalised_theme(options: dict) -> str:
    raw = options.get("theme", "auto")
    return raw if raw in _ALLOWED_THEMES else "auto"


def config_json_generator(options: dict):
    async def config_json(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "zone_colour": options.get("zone_colour", "purple"),
                "theme": _normalised_theme(options),
            }
        )

    return config_json


# --- Tracker overlay -------------------------------------------------------

# Entity ids are user-supplied config that ends up in a URL path. Accept only
# the shape Home Assistant itself guarantees — lowercase domain, dot, object id
# — so nothing can smuggle a path traversal or query string into the Supervisor
# call. Anything else is dropped and logged rather than sanitised: a silently
# rewritten entity id would be a confusing thing to debug.
_ENTITY_ID_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z0-9_]+$")


def overlay_entities(options: dict) -> list[str]:
    """The entity ids the user has opted in to plotting, validated and capped.

    Empty by default. Anyone holding save_token can read the overlay over the
    LAN, so it must never become a general state proxy: only ids named here
    are ever requested from Home Assistant or returned to a client.
    """
    raw = options.get("overlay_entities") or []
    if isinstance(raw, str):
        raw = [part for part in re.split(r"[,\n]", raw)]
    if not isinstance(raw, list):
        _LOGGER.warning(
            "overlay_entities is %s, expected a list or comma-separated string "
            "— ignoring it and plotting nothing.",
            type(raw).__name__,
        )
        return []
    out: list[str] = []
    for item in raw:
        candidate = str(item).strip()
        if not candidate:
            continue
        if not _ENTITY_ID_RE.match(candidate):
            _LOGGER.warning(
                "Ignoring overlay entity %r: not a valid entity id "
                "(expected e.g. device_tracker.my_phone).",
                candidate,
            )
            continue
        if candidate not in out:
            out.append(candidate)
    if len(out) > MAX_OVERLAY_ENTITIES:
        _LOGGER.warning(
            "overlay_entities lists %d entities; using the first %d.",
            len(out),
            MAX_OVERLAY_ENTITIES,
        )
        out = out[:MAX_OVERLAY_ENTITIES]
    return out


def tracker_refresh_seconds(options: dict) -> int:
    """Seconds between the editor's /trackers.json polls; 0 means load once."""
    raw = options.get("tracker_refresh_seconds", DEFAULT_TRACKER_REFRESH_SECONDS)
    if raw is None:
        return DEFAULT_TRACKER_REFRESH_SECONDS
    # bool is an int subclass; `true` here is a config mistake, not "1 second".
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        _LOGGER.warning(
            "tracker_refresh_seconds must be 0 or a whole number of seconds; using %d.",
            DEFAULT_TRACKER_REFRESH_SECONDS,
        )
        return DEFAULT_TRACKER_REFRESH_SECONDS
    if raw == 0:
        return 0
    clamped = min(max(raw, MIN_TRACKER_REFRESH_SECONDS), MAX_TRACKER_REFRESH_SECONDS)
    if clamped != raw:
        _LOGGER.warning(
            "tracker_refresh_seconds %d is outside %d-%d; using %d.",
            raw,
            MIN_TRACKER_REFRESH_SECONDS,
            MAX_TRACKER_REFRESH_SECONDS,
            clamped,
        )
    return clamped


# Shared by every /trackers.json request: a pool per request would multiply
# the worker cap by the number of open editors, worst exactly during an outage.
_OVERLAY_POOL = ThreadPoolExecutor(max_workers=OVERLAY_FETCH_WORKERS, thread_name_prefix="overlay")


def _fetch_state(entity_id: str, token: str) -> dict | None:
    """Fetch one entity's state through the Supervisor proxy. Blocking."""
    req = urllib.request.Request(
        f"{SUPERVISOR_API}/states/{entity_id}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=OVERLAY_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    # URLError and TimeoutError are OSErrors; HTTPException covers a garbled
    # or truncated reply.
    except (OSError, ValueError, http.client.HTTPException) as err:
        # One unreachable or malformed entity must not fail the whole overlay:
        # the map is still useful with the trackers that did resolve.
        _LOGGER.debug("Overlay fetch failed for %s: %s", entity_id, err)
        return None


def _overlay_point(entity_id: str, payload: dict | None) -> dict | None:
    """Reduce a full HA state object to the few fields the map needs.

    Everything else — every other attribute the entity carries — is discarded
    here rather than at the client, so it never crosses the wire.
    """
    if not isinstance(payload, dict):
        return None
    attrs = payload.get("attributes")
    if not isinstance(attrs, dict):
        return None
    lat, lon = attrs.get("latitude"), attrs.get("longitude")
    # Range-checked before float(), which overflows on a huge int.
    if not (
        _finite_number(lat) and _finite_number(lon) and -90 <= lat <= 90 and -180 <= lon <= 180
    ):
        return None
    acc = attrs.get("gps_accuracy")
    if not (_finite_number(acc) and 0 <= acc <= 1e7):
        acc = None
    battery = attrs.get("battery_level")
    if not (_finite_number(battery) and 0 <= battery <= 100):
        battery = None
    return {
        "entity_id": entity_id,
        "name": str(attrs.get("friendly_name") or entity_id),
        "state": str(payload.get("state") or "unknown"),
        "latitude": round(float(lat), OVERLAY_COORD_DECIMALS),
        "longitude": round(float(lon), OVERLAY_COORD_DECIMALS),
        "gps_accuracy": None if acc is None else round(float(acc), 1),
        "battery_level": None if battery is None else round(battery),
    }


def trackers_json_generator(options: dict):
    refresh_seconds = tracker_refresh_seconds(options)
    entities = overlay_entities(options)

    # The editor polls this, so a cached copy would freeze every marker at the
    # position of whichever response the browser happened to keep.
    def _uncached(body: dict, status_code: int = 200) -> JSONResponse:
        return JSONResponse(body, status_code=status_code, headers={"Cache-Control": "no-store"})

    async def trackers_json(request: Request) -> Response:
        # Same gate as /zones.json — this returns positions, so it must not
        # be reachable on terms the zones file isn't.
        refusal = _gate(options, request, "/trackers.json")
        if refusal is not None:
            return refusal

        if not entities:
            # Opted out (the default). Say so explicitly rather than returning
            # an empty list, so the editor can distinguish "not configured"
            # from "configured but nothing has a position right now".
            return _uncached({"configured": False, "trackers": []})

        token = os.environ.get(SUPERVISOR_TOKEN_ENV, "")
        if not token:
            _LOGGER.warning(
                "overlay_entities is set but %s is missing — is homeassistant_api "
                "enabled for this add-on? Plotting nothing.",
                SUPERVISOR_TOKEN_ENV,
            )
            return _uncached({"configured": True, "trackers": [], "error": "no_supervisor_token"})

        def _gather() -> tuple[list[dict], list[str]]:
            # One deadline for the whole overlay, with the fetches concurrent:
            # sequential per-entity timeouts would let 25 entities behind an
            # unreachable Supervisor hold this worker for 25 timeouts, and the
            # editor polls this. An entity Home Assistant could not be asked
            # about in time is listed, not dropped, so a polling client can tell
            # "unreachable" from "reported no position" and keep its marker.
            futures = {_OVERLAY_POOL.submit(_fetch_state, e, token): e for e in entities}
            done, pending = wait(futures, timeout=OVERLAY_TIMEOUT_SECONDS)
            for future in pending:
                future.cancel()  # still queued: do not spend the pool on a poll that has given up
            found, unavailable = [], []
            for future, entity_id in futures.items():
                payload = future.result() if future in done else None
                if payload is None:
                    unavailable.append(entity_id)
                    continue
                point = _overlay_point(entity_id, payload)
                if point is not None:
                    found.append(point)
            return found, unavailable

        trackers, unavailable = await run_in_threadpool(_gather)
        return _uncached(
            {
                "configured": True,
                "trackers": trackers,
                "unavailable": unavailable,
                "refresh_seconds": refresh_seconds,
            }
        )

    return trackers_json


def zones_json_generator(options: dict):
    async def zones_json(request: Request) -> Response:
        # Shares the failure bucket with /save_zones, so an attacker can't
        # rotate between GET and POST to double their guess budget.
        refusal = _gate(options, request, "/zones.json")
        if refusal is not None:
            return refusal

        # Pass the file bytes through verbatim — atomic_write_json guarantees
        # the file is always valid JSON, so re-parsing and re-serialising via
        # JSONResponse would be a pointless round-trip.
        def _read_zones() -> tuple[bytes, float]:
            with open(ZONES_FILE, "rb") as f:
                return f.read(), os.stat(ZONES_FILE).st_mtime

        try:
            body, mtime = await run_in_threadpool(_read_zones)
        except OSError:
            # File missing or unreadable (ownership drift after a Supervisor
            # remount, disk error, etc.). Return 503 with a log line rather
            # than letting Starlette emit a generic 500 traceback.
            _LOGGER.exception("Failed to read %s", ZONES_FILE)
            return JSONResponse(
                {"error": "zones file unreadable"},
                status_code=503,
            )
        etag = _etag_for_bytes(body)
        last_modified = formatdate(mtime, usegmt=True)
        base_headers = {
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "ETag": etag,
            "Last-Modified": last_modified,
        }

        # RFC 7232 conditional GET. If the client presents an If-None-Match
        # that matches our current ETag (or is the wildcard "*"), return 304
        # with no body so the integration's polling loop doesn't re-parse
        # the full FeatureCollection on every tick. Parse comma-separated
        # lists too — the spec allows multiple candidate validators.
        inm = request.headers.get("if-none-match", "").strip()
        if inm:
            candidates = {e.strip() for e in inm.split(",")}
            if etag in candidates or "*" in candidates:
                return Response(status_code=304, headers=base_headers)

        return Response(content=body, media_type="application/json", headers=base_headers)

    return zones_json


async def healthz(_request: Request) -> PlainTextResponse:
    # Verify the zones file is accessible — /healthz drives the Docker
    # HEALTHCHECK, so an unreadable zones.json marks the container unhealthy
    # rather than leaving it broken but reported as running.
    # Readability for this process, not mere existence: a file left owned by
    # another user must count as broken too.
    if not os.access(ZONES_FILE, os.R_OK):
        return PlainTextResponse("zones file unreadable", status_code=503)
    return PlainTextResponse("ok")


def generate_app(options: dict) -> TransportPeerMiddleware:
    """Build the ASGI app exactly as it is served.

    Proxy headers are applied here rather than by uvicorn, inside the layer
    that records the TCP peer, so serve it with ``proxy_headers=False``.
    """
    static_folder = "static"
    routes = [
        Route("/save_zones", save_zones_generator(options), methods=["POST"]),
        Route("/zones.json", zones_json_generator(options), methods=["GET"]),
        Route("/config.json", config_json_generator(options), methods=["GET"]),
        Route("/trackers.json", trackers_json_generator(options), methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
        # html=True makes "/" return index.html. Explicit Routes above take
        # precedence on their exact paths.
        Mount("/", app=StaticFiles(directory=static_folder, html=True), name="static"),
    ]

    middleware = [
        # Outside the gate, so its refusals carry the headers too.
        Middleware(SecurityHeadersMiddleware),
        Middleware(AuthoriseMiddleware, options=options),
        # Innermost: compress text responses (the unminified vendored Leaflet
        # JS/CSS are the bulk of page weight). Auth/security middleware stay
        # outermost so they still gate before any work.
        Middleware(GZipMiddleware, minimum_size=1024),
    ]

    app = Starlette(debug=False, routes=routes, middleware=middleware)

    proxy_ip_allowlist = _parse_trusted_proxies(options)
    if proxy_ip_allowlist:
        # The count only, for the reason _parse_trusted_proxies gives.
        _LOGGER.info(
            "Honouring X-Forwarded-For from %d configured proxy/proxies.",
            len(proxy_ip_allowlist),
        )
        app = ProxyHeadersMiddleware(app, trusted_hosts=proxy_ip_allowlist)
    return TransportPeerMiddleware(app)


# Ingress is decided by the TCP peer (TransportPeerMiddleware), never by a
# forwarded header, so no trusted_proxies entry can make a request ingress.
# What a trusted proxy decides is which client a request is logged and
# rate-limited as. These entries are dropped, with a logged error rather than
# a failed startup so a typo can't lock a user out:
#   - wildcards, which would let every client name its own address and so
#     escape the per-client failure limit;
#   - the ingress IP or any range covering it: ingress is HA's own proxy and
#     needs no listing, and a range that wide trusts every other host on the
#     Supervisor network to name any client it likes.
# Every message is a constant string: CodeQL flags %s-logging of any value
# derived from the options dict, which also carries save_token.
_INGRESS_IPS = tuple(ipaddress.ip_address(ip) for ip in ALLOWED_IPS)


def _parse_trusted_proxies(options: dict) -> list[str]:
    """Parse and validate the trusted_proxies option; return what uvicorn gets."""
    raw = options.get("trusted_proxies", "") or ""
    entries = [p.strip() for p in raw.split(",") if p.strip()]
    safe = []
    for entry in entries:
        try:
            net = ipaddress.ip_network(entry, strict=False)
            iface_ip = ipaddress.ip_interface(entry).ip
        except ValueError:
            net = iface_ip = None
        # Every spelling of "all addresses": a /0 network (::/0, 0::/0,
        # 0.0.0.0/0, ...) or the bare unspecified address (0.0.0.0, ::).
        if entry == "*" or (
            net is not None
            and (
                net.prefixlen == 0
                or (net.num_addresses == 1 and net.network_address.is_unspecified)
            )
        ):
            _LOGGER.error(
                "Refusing a trusted_proxies entry that means every address "
                "(such as *, 0.0.0.0/0 or ::/0). It would let any client "
                "choose the address it is logged and rate-limited as. "
                "Dropping this entry; fix the option in the addon configuration."
            )
            continue
        if net is None:
            _LOGGER.error(
                "Refusing an unparseable trusted_proxies entry "
                "(expected an IPv4/IPv6 address or CIDR). Dropping "
                "this entry; fix the option in the addon "
                "configuration."
            )
            continue
        # A bare address parses as a /32, so this also catches the ingress
        # IP listed on its own.
        if any(ip in net for ip in _INGRESS_IPS):
            _LOGGER.error(
                "Refusing a trusted_proxies entry whose CIDR covers the HA "
                "ingress IP (172.30.32.2). Ingress is recognised by its own "
                "connection and needs no listing, and a range this wide "
                "trusts other hosts on the Supervisor network. Dropping this "
                "entry; fix the option in the addon configuration."
            )
            continue
        if net.prefixlen < net.max_prefixlen:
            _LOGGER.warning(
                "A trusted_proxies entry is a range: every address in the range "
                "is believed about which client it forwards for. List each "
                "proxy's own IP instead."
            )
        # uvicorn parses CIDRs strictly, so a host-bit entry such as
        # 192.168.1.5/24 would silently trust nothing; hand it the network,
        # and say so.
        if "/" in entry and iface_ip != net.network_address:
            _LOGGER.warning(
                "A trusted_proxies entry has host bits set and is applied as "
                "its whole network. List each proxy's own IP instead."
            )
        safe.append(str(net) if "/" in entry else entry)
    return safe


# Options logged verbatim at startup. An allowlist, so an option added later
# (or an unexpected key in options.json) is never logged by default.
_LOGGED_OPTION_KEYS = (
    "allow_all_ips",
    "log_level",
    "theme",
    "tracker_refresh_seconds",
    "zone_colour",
)


def _entry_count(value) -> int:
    if isinstance(value, str):
        value = re.split(r"[,\n]", value)
    if not isinstance(value, list):
        return 0
    return sum(1 for item in value if str(item).strip())


def loggable_options(options: dict) -> dict:
    """The options as they may appear in the add-on log.

    The token is reduced to whether it is set; entity ids and proxy addresses
    identify devices and hosts, so only their counts are logged.
    """
    out = {key: options[key] for key in _LOGGED_OPTION_KEYS if key in options}
    # "***" is the form DOCS.md and TESTING.md tell users to look for.
    out["save_token"] = "***" if (options.get("save_token") or "").strip() else "not set"
    out["overlay_entities"] = _entry_count(options.get("overlay_entities"))
    out["trusted_proxies"] = _entry_count(options.get("trusted_proxies"))
    return out


def log_retired_allow_all_ips(options: dict) -> None:
    """allow_all_ips is kept in the schema so old configs validate; say it does nothing."""
    if options.get("allow_all_ips"):
        _LOGGER.error(
            "allow_all_ips no longer grants LAN access. Set save_token and send it as the "
            "X-Save-Token header; use the editor from the Home Assistant sidebar."
        )


if __name__ == "__main__":  # pragma: no cover
    # Exercised by the boot tests in test_request_trust.py (through runpy,
    # with uvicorn.run captured) and by build.yml's container smoke.
    # Bring logging up at the default level first so any errors in
    # load_options are visible, then re-apply the configured level.
    configure_logging()

    os.makedirs(DATA_FOLDER, exist_ok=True)
    if not os.path.exists(ZONES_FILE):
        atomic_write_json(ZONES_FILE, {"type": "FeatureCollection", "features": []})

    options = load_options()
    configure_logging(resolve_log_level(options.get("log_level")))
    _LOGGER.info("Loaded options: %s", loggable_options(options))
    if os.getuid() == 0:
        # The s6 init in rootfs/etc/services.d/web/run is supposed to drop
        # privileges to the 'app' user. If we got here as root, one of its
        # fallbacks fired (missing s6-setuidgid, missing app user, or
        # options.json unreadable). Log prominently on every boot so a
        # fallback-running install can't go unnoticed — the AppArmor profile
        # is less effective when the workload runs as uid 0.
        _LOGGER.warning(
            "Running as uid 0 (root). The s6 service script fell back from "
            "dropping privileges to the 'app' user — check the service log "
            "for a warning explaining which condition fired."
        )
    log_retired_allow_all_ips(options)

    # generate_app applies trusted_proxies itself, inside the layer that
    # records the TCP peer; uvicorn's own proxy handling (on by default)
    # would rewrite the client before that record is taken.
    # No access log: it records every client's IP and request line at INFO in
    # an add-on log any HA user can read and nothing expires.
    # Port 8000 is what config.yaml's ingress_port and the HEALTHCHECK expect.
    uvicorn.run(
        generate_app(options), host="0.0.0.0", port=8000, access_log=False, proxy_headers=False
    )
