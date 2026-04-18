import contextlib
import json
import logging
import os
import sys
import tempfile

from starlette.requests import Request

from const import ALLOWED_IPS, OPTIONS_FILE

_LOGGER = logging.getLogger(__name__)


_VALID_LOG_LEVELS = ("debug", "info", "warning", "error", "critical")


def resolve_log_level(value) -> int:
    """Map a string log level (case-insensitive) to a ``logging`` constant.

    Unknown or non-string values fall back to ``logging.INFO`` rather than
    raising, so a typo'd ``log_level`` option doesn't crash the addon.
    """
    if isinstance(value, str) and value.lower() in _VALID_LOG_LEVELS:
        return getattr(logging, value.upper())
    return logging.INFO


def configure_logging(level: int = logging.INFO) -> None:
    """Configure the root logger once.

    Call exactly once from ``__main__``. Uses ``logging.basicConfig`` so it
    no-ops if handlers are already attached (e.g. by test frameworks).
    """
    logging.basicConfig(
        level=level,
        format="[%(levelname)s: %(asctime)s]: %(message)s",
        stream=sys.stdout,
    )
    # basicConfig is a no-op when handlers already exist, so set the level
    # explicitly to honour later calls (e.g. when options come in after a
    # default setup).
    logging.getLogger().setLevel(level)


def allow_all_ips(options: dict) -> bool:
    """Check if the --allow-all-ips flag is passed or enabled in the options."""
    return '--allow-all-ips' in sys.argv or '-a' in sys.argv or options.get('allow_all_ips', False)


def allowed_ip(request: Request) -> bool:
    """Check if the request's client IP is allowed to access the web interface."""
    if not request.client.host:
        return False
    return request.client.host in ALLOWED_IPS


def allow_request(options: dict, request: Request) -> bool:
    """Check if the request is allowed to access the web interface."""
    return allow_all_ips(options) or allowed_ip(request)


def atomic_write_json(path: str, data) -> None:
    """Serialise ``data`` to ``path`` atomically.

    Writes to a temporary file in the same directory, flushes and fsyncs it,
    then renames it over the destination. Guarantees that a concurrent reader
    (or a reader after a crash/power loss) never observes a partial or
    truncated file: either the previous contents are visible, or the new
    contents are visible — never an intermediate state.
    """
    directory = os.path.dirname(path) or '.'
    fd, tmp_path = tempfile.mkstemp(prefix='.', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def load_options() -> dict:
    """Load the addon options from OPTIONS_FILE.

    Returns an empty dict when the file is missing. Invalid JSON or I/O
    errors are logged and fall back to an empty dict so the addon can still
    start with defaults — a corrupt options.json must not be a boot-loop.
    """
    if not os.path.exists(OPTIONS_FILE):
        return {}
    try:
        with open(OPTIONS_FILE, 'r') as f:
            loaded = json.load(f)
    except (OSError, json.JSONDecodeError):
        _LOGGER.exception("Failed to read %s; starting with default options.", OPTIONS_FILE)
        return {}
    if not isinstance(loaded, dict):
        _LOGGER.warning("%s did not contain a JSON object; starting with default options.", OPTIONS_FILE)
        return {}
    return loaded
