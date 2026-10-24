"""Talk to Home Assistant: announce this add-on, and ask it to reload the zones.

Both calls use the add-on's own SUPERVISOR_TOKEN. Discovery needs no extra
permission (every add-on may post to ``/discovery``), and the reload goes
through the Core API proxy granted by ``homeassistant_api``.
"""

import json
import logging
import os
import socket
import urllib.error
import urllib.request

_LOGGER = logging.getLogger(__name__)

DISCOVERY_URL = "http://supervisor/discovery"
RELOAD_URL = "http://supervisor/core/api/services/polygonal_zones/reload_all_zones"
TIMEOUT_SECONDS = 5.0
# A save waits on this one, and the file is already written by then: a slow
# Supervisor must not make the editor feel hung.
NOTIFY_TIMEOUT_SECONDS = 2.0
PORT = 8000


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: the Supervisor token goes to the Supervisor only."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(newurl, code, "redirect refused", headers, fp)


def _post(url: str, payload: dict, token: str, timeout: float = TIMEOUT_SECONDS) -> int:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    with opener.open(request, timeout=timeout) as response:
        return response.status


def _supervisor_token() -> str:
    return os.environ.get("SUPERVISOR_TOKEN", "")


def announce(save_token: str) -> None:
    """Tell Home Assistant where this add-on's zones are, so it can offer setup."""
    token = _supervisor_token()
    if not token:
        _LOGGER.warning("No SUPERVISOR_TOKEN: cannot announce zones to Home Assistant")
        return
    config = {
        "host": socket.gethostname(),
        "port": PORT,
        "path": "/zones.json",
        "token": save_token,
    }
    try:
        _post(DISCOVERY_URL, {"service": "polygonal_zones", "config": config}, token)
        _LOGGER.info("Announced zones to Home Assistant")
    except (urllib.error.URLError, OSError, ValueError):
        _LOGGER.warning("Could not announce zones to Home Assistant", exc_info=True)


def notify_saved() -> tuple[str, str]:
    """Ask Home Assistant to re-read the zones after a save.

    Returns ``(state, note)`` for the editor's status line: ``ok``, ``older``
    (the integration predates the reload service), ``refused`` or
    ``unreachable``. Fixed text only — nothing from the response is echoed.
    """
    token = _supervisor_token()
    if not token:
        return "unreachable", "not connected to Home Assistant"
    try:
        _post(RELOAD_URL, {}, token, timeout=NOTIFY_TIMEOUT_SECONDS)
        return "ok", ""
    except urllib.error.HTTPError as err:
        # Home Assistant answers 400 for a service it does not know
        # (`APIDomainServicesView.post`), which is what an integration older
        # than 1.16.0 looks like. 404 is the same case on any other route.
        if err.code in (400, 404):
            return (
                "older",
                "Home Assistant needs integration 1.16.0 or later to load saves automatically",
            )
        if err.code == 429:
            return "refused", "Home Assistant is busy; it will pick up the next save"
        return "refused", "Home Assistant refused the reload"
    except (urllib.error.URLError, OSError) as err:
        _LOGGER.warning("Zone reload request failed: %s", err)
        return "unreachable", "could not reach Home Assistant"
