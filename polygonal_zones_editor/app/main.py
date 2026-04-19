import hashlib
import ipaddress
import json
import logging
import math
import os
import secrets
import time
from collections import defaultdict, deque
from email.utils import formatdate

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from helpers import (
    allow_all_ips,
    allow_request,
    allowed_ip,
    atomic_write_json,
    configure_logging,
    load_options,
    resolve_log_level,
)
from const import DATA_FOLDER, ZONES_FILE, MAX_SAVE_BYTES

_LOGGER = logging.getLogger(__name__)

# Paths that bypass the IP allowlist middleware. /healthz is exempt so the
# Docker HEALTHCHECK works. /save_zones runs its own authorisation logic
# (allowing token-bearing requests through even when allow_all_ips is off),
# so it bypasses the coarse middleware too. /zones.json mirrors /save_zones
# now that reads are also token-gated — a user setting save_token without
# allow_all_ips: true expects the token to unlock BOTH LAN reads and LAN
# writes, same as on /save_zones.
AUTHZ_EXEMPT_PATHS = frozenset({"/healthz", "/save_zones", "/zones.json"})

# Rate limit on /save_zones authorisation failures. Protects against brute-
# forcing save_token when the port is exposed on LAN. Only failures count;
# a client presenting a valid token or coming from ingress never increments
# the counter.
_SAVE_FAILURE_LIMIT = 10
_SAVE_FAILURE_WINDOW_SECONDS = 60
_save_failures: dict[str, deque] = defaultdict(deque)


def _rate_limit_exceeded(client_host: str) -> bool:
    """Check whether client_host has hit the failure budget in the window."""
    now = time.time()
    cutoff = now - _SAVE_FAILURE_WINDOW_SECONDS
    failures = _save_failures[client_host]
    while failures and failures[0] < cutoff:
        failures.popleft()
    return len(failures) >= _SAVE_FAILURE_LIMIT


def _record_save_failure(client_host: str) -> None:
    _save_failures[client_host].append(time.time())


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


def authorise_save(options: dict, request: Request) -> tuple[bool, str | None]:
    """Decide whether a /save_zones request is allowed.

    Returns ``(allowed, reason)``. ``reason`` is ``"invalid_token"`` when a
    token is configured but the request did not present a valid one (so the
    handler can return 401 instead of 403), or ``"not_allowed"`` for a
    plain block.

    Order:
      1. Ingress (172.30.32.2) is always allowed — the HA UI uses it.
      2. If ``save_token`` is configured, require a constant-time-equal
         ``X-Save-Token`` header. allow_all_ips no longer matters when a
         token is set; the token is the stronger signal.
      3. Otherwise fall back to allow_all_ips.
    """
    if allowed_ip(request):
        return True, None

    save_token = (options.get("save_token") or "").strip()
    if save_token:
        # Strip both stored and provided — symmetric handling so a trailing
        # whitespace in either doesn't silently break a correct token match.
        provided = request.headers.get("x-save-token", "").strip()
        if provided and secrets.compare_digest(provided.encode(), save_token.encode()):
            return True, None
        return False, "invalid_token"

    if allow_all_ips(options):
        return True, None

    return False, "not_allowed"


def authorise_read(options: dict, request: Request) -> tuple[bool, str | None]:
    """Decide whether a GET /zones.json request is allowed.

    Full mirror of ``authorise_save``: ingress → token → allow_all_ips →
    deny. Previously reads were gated only by the IPAllowMiddleware, which
    meant save_token didn't unlock LAN reads the way it unlocks LAN writes
    — a user who set save_token and left allow_all_ips off couldn't read
    zones.json on LAN even with the correct token. That asymmetry made
    the reading path harder to reason about than the write path.

    /zones.json is now in AUTHZ_EXEMPT_PATHS, so the middleware lets every
    request through and this function is the sole read gate.
    """
    if allowed_ip(request):
        return True, None

    save_token = (options.get("save_token") or "").strip()
    if save_token:
        provided = request.headers.get("x-save-token", "").strip()
        if provided and secrets.compare_digest(provided.encode(), save_token.encode()):
            return True, None
        return False, "invalid_token"

    if allow_all_ips(options):
        return True, None

    return False, "not_allowed"


class IPAllowMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, options: dict):
        super().__init__(app)
        self.options = options

    async def dispatch(self, request: Request, call_next):
        if request.url.path in AUTHZ_EXEMPT_PATHS:
            return await call_next(request)
        if not allow_request(self.options, request):
            _LOGGER.warning("Blocked request from %s on %s",
                            request.client.host, request.url.path)
            return PlainTextResponse("not allowed", status_code=403)
        return await call_next(request)


# Content-Security-Policy tailored to the addon's frontend.
#
# script-src/style-src allow unpkg for Leaflet + Leaflet-Draw (SRI-pinned
# in index.html) and 'unsafe-inline' for the one onclick handler on the
# Save button + Leaflet's injected inline styles.
#
# img-src covers OSM (default), CARTO (dark theme), and Esri World Imagery
# (satellite, #31) tile servers, unpkg for Leaflet-Draw's spritesheet SVG
# (dist/images/spritesheet.svg), and data:/blob: for Leaflet's inline-
# rendered tile markers.
#
# frame-ancestors permits HA's ingress origin and Nabu Casa remote access
# to iframe the addon UI; everything else is blocked (clickjacking defense).
_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://unpkg.com 'unsafe-inline'; "
    "style-src 'self' https://unpkg.com 'unsafe-inline'; "
    "img-src 'self' https://unpkg.com https://*.tile.openstreetmap.org "
    "https://*.basemaps.cartocdn.com https://server.arcgisonline.com "
    "data: blob:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'self' https://*.home-assistant.io https://*.ui.nabu.casa"
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


def _validate_position(pos, where: str) -> None:
    """Validate a single GeoJSON position `[lon, lat]` (altitude ignored).

    Raises ValueError with a descriptive, index-bearing message on failure.
    Rejects bool values (which would otherwise pass `isinstance(..., int)`
    because bool subclasses int in Python), NaN / inf, non-numeric types,
    and out-of-range WGS84 coordinates.
    """
    if not isinstance(pos, list) or len(pos) < 2:
        raise ValueError(f"{where}: position must be a list of at least 2 numbers")
    lon, lat = pos[0], pos[1]
    for name, value in (("longitude", lon), ("latitude", lat)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{where}: {name} must be a number")
        if not math.isfinite(value):
            raise ValueError(f"{where}: {name} must be finite")
    if not (_LON_MIN <= lon <= _LON_MAX):
        raise ValueError(f"{where}: longitude out of range [-180, 180]")
    if not (_LAT_MIN <= lat <= _LAT_MAX):
        raise ValueError(f"{where}: latitude out of range [-90, 90]")


def _validate_linear_ring(ring, where: str) -> int:
    """Validate a GeoJSON linear ring. Returns the vertex count on success."""
    if not isinstance(ring, list):
        raise ValueError(f"{where}: ring must be a list of positions")
    if len(ring) < _MIN_RING_POSITIONS:
        raise ValueError(
            f"{where}: ring must have at least {_MIN_RING_POSITIONS} positions"
        )
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
    features = obj.get("features")
    if not isinstance(features, list):
        raise ValueError("features must be a list")

    seen_names: set[str] = set()
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
            total_vertices = _validate_polygon_coordinates(
                coords, f"{where}.geometry.coordinates"
            )
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
            raise ValueError(
                f"{where}.geometry.type: must be Polygon or MultiPolygon"
            )
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


def save_zones_generator(options: dict):
    async def save_zones(request: Request):
        client_host = request.client.host or "unknown"
        if _rate_limit_exceeded(client_host):
            _LOGGER.warning(
                "Rate limit hit on /save_zones for %s (%d failures in %ds). "
                "Further attempts refused for the remainder of the window.",
                client_host, _SAVE_FAILURE_LIMIT, _SAVE_FAILURE_WINDOW_SECONDS,
            )
            return JSONResponse(
                {"error": "too many failed attempts"},
                status_code=429,
            )
        ok, reason = authorise_save(options, request)
        if not ok:
            _record_save_failure(client_host)
            if reason == "invalid_token":
                _LOGGER.warning(
                    "Rejected save from %s: missing or invalid X-Save-Token",
                    client_host,
                )
                return JSONResponse(
                    {"error": "missing or invalid X-Save-Token"},
                    status_code=401,
                )
            _LOGGER.warning(
                "Blocked request from %s on %s",
                client_host, request.url.path,
            )
            return PlainTextResponse("not allowed", status_code=403)

        content_length = request.headers.get("content-length")
        if content_length is None or not content_length.isdigit() or int(content_length) > MAX_SAVE_BYTES:
            return JSONResponse({"error": "payload too large or missing Content-Length"}, status_code=413)

        body = await request.body()
        if len(body) > MAX_SAVE_BYTES:
            return JSONResponse({"error": "payload too large"}, status_code=413)

        try:
            geo_json = json.loads(body)
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid json"}, status_code=400)

        try:
            _validate_feature_collection(geo_json)
        except ValueError as e:
            return JSONResponse(
                {"error": "invalid GeoJSON", "detail": str(e)},
                status_code=422,
            )

        # Optimistic concurrency: when the client provides If-Match, refuse to
        # overwrite if the on-disk file has changed since they read it. There
        # is a short TOCTOU window between this check and atomic_write_json;
        # adequate for a single-user addon, where contention is rare.
        if_match = request.headers.get("if-match", "").strip()
        if if_match:
            current = _current_zones_etag()
            if current != if_match:
                _LOGGER.info(
                    "Conflict on save from %s: client If-Match=%s, current=%s",
                    request.client.host, if_match, current,
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

        try:
            atomic_write_json(ZONES_FILE, geo_json)
        except OSError:
            _LOGGER.exception("Failed to write %s", ZONES_FILE)
            return JSONResponse({"error": "write failed"}, status_code=500)

        new_etag = _current_zones_etag()
        _LOGGER.info("Saved %d features to zones.json", len(geo_json["features"]))
        return JSONResponse(
            {"status": "ok", "etag": new_etag},
            headers={"ETag": new_etag} if new_etag else {},
        )

    return save_zones


_ALLOWED_THEMES = ("auto", "light", "dark")


def _normalised_theme(options: dict) -> str:
    raw = options.get("theme", "auto")
    return raw if raw in _ALLOWED_THEMES else "auto"


def config_json_generator(options: dict):
    async def config_json(_request: Request) -> JSONResponse:
        return JSONResponse({
            "zone_colour": options.get("zone_colour", "green"),
            "theme": _normalised_theme(options),
        })
    return config_json


def zones_json_generator(options: dict):
    async def zones_json(request: Request) -> Response:
        # When save_token is set, non-ingress reads require the same
        # X-Save-Token as writes. Shares the rate-limit bucket with
        # /save_zones so an attacker can't rotate between GET and POST
        # to double their guess budget.
        client_host = request.client.host or "unknown"
        if _rate_limit_exceeded(client_host):
            _LOGGER.warning(
                "Rate limit hit on /zones.json for %s (%d failures in %ds). "
                "Further attempts refused for the remainder of the window.",
                client_host, _SAVE_FAILURE_LIMIT, _SAVE_FAILURE_WINDOW_SECONDS,
            )
            return JSONResponse(
                {"error": "too many failed attempts"},
                status_code=429,
            )
        ok, reason = authorise_read(options, request)
        if not ok:
            _record_save_failure(client_host)
            if reason == "invalid_token":
                _LOGGER.warning(
                    "Rejected /zones.json read from %s: missing or invalid X-Save-Token",
                    client_host,
                )
                return JSONResponse(
                    {"error": "missing or invalid X-Save-Token"},
                    status_code=401,
                )
            _LOGGER.warning(
                "Blocked /zones.json read from %s (not ingress, no token configured, allow_all_ips off)",
                client_host,
            )
            return PlainTextResponse("not allowed", status_code=403)

        # Pass the file bytes through verbatim — atomic_write_json guarantees
        # the file is always valid JSON, so re-parsing and re-serialising via
        # JSONResponse would be a pointless round-trip.
        try:
            with open(ZONES_FILE, "rb") as f:
                body = f.read()
            mtime = os.stat(ZONES_FILE).st_mtime
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

        return Response(
            content=body,
            media_type="application/json",
            headers={
                **base_headers,
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    return zones_json


async def healthz(_request: Request) -> PlainTextResponse:
    # Verify the zones file is accessible — /healthz drives the Docker
    # HEALTHCHECK which drives Supervisor's container-restart signal. A
    # process-alive-only check would leave users in a broken-but-"running"
    # state when zones.json becomes unreadable.
    try:
        os.stat(ZONES_FILE)
    except OSError:
        return PlainTextResponse("zones file unreadable", status_code=503)
    return PlainTextResponse("ok")


def generate_app(options: dict) -> tuple[Starlette, dict]:
    static_folder = "static"
    routes = [
        Route("/save_zones", save_zones_generator(options), methods=["POST"]),
        Route("/zones.json", zones_json_generator(options), methods=["GET"]),
        Route("/config.json", config_json_generator(options), methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
        # html=True makes "/" return index.html. Explicit Routes above take
        # precedence on their exact paths.
        Mount("/", app=StaticFiles(directory=static_folder, html=True), name="static"),
    ]

    log_config = uvicorn.config.LOGGING_CONFIG
    log_config["version"] = 1
    log_config["disable_existing_loggers"] = False

    middleware = [
        Middleware(IPAllowMiddleware, options=options),
        Middleware(SecurityHeadersMiddleware),
    ]

    app = Starlette(debug=False, routes=routes, middleware=middleware)
    return app, log_config


# Values that, when handed to uvicorn's forwarded_allow_ips, would let any
# on-path attacker forge X-Forwarded-For: 172.30.32.2 and be treated as the
# HA ingress sidecar by allowed_ip(). We drop these with a logged error
# rather than failing startup so a typo can't lock a user out.
#
# Split into two categories so each branch can log a constant string with
# no interpolation — CodeQL's taint analysis otherwise flags %s-logging of
# any value derived from the options dict (which also carries save_token).
_TRUSTED_PROXIES_WILDCARDS = frozenset({"*", "0.0.0.0", "0.0.0.0/0", "::", "::/0"})
_INGRESS_IP_STR = "172.30.32.2"


_INGRESS_IP_OBJ = ipaddress.ip_address(_INGRESS_IP_STR)


def _parse_trusted_proxies(options: dict) -> list[str]:
    """Parse + validate the trusted_proxies option.

    Rejects, with a logged error, any entry that would let an on-path
    attacker forge X-Forwarded-For: 172.30.32.2 and be treated as the
    HA ingress sidecar by allowed_ip(). Each rejection logs a constant
    string (no value interpolation) so CodeQL's taint analysis doesn't
    flag the options-derived value.

    Rejection criteria (any one disqualifies):
      1. Literal wildcards (*, 0.0.0.0, 0.0.0.0/0, ::, ::/0).
      2. The ingress IP itself.
      3. Unparseable as an IP or CIDR (hostnames, garbage).
      4. A CIDR that contains the ingress IP (e.g. 172.30.0.0/16,
         0.0.0.0/8, 172.30.32.2/24) — closes the "supernet bypass"
         vector a naive literal match would miss.
    """
    raw = options.get("trusted_proxies", "") or ""
    entries = [p.strip() for p in raw.split(",") if p.strip()]
    safe = []
    for entry in entries:
        if entry in _TRUSTED_PROXIES_WILDCARDS:
            _LOGGER.error(
                "Refusing a wildcard trusted_proxies entry (one of "
                "*, 0.0.0.0, 0.0.0.0/0, ::, ::/0). It would let any "
                "client forge X-Forwarded-For and bypass the ingress-IP "
                "check on /save_zones. Dropping this entry; fix the "
                "option in the addon configuration."
            )
            continue
        if entry == _INGRESS_IP_STR:
            _LOGGER.error(
                "Refusing the HA ingress IP (172.30.32.2) as a "
                "trusted_proxies entry. It would let any client forge "
                "X-Forwarded-For and bypass the ingress-IP check on "
                "/save_zones. Dropping this entry; fix the option in "
                "the addon configuration."
            )
            continue
        try:
            net = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            _LOGGER.error(
                "Refusing an unparseable trusted_proxies entry "
                "(expected an IPv4/IPv6 address or CIDR). Dropping "
                "this entry; fix the option in the addon "
                "configuration."
            )
            continue
        # If the entry is an IPv4 network that contains the ingress IP,
        # it's a supernet bypass (e.g. 172.30.0.0/16 covers the ingress
        # IP 172.30.32.2). Reject. IPv6 networks can't contain an IPv4
        # address so no check is needed there.
        if isinstance(net, ipaddress.IPv4Network) and _INGRESS_IP_OBJ in net:
            _LOGGER.error(
                "Refusing a trusted_proxies entry whose CIDR covers "
                "the HA ingress IP. It would let any client forge "
                "X-Forwarded-For and bypass the ingress-IP check on "
                "/save_zones. Dropping this entry; fix the option in "
                "the addon configuration."
            )
            continue
        safe.append(entry)
    return safe


if __name__ == "__main__":  # pragma: no cover
    # The entrypoint block is covered by the build.yml smoke test (boots
    # the container under docker, probes /healthz + /zones.json), not by
    # pytest. Extracting to a function would give pytest a handle but
    # wouldn't exercise uvicorn.run(), which is the point of the block.
    # Bring logging up at the default level first so any errors in
    # load_options are visible, then re-apply the configured level.
    configure_logging()

    os.makedirs(DATA_FOLDER, exist_ok=True)
    if not os.path.exists(ZONES_FILE):
        with open(ZONES_FILE, "w") as f:
            json.dump({"type": "FeatureCollection", "features": []}, f)

    options = load_options()
    configure_logging(resolve_log_level(options.get("log_level")))
    redacted = {k: ("***" if k == "save_token" and v else v) for k, v in options.items()}
    _LOGGER.info("Loaded options: %s", redacted)
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
    if allow_all_ips(options):
        if (options.get("save_token") or "").strip():
            _LOGGER.info("allow_all_ips is on; /save_zones requires X-Save-Token from non-ingress clients.")
        else:
            _LOGGER.warning(
                "allow_all_ips is enabled and no save_token is set — /save_zones is reachable from any IP. "
                "Set the save_token option to require an X-Save-Token header on non-ingress requests."
            )

    proxy_ip_allowlist = _parse_trusted_proxies(options)
    if proxy_ip_allowlist:
        # Log the count only, not the list contents. `proxy_ip_allowlist` is
        # derived from the `trusted_proxies` option, and CodeQL's
        # py/clear-text-logging-sensitive-data rule flags any options-sourced
        # value logged via %s — same reason _parse_trusted_proxies itself
        # logs rejection reasons as constant strings. TESTING.md already
        # documents the count-based format as the expected operator-visible
        # output, so this aligns code with docs.
        _LOGGER.info(
            "Honouring X-Forwarded-For from %d configured proxy/proxies.",
            len(proxy_ip_allowlist),
        )

    app, log_config = generate_app(options)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_config=log_config,
        proxy_headers=bool(proxy_ip_allowlist),
        forwarded_allow_ips=",".join(proxy_ip_allowlist) if proxy_ip_allowlist else None,
    )
