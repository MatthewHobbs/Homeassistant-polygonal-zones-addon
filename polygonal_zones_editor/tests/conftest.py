import json
from pathlib import Path
from types import SimpleNamespace

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"


@pytest.fixture(autouse=True)
def chdir_to_app(monkeypatch):
    # StaticFiles(directory="static") and the index template read are
    # resolved relative to cwd.
    monkeypatch.chdir(APP_DIR)


@pytest.fixture(autouse=True)
def reset_failure_records():
    # Module state: failures left by one test would push a later one into 429.
    import main

    main._auth_failures.clear()
    main._last_sweep = 0.0
    yield
    main._auth_failures.clear()
    main._last_sweep = 0.0


@pytest.fixture
def tmp_zones_file(tmp_path, monkeypatch):
    import const
    import main

    zones = tmp_path / "zones.json"
    zones.write_text(json.dumps({"type": "FeatureCollection", "features": []}))
    monkeypatch.setattr(const, "ZONES_FILE", str(zones))
    monkeypatch.setattr(main, "ZONES_FILE", str(zones))
    return zones


@pytest.fixture
def tmp_options_file(tmp_path, monkeypatch):
    import const
    import helpers

    options_path = tmp_path / "options.json"
    monkeypatch.setattr(const, "OPTIONS_FILE", str(options_path))
    monkeypatch.setattr(helpers, "OPTIONS_FILE", str(options_path))
    return options_path


def make_request(host: str | None = "testclient") -> SimpleNamespace:
    from const import TRANSPORT_PEER_SCOPE_KEY

    return SimpleNamespace(
        client=SimpleNamespace(host=host), scope={TRANSPORT_PEER_SCOPE_KEY: host}
    )


@pytest.fixture
def request_factory():
    return make_request
