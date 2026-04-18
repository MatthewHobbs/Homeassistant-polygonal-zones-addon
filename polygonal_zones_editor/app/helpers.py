import contextlib
import json
import logging
import os
import sys
import tempfile

from starlette.requests import Request

from const import ALLOWED_IPS, OPTIONS_FILE


def init_logging() -> logging.Logger:
    """Cretae a logger that formats the log messages.

    Returns:
        logging.Logger: A logger that formats the log messages.
    """
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('[%(levelname)s: %(asctime)s]: %(message)s')
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


def allow_all_ips(options: dict) -> bool:
    """Check if the --allow-all-ips flag is passed or enabled in the options.

    Args:
        options (dict): A dictionary of options.

    Returns:
        bool: True if the --allow-all-ips flag is passed or enabled in the options, False otherwise.
    """
    return '--allow-all-ips' in sys.argv or '-a' in sys.argv or options.get('allow_all_ips', False)


def allowed_ip(request: Request) -> bool:
    """Check if the request's client IP is allowed to access the web interface.

    Args:
        request (Request): A request object.

    Returns:
        bool: True if the request's client IP is allowed to access the web interface, False otherwise.
    """
    if not request.client.host:
        return False

    return request.client.host in ALLOWED_IPS


def allow_request(options: dict, request: Request) -> bool:
    """Check if the request is allowed to access the web interface.

    Args:
        options (dict): A dictionary of options.
        request (Request): A request object.

    Returns:
        bool: True if the request is allowed to access the web interface, False otherwise.
    """
    return allow_all_ips(options) or allowed_ip(request)


def get_file_list(path: str) -> list[str]:
    """Get a list of files in a given path.

    Args:
        path (str): The path to get the files from.

    Returns:
        list[str]: A list of files in the given path.
    """
    files = []
    for root, dirs, filenames in os.walk(path):
        for filename in filenames:
            files.append(os.path.join(root, filename))
    return files


def atomic_write_json(path: str, data) -> None:
    """Serialise ``data`` to ``path`` atomically.

    Writes to a temporary file in the same directory, flushes and fsyncs it,
    then renames it over the destination. Guarantees that a concurrent reader
    (or a reader after a crash/power loss) never observes a partial or
    truncated file: either the previous contents are visible, or the new
    contents are visible — never an intermediate state.

    ``os.replace`` is atomic on POSIX when source and destination are on the
    same filesystem, which is why the temp file is created in the same
    directory as the destination rather than ``/tmp``.

    Args:
        path (str): Destination file path.
        data: Any JSON-serialisable object.
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
    """Load the options from the options file.

    Returns:
        dict: A dictionary of options.
    """
    o = {}
    if os.path.exists(OPTIONS_FILE):
        with open(OPTIONS_FILE, 'r') as f:
            o = json.load(f)
    return o
