import json
import logging
import os

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, HTMLResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from helpers import configure_logging, allow_request, allow_all_ips, load_options, atomic_write_json
from const import DATA_FOLDER, ZONES_FILE, MAX_SAVE_BYTES

_LOGGER = logging.getLogger(__name__)

# Paths that bypass the IP allowlist (health probes need to work without any
# client-IP constraints).
AUTHZ_EXEMPT_PATHS = frozenset({"/healthz"})


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


async def save_zones(request: Request) -> JSONResponse:
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

    try:
        atomic_write_json(ZONES_FILE, geo_json)
    except OSError:
        _LOGGER.exception("Failed to write %s", ZONES_FILE)
        return JSONResponse({"error": "write failed"}, status_code=500)

    _LOGGER.info("Saved %d features to zones.json", len(geo_json["features"]))
    return JSONResponse({"status": "ok"})


def index_html_generator(options: dict, static_folder: str):
    async def get_index(_request: Request) -> HTMLResponse:
        with open(os.path.join(static_folder, "index.html"), "r") as f:
            content = f.read()
        # json.dumps produces a safe JS string literal even if the option
        # contains quotes or control characters.
        safe_colour = json.dumps(options.get("zone_colour", "green"))
        content = content.replace("{{ ZONE_COLOUR }}", safe_colour)
        return HTMLResponse(content)
    return get_index


async def zones_json(_request: Request) -> JSONResponse:
    with open(ZONES_FILE, "r") as f:
        data = json.load(f)
    return JSONResponse(data, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })


async def healthz(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def generate_app(options: dict) -> tuple[Starlette, dict]:
    static_folder = "static"
    routes = [
        Route("/", index_html_generator(options, static_folder), methods=["GET"]),
        Route("/save_zones", save_zones, methods=["POST"]),
        Route("/zones.json", zones_json, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
        Mount("/", app=StaticFiles(directory=static_folder, html=False), name="static"),
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
    configure_logging()

    os.makedirs(DATA_FOLDER, exist_ok=True)
    if not os.path.exists(ZONES_FILE):
        with open(ZONES_FILE, "w") as f:
            json.dump({"type": "FeatureCollection", "features": []}, f)

    options = load_options()
    _LOGGER.info("Loaded options: %s", options)
    if allow_all_ips(options):
        _LOGGER.warning(
            "allow_all_ips is enabled — the addon is reachable without IP "
            "restriction. Only enable this if you understand the risk."
        )

    trusted = _parse_trusted_proxies(options)
    if trusted:
        _LOGGER.info("Trusting X-Forwarded-For from proxies: %s", trusted)

    app, log_config = generate_app(options)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_config=log_config,
        proxy_headers=bool(trusted),
        forwarded_allow_ips=",".join(trusted) if trusted else None,
    )
