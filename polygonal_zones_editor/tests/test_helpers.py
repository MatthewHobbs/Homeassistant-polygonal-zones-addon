import json
import sys

import pytest


def test_allow_all_ips_long_flag(monkeypatch):
    import helpers

    monkeypatch.setattr(sys, "argv", ["main.py", "--allow-all-ips"])
    assert helpers.allow_all_ips({}) is True


def test_allow_all_ips_short_flag(monkeypatch):
    import helpers

    monkeypatch.setattr(sys, "argv", ["main.py", "-a"])
    assert helpers.allow_all_ips({}) is True


def test_allow_all_ips_from_options(monkeypatch):
    import helpers

    monkeypatch.setattr(sys, "argv", ["main.py"])
    assert helpers.allow_all_ips({"allow_all_ips": True}) is True


def test_allow_all_ips_default_false(monkeypatch):
    import helpers

    monkeypatch.setattr(sys, "argv", ["main.py"])
    assert helpers.allow_all_ips({}) is False
    assert helpers.allow_all_ips({"allow_all_ips": False}) is False


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


def test_allow_request_opens_when_allow_all(monkeypatch, request_factory):
    import helpers

    monkeypatch.setattr(sys, "argv", ["main.py"])
    assert helpers.allow_request({"allow_all_ips": True}, request_factory("10.0.0.1")) is True


def test_allow_request_falls_back_to_ip_allowlist(monkeypatch, request_factory):
    import helpers

    monkeypatch.setattr(sys, "argv", ["main.py"])
    assert helpers.allow_request({}, request_factory("172.30.32.2")) is True
    assert helpers.allow_request({}, request_factory("10.0.0.1")) is False


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


def test_atomic_write_json_leaves_existing_file_intact_on_write_failure(
    tmp_path, monkeypatch
):
    """Simulate a crash mid-write: json.dump raises after the temp file is
    opened but before the rename happens. The destination must be untouched
    and no stray temp file must remain."""
    import helpers

    target = tmp_path / "zones.json"
    original = {"type": "FeatureCollection", "features": [{"id": "original"}]}
    target.write_text(json.dumps(original))

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated crash mid-write")

    monkeypatch.setattr(helpers.json, "dump", boom)

    with pytest.raises(RuntimeError, match="simulated crash"):
        helpers.atomic_write_json(str(target), {"anything": True})

    assert json.loads(target.read_text()) == original
    siblings = [p.name for p in tmp_path.iterdir()]
    assert siblings == ["zones.json"], f"temp file leaked: {siblings}"
