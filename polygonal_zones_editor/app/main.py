import contextlib
import json
import os

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, FileResponse, PlainTextResponse, HTMLResponse
from starlette.routing import Route

from helpers import init_logging, allow_request, allow_all_ips, load_options, get_file_list, atomic_write_json
from const import DATA_FOLDER, ZONES_FILE, MAX_SAVE_BYTES

_LOGGER = init_logging()


def generate_static_file_routes(static_folder, prefix='/', options: dict = None) -> list[Route]:
    if options is None:
        options = {}

    def static_file_route(request: Request) -> FileResponse | PlainTextResponse:
        if not allow_request(options, request):
            _LOGGER.warning("Blocked request from %s on %s", request.client.host, request.url.path)
            return PlainTextResponse('not allowed', status_code=403)

        path = str(request.url.path)
        if path.endswith('/'):
            path += 'index.html'

        return FileResponse(static_folder + path)

    route_names = get_file_list(static_folder)
    for i, path in enumerate(route_names):
        path = path.replace(static_folder, '')
        path = path.replace('\\', '/')
        path = path.replace('//', '/')

        if path.endswith('index.html'):
            continue

        route_names[i] = path

    with contextlib.suppress(ValueError):
        route_names.remove('zones.json')

    return [Route(prefix + static_file, static_file_route, methods=['GET']) for static_file in route_names]


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
    async def save_zones(request: Request) -> PlainTextResponse | JSONResponse:
        """Saves the zones.json file."""
        if not allow_request(options, request):
            _LOGGER.warning("Blocked request from %s on %s", request.client.host, request.url.path)
            return PlainTextResponse('not allowed', status_code=403)

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
        return JSONResponse({'status': 'ok'})

    return save_zones


def index_html_generator(options: dict, static_folder):
    async def get_index(request: Request):
        if not allow_request(options, request):
            _LOGGER.warning("Blocked request from %s on %s", request.client.host, request.url.path)
            return PlainTextResponse('not allowed', status_code=403)

        path = static_folder + str(request.url.path)
        if path.endswith('/'):
            path += 'index.html'

        with open(path, 'r') as f:
            content = f.read()
            # json.dumps escapes quotes/backslashes/control chars, producing a
            # safe JS string literal even if the option is malformed.
            safe_colour = json.dumps(options.get('zone_colour', 'green'))
            content = content.replace('{{ ZONE_COLOUR }}', safe_colour)
            return HTMLResponse(content)

    return get_index


def zones_json_generator(options: dict):
    async def zones_json(request: Request) -> JSONResponse | PlainTextResponse:
        """Returns the zones.json file."""
        if not allow_request(options, request):
            _LOGGER.warning("Blocked request from %s on %s", request.client.host, request.url.path)
            return PlainTextResponse('not allowed', status_code=403)

        with open(ZONES_FILE, 'r') as f:
            data = json.load(f)
            return JSONResponse(data, headers={
                'Cache-Control': 'no-cache, no-store, must-revalidate',
                'Pragma': 'no-cache',
                'Expires': '0',
            })

    return zones_json


async def healthz(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def generate_app(options: dict) -> tuple[Starlette, dict]:
    """Returns the Starlette app."""
    routes = generate_static_file_routes('static/', options=options)
    routes.append(Route('/', index_html_generator(options, 'static/'), methods=['GET']))
    routes.append(Route('/save_zones', save_zones_generator(options), methods=['POST']))
    routes.append(Route('/zones.json', zones_json_generator(options), methods=['GET']))
    routes.append(Route('/healthz', healthz, methods=['GET']))

    log_config = uvicorn.config.LOGGING_CONFIG
    log_config['version'] = 1
    log_config['disable_existing_loggers'] = False

    # OpenStreetMap tile usage policy compliance: ensure a Referer is sent on
    # tile requests. 'no-referrer' and 'same-origin' are non-compliant.
    # https://operations.osmfoundation.org/policies/tiles/
    class ReferrerPolicyMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            response = await call_next(request)
            response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
            return response

    middleware = [Middleware(ReferrerPolicyMiddleware)]

    app = Starlette(debug=False, routes=routes, middleware=middleware)
    return app, log_config


if __name__ == '__main__':
    os.makedirs(DATA_FOLDER, exist_ok=True)
    if not os.path.exists(ZONES_FILE):
        with open(ZONES_FILE, 'w') as f:
            json.dump({"type": "FeatureCollection", "features": []}, f)

    options = load_options()
    _LOGGER.info("Loaded options: %s", options)
    if allow_all_ips(options):
        _LOGGER.warning(
            "allow_all_ips is enabled — the addon is reachable without IP "
            "restriction. Only enable this if you understand the risk."
        )

    app, log_config = generate_app(options)
    uvicorn.run(app, host='0.0.0.0', port=8000, log_config=log_config)
