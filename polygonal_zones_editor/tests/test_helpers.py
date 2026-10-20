import json
import re

import pytest


def test_allowed_ip_permits_supervisor_host(request_factory):
    import helpers

    assert helpers.allowed_ip(request_factory("172.30.32.2")) is True


def test_allowed_ip_blocks_other_hosts(request_factory):
    import helpers

    assert helpers.allowed_ip(request_factory("10.0.0.1")) is False
    assert helpers.allowed_ip(request_factory("127.0.0.1")) is False


def test_allowed_ip_blocks_missing_host(request_factory):
    import helpers

    assert helpers.allowed_ip(request_factory(None)) is False


def test_allowed_ip_uses_transport_peer_not_resolved_client(request_factory):
    from const import TRANSPORT_PEER_SCOPE_KEY
    import helpers

    forwarded = request_factory("172.30.32.2")
    forwarded.scope = {TRANSPORT_PEER_SCOPE_KEY: "192.168.1.50"}
    assert helpers.allowed_ip(forwarded) is False

    unrecorded = request_factory("172.30.32.2")
    unrecorded.scope = {}
    assert helpers.allowed_ip(unrecorded) is False


def test_load_options_returns_empty_dict_when_missing(tmp_options_file):
    import helpers

    assert not tmp_options_file.exists()
    assert helpers.load_options() == {}


def test_load_options_reads_existing_file(tmp_options_file):
    import helpers

    tmp_options_file.write_text(json.dumps({"allow_all_ips": True, "zone_colour": "purple"}))
    assert helpers.load_options() == {"allow_all_ips": True, "zone_colour": "purple"}


def test_configure_logging_sets_root_handler():
    import logging

    import helpers

    # Reset root logger so basicConfig actually runs.
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    helpers.configure_logging()
    assert root.level == logging.INFO
    assert root.handlers, "expected at least one handler on root"


def test_configure_logging_is_idempotent():
    import logging

    import helpers

    helpers.configure_logging()
    before = len(logging.getLogger().handlers)
    helpers.configure_logging()
    # basicConfig no-ops when handlers already exist; we must not accumulate.
    assert len(logging.getLogger().handlers) == before


def test_configure_logging_applies_level_on_subsequent_call():
    import logging

    import helpers

    helpers.configure_logging(logging.INFO)
    helpers.configure_logging(logging.DEBUG)
    assert logging.getLogger().level == logging.DEBUG
    # Reset so other tests aren't affected.
    helpers.configure_logging(logging.INFO)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("debug", "DEBUG"),
        ("INFO", "INFO"),
        ("Warning", "WARNING"),
        ("error", "ERROR"),
        ("critical", "CRITICAL"),
        (None, "INFO"),
        ("nonsense", "INFO"),
        (42, "INFO"),
    ],
)
def test_resolve_log_level(value, expected):
    import logging

    import helpers

    assert helpers.resolve_log_level(value) == getattr(logging, expected)


def test_load_options_falls_back_on_malformed_json(tmp_options_file, caplog):
    import logging

    import helpers

    tmp_options_file.write_text("{not valid json")
    with caplog.at_level(logging.ERROR):
        assert helpers.load_options() == {}
    assert any("Failed to read" in rec.message for rec in caplog.records)


def test_load_options_falls_back_when_not_an_object(tmp_options_file, caplog):
    import logging

    import helpers

    tmp_options_file.write_text("[1, 2, 3]")
    with caplog.at_level(logging.WARNING):
        assert helpers.load_options() == {}
    assert any("JSON object" in rec.message for rec in caplog.records)


def test_atomic_write_json_writes_new_file(tmp_path):
    import helpers

    target = tmp_path / "zones.json"
    payload = {"type": "FeatureCollection", "features": [{"id": 1}]}

    helpers.atomic_write_json(str(target), payload)

    assert json.loads(target.read_text()) == payload


def test_atomic_write_json_returns_the_bytes_it_wrote(tmp_path):
    import helpers

    target = tmp_path / "zones.json"
    assert helpers.atomic_write_json(str(target), {"a": [1, 2.5]}) == target.read_bytes()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_atomic_write_json_refuses_non_finite_numbers_without_touching_disk(tmp_path, value):
    import helpers

    target = tmp_path / "zones.json"
    target.write_text("{}")
    with pytest.raises(ValueError):
        helpers.atomic_write_json(str(target), {"properties": {"w": value}})
    assert [p.name for p in tmp_path.iterdir()] == ["zones.json"]
    assert target.read_text() == "{}"


def test_atomic_write_json_replaces_existing_file(tmp_path):
    import helpers

    target = tmp_path / "zones.json"
    target.write_text(json.dumps({"type": "FeatureCollection", "features": []}))
    new_payload = {"type": "FeatureCollection", "features": [{"id": 42}]}

    helpers.atomic_write_json(str(target), new_payload)

    assert json.loads(target.read_text()) == new_payload


def test_atomic_write_json_leaves_existing_file_intact_on_serialisation_failure(
    tmp_path, monkeypatch
):
    import helpers

    target = tmp_path / "zones.json"
    original = {"type": "FeatureCollection", "features": [{"id": "original"}]}
    target.write_text(json.dumps(original))

    class Unserialisable:
        pass

    with pytest.raises(TypeError):
        helpers.atomic_write_json(str(target), Unserialisable())

    # Previous contents must still be intact — this is the crash-safety guarantee.
    assert json.loads(target.read_text()) == original

    # And no temp files should be left behind in the destination directory.
    siblings = [p.name for p in tmp_path.iterdir()]
    assert siblings == ["zones.json"], f"temp file leaked: {siblings}"


def test_atomic_write_json_swallows_dir_open_error(tmp_path, monkeypatch):
    """If opening the parent directory for fsync fails, the write still
    succeeds — durability of the directory entry is best-effort. Covers
    the `os.open → OSError → return` branch in helpers.py.
    """
    import os

    import helpers

    real_open = os.open

    def selective_open(path, *args, **kwargs):
        # Reject *directory* opens (O_RDONLY on the parent dir) but let
        # the tempfile.mkstemp open still succeed.
        if os.path.isdir(path):
            raise OSError("simulated directory open failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", selective_open)

    target = tmp_path / "zones.json"
    payload = {"type": "FeatureCollection", "features": []}
    # The write should complete regardless of the directory-fsync failure.
    helpers.atomic_write_json(str(target), payload)
    assert json.loads(target.read_text()) == payload


def test_atomic_write_json_fsyncs_parent_directory(tmp_path, monkeypatch):
    """After the rename, the parent directory must be fsynced so the new
    directory entry is durable on hard power-off. Without this, on some
    filesystems (overlayfs, vfat, tmpfs) the rename may not be persisted
    before power is lost.
    """
    import os

    import helpers

    fsynced_fds = []
    original_fsync = os.fsync

    def track_fsync(fd):
        fsynced_fds.append(fd)
        return original_fsync(fd)

    monkeypatch.setattr(os, "fsync", track_fsync)

    target = tmp_path / "zones.json"
    helpers.atomic_write_json(str(target), {"type": "FeatureCollection", "features": []})

    # Two fsyncs expected: one on the temp file (data durability), one on
    # the parent directory (rename durability).
    assert len(fsynced_fds) == 2, f"expected 2 fsync calls, got {len(fsynced_fds)}"


def test_atomic_write_json_leaves_existing_file_intact_on_write_failure(tmp_path, monkeypatch):
    """Simulate a crash mid-write: fsync raises after the temp file is
    written but before the rename happens. The destination must be untouched
    and no stray temp file must remain."""
    import helpers

    target = tmp_path / "zones.json"
    original = {"type": "FeatureCollection", "features": [{"id": "original"}]}
    target.write_text(json.dumps(original))

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated crash mid-write")

    monkeypatch.setattr(helpers.os, "fsync", boom)

    with pytest.raises(RuntimeError, match="simulated crash"):
        helpers.atomic_write_json(str(target), {"anything": True})

    assert json.loads(target.read_text()) == original
    siblings = [p.name for p in tmp_path.iterdir()]
    assert siblings == ["zones.json"], f"temp file leaked: {siblings}"


def _keys_under(path, section):
    """The two-space-indented keys of a top-level YAML block; no YAML parser needed."""
    keys, inside = [], False
    for line in path.read_text().splitlines():
        if not line.startswith(" ") and line.strip():
            inside = line.rstrip() == f"{section}:"
        elif inside and re.match(r"^  [a-z_]+:", line):
            keys.append(line.split(":")[0].strip())
    return keys


def test_supervisor_watchdog_probes_the_open_health_check():
    # /healthz is the one path that needs no token, so the watchdog can reach it.
    from tests.conftest import APP_DIR

    config = (APP_DIR.parent / "config.yaml").read_text().splitlines()
    assert 'watchdog: "http://[HOST]:[PORT:8000]/healthz"' in config


def test_every_option_has_a_name_and_description_in_the_ui():
    # A missing entry shows Supervisor users the raw key and no explanation.
    from tests.conftest import APP_DIR

    addon = APP_DIR.parent
    schema = _keys_under(addon / "config.yaml", "schema")
    translated = _keys_under(addon / "translations" / "en.yaml", "configuration")
    assert "overlay_entities" in schema
    assert sorted(schema) == sorted(translated)
    text = (addon / "translations" / "en.yaml").read_text()
    assert "readable by anyone holding the save_token" in " ".join(text.split())
