import hashlib
import ipaddress
import json
import logging
import os
import secrets
import time
from collections import defaultdict, deque

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
# so it bypasses the coarse middleware too.
AUTHZ_EXEMPT_PATHS = frozenset({"/healthz", "/save_zones"})

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
# img-src covers OSM (default) and CARTO (dark theme) tile servers, unpkg
# for Leaflet-Draw's spritesheet SVG (dist/images/spritesheet.svg), and
# data:/blob: for Leaflet's inline-rendered tile markers.
#
# frame-ancestors permits HA's ingress origin and Nabu Casa remote access
# to iframe the addon UI; everything else is blocked (clickjacking defense).
_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://unpkg.com 'unsafe-inline'; "
    "style-src 'self' https://unpkg.com 'unsafe-inline'; "
    "img-src 'self' https://unpkg.com https://*.tile.openstreetmap.org "
    "https://*.basemaps.cartocdn.com data: blob:; "
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


def _is_valid_feature_collection(obj) -> bool:
    if not isinstance(obj, dict) or obj.get("type") != "FeatureCollection":
        return False
    features = obj.get("features")
    if not isinstance(features, list):
        return False
    for f in features:
        if not isinstance(f, dict) or f.get("type") != "Feature":
            return False
        geom = f.get("geometry")
        if not isinstance(geom, dict):
            return False
        if geom.get("type") not in ("Polygon", "MultiPolygon"):
            return False
        if not isinstance(geom.get("coordinates"), list):
            return False
        props = f.get("properties")
        if props is not None and not isinstance(props, dict):
            return False
        if isinstance(props, dict):
            name = props.get("name")
            if name is not None and not isinstance(name, str):
                return False
    return True


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

        if not _is_valid_feature_collection(geo_json):
            return JSONResponse({"error": "not a GeoJSON FeatureCollection"}, status_code=422)

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


async def zones_json(_request: Request) -> Response:
    # Pass the file bytes through verbatim — atomic_write_json guarantees the
    # file is always valid JSON, so re-parsing and re-serialising via
    # JSONResponse would be a pointless round-trip.
    try:
        with open(ZONES_FILE, "rb") as f:
            body = f.read()
    except OSError:
        # File missing or unreadable (ownership drift after a Supervisor
        # remount, disk error, etc.). Return 503 with a log line rather than
        # letting Starlette emit a generic 500 traceback.
        _LOGGER.exception("Failed to read %s", ZONES_FILE)
        return JSONResponse(
            {"error": "zones file unreadable"},
            status_code=503,
        )
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "ETag": _etag_for_bytes(body),
        },
    )


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
        Route("/zones.json", zones_json, methods=["GET"]),
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
        _LOGGER.info(
            "Honouring X-Forwarded-For from these proxies: %s",
            proxy_ip_allowlist,
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
