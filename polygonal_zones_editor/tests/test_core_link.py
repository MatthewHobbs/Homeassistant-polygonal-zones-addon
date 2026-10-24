"""Tests for app/core_link.py — discovery and the reload call."""

import json
from unittest.mock import MagicMock
import urllib.error

import core_link
import pytest


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://supervisor", code, "nope", {}, None)


class TestAnnounce:
    def test_announces_the_supervisor_ip(self, monkeypatch):
        """The container hostname does not resolve from Core on every install;
        the Supervisor's own ip_address does."""
        monkeypatch.setenv("SUPERVISOR_TOKEN", "sv")
        monkeypatch.setattr(core_link.socket, "gethostname", lambda: "local-polygonal-zones")
        monkeypatch.setattr(
            core_link, "_get_json", MagicMock(return_value={"data": {"ip_address": "172.30.33.1"}})
        )
        post = MagicMock(return_value=200)
        monkeypatch.setattr(core_link, "_post", post)

        core_link.announce("save-me")

        url, payload, token = post.call_args.args
        assert url == core_link.DISCOVERY_URL
        assert token == "sv"
        assert payload == {
            "service": "polygonal_zones",
            "config": {
                "host": "172.30.33.1",
                "port": 8000,
                "path": "/zones.json",
                "token": "save-me",
            },
        }

    def test_falls_back_to_the_hostname(self, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "sv")
        monkeypatch.setattr(core_link.socket, "gethostname", lambda: "local-polygonal-zones")
        monkeypatch.setattr(
            core_link, "_get_json", MagicMock(side_effect=urllib.error.URLError("no"))
        )
        post = MagicMock(return_value=200)
        monkeypatch.setattr(core_link, "_post", post)

        core_link.announce("save-me")

        assert post.call_args.args[1]["config"]["host"] == "local-polygonal-zones"

    def test_without_a_supervisor_token_it_does_nothing(self, monkeypatch):
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        post = MagicMock()
        monkeypatch.setattr(core_link, "_post", post)

        core_link.announce("save-me")

        post.assert_not_called()

    def test_a_failed_announce_is_logged_not_raised(self, monkeypatch, caplog):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "sv")
        monkeypatch.setattr(
            core_link, "_post", MagicMock(side_effect=urllib.error.URLError("down"))
        )

        core_link.announce("save-me")

        assert "Could not announce" in caplog.text


class TestNotifySaved:
    def test_success(self, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "sv")
        post = MagicMock(return_value=200)
        monkeypatch.setattr(core_link, "_post", post)

        assert core_link.notify_saved() == ("ok", "")
        # A save waits on this call, and the file is already written: it must
        # not inherit the longer discovery timeout.
        assert post.call_args.kwargs["timeout"] == core_link.NOTIFY_TIMEOUT_SECONDS
        assert core_link.NOTIFY_TIMEOUT_SECONDS < core_link.TIMEOUT_SECONDS

    def test_without_a_token_it_is_unreachable(self, monkeypatch):
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)

        assert core_link.notify_saved() == ("unreachable", "not connected to Home Assistant")

    @pytest.mark.parametrize(
        ("code", "state"),
        # 400 is what Core answers for a service the integration has not
        # registered; 404 covers the same case on another route.
        [(400, "older"), (404, "older"), (429, "refused"), (403, "refused")],
    )
    def test_http_errors_map_to_fixed_messages(self, monkeypatch, code, state):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "sv")
        monkeypatch.setattr(core_link, "_post", MagicMock(side_effect=_http_error(code)))

        got_state, note = core_link.notify_saved()

        assert got_state == state
        assert str(code) not in note  # nothing from the response is echoed

    def test_a_connection_failure_is_unreachable(self, monkeypatch, caplog):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "sv")
        monkeypatch.setattr(
            core_link, "_post", MagicMock(side_effect=urllib.error.URLError("down"))
        )

        assert core_link.notify_saved()[0] == "unreachable"
        assert "Zone reload request failed" in caplog.text


def test_redirects_are_refused(monkeypatch):
    """The Supervisor token must never follow a redirect."""
    handler = core_link._NoRedirect()

    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(None, None, 302, "Found", {}, "http://elsewhere/")


def test_post_sends_the_bearer_token_and_returns_the_status(monkeypatch):
    captured = {}

    class _Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _Opener:
        def open(self, request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return _Response()

    monkeypatch.setattr(core_link.urllib.request, "build_opener", lambda *handlers: _Opener())

    assert core_link._post("http://supervisor/x", {"a": 1}, "sv") == 204
    request = captured["request"]
    assert request.get_header("Authorization") == "Bearer sv"
    assert json.loads(request.data) == {"a": 1}
    assert captured["timeout"] == core_link.TIMEOUT_SECONDS


def test_get_json_sends_the_bearer_token_and_parses(monkeypatch):
    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"data": {"ip_address": "172.30.33.1"}}'

    class _Opener:
        def open(self, request, timeout):
            captured["request"] = request
            return _Response()

    monkeypatch.setattr(core_link.urllib.request, "build_opener", lambda *handlers: _Opener())

    assert core_link._get_json("http://supervisor/addons/self/info", "sv") == {
        "data": {"ip_address": "172.30.33.1"}
    }
    assert captured["request"].get_header("Authorization") == "Bearer sv"
