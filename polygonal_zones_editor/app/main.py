import hashlib
import json
import logging
import os
import secrets

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
        provided = request.headers.get("x-save-token", "")
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


class ReferrerPolicyMiddleware(BaseHTTPMiddleware):
    # OpenStreetMap tile usage policy compliance.
    # https://operations.osmfoundation.org/policies/tiles/
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
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
        ok, reason = authorise_save(options, request)
        if not ok:
            if reason == "invalid_token":
                _LOGGER.warning(
                    "Rejected save from %s: missing or invalid X-Save-Token",
                    request.client.host,
                )
                return JSONResponse(
                    {"error": "missing or invalid X-Save-Token"},
                    status_code=401,
                )
            _LOGGER.warning(
                "Blocked request from %s on %s",
                request.client.host, request.url.path,
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
                return JSONResponse(
                    {
                        "error": "precondition failed",
                        "current_etag": current,
                    },
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
    with open(ZONES_FILE, "rb") as f:
        body = f.read()
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
        Middleware(ReferrerPolicyMiddleware),
    ]

    app = Starlette(debug=False, routes=routes, middleware=middleware)
    return app, log_config


def _parse_trusted_proxies(options: dict) -> list[str]:
    raw = options.get("trusted_proxies", "") or ""
    return [p.strip() for p in raw.split(",") if p.strip()]


if __name__ == "__main__":
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
