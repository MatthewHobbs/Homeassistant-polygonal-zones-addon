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


def test_get_file_list_walks_subdirectories(tmp_path):
    import helpers

    (tmp_path / "a.txt").write_text("a")
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "b.txt").write_text("b")

    result = helpers.get_file_list(str(tmp_path))
    assert sorted(result) == sorted([
        str(tmp_path / "a.txt"),
        str(nested / "b.txt"),
    ])


def test_get_file_list_empty_dir(tmp_path):
    import helpers

    assert helpers.get_file_list(str(tmp_path)) == []


def test_load_options_returns_empty_dict_when_missing(tmp_options_file):
    import helpers

    assert not tmp_options_file.exists()
    assert helpers.load_options() == {}


def test_load_options_reads_existing_file(tmp_options_file):
    import helpers

    tmp_options_file.write_text(json.dumps({"allow_all_ips": True, "zone_colour": "purple"}))
    assert helpers.load_options() == {"allow_all_ips": True, "zone_colour": "purple"}


def test_init_logging_returns_configured_logger():
    import logging

    import helpers

    logger = helpers.init_logging()
    assert isinstance(logger, logging.Logger)
    assert logger.level == logging.INFO
    assert logger.handlers, "expected at least one handler attached"
