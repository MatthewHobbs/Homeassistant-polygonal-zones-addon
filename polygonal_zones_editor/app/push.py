"""Deliver the zones to the companion integration.

After every save the add-on pushes zones.json to a dedicated endpoint of the
integration through Core's API, authenticated with the add-on's own Supervisor
token. Nobody types, copies or stores a secret.

This is best effort and must never slow or fail a save: a daemon thread does
the work, and a save only sets an event. Failures are shown in the editor
(`status()`), never swallowed silently, and a push that cannot succeed is not
retried until the zones change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import http.client
import json
import logging
import os
import random
import threading
import time
from typing import TYPE_CHECKING
import urllib.error
import urllib.request

import const
from helpers import atomic_write_json

if TYPE_CHECKING:
    from collections.abc import Callable

_LOGGER = logging.getLogger(__name__)

# What the editor shows. "off" and "pending" are decided here; the rest come
# from classify().
OFF, PENDING, DELIVERED, WAITING, REJECTED, RETRYING = (
    "off",
    "pending",
    "delivered",
    "waiting",
    "rejected",
    "retrying",
)

# How the next attempt is scheduled after a classified reply.
NONE, FIXED, BACKOFF, UNTIL_CHANGED = "none", "fixed", "backoff", "until_changed"


@dataclass(frozen=True)
class Outcome:
    """What one reply from Core means, and when to try again."""

    state: str
    detail: str | None
    schedule: str
    delay: float = 0.0


def clean_message(body: bytes) -> str | None:
    """The integration's `message`, made safe to show: plain, short, one line."""
    try:
        parsed = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return None
    message = parsed.get("message") if isinstance(parsed, dict) else None
    if not isinstance(message, str):
        return None
    flat = "".join(ch if ch.isprintable() else " " for ch in message).strip()
    return flat[: const.PUSH_DETAIL_MAX_CHARS] or None


def classify(status: int | None, body: bytes) -> Outcome:
    """Map a reply (None: no reply at all) to a state and a retry rule."""
    if status == 200:
        return Outcome(DELIVERED, None, NONE)
    if status in (401, 403):
        return Outcome(
            REJECTED,
            "Home Assistant refused the add-on's access.",
            FIXED,
            const.PUSH_REFUSED_RETRY_SECONDS,
        )
    if status == 404:
        return Outcome(
            WAITING,
            "The integration is not installed, or is too old to receive zones from the add-on.",
            FIXED,
            const.PUSH_WAITING_RETRY_SECONDS,
        )
    if status == 409:
        return Outcome(
            WAITING,
            "The integration is not set up to receive zones from the add-on.",
            FIXED,
            const.PUSH_WAITING_RETRY_SECONDS,
        )
    if status == 422:
        return Outcome(
            REJECTED,
            clean_message(body) or "The integration refused the zones.",
            UNTIL_CHANGED,
        )
    if status == 429:
        return Outcome(
            RETRYING,
            "Waiting a moment before sending again.",
            FIXED,
            const.PUSH_RATE_LIMITED_RETRY_SECONDS,
        )
    return Outcome(RETRYING, "Could not reach Home Assistant.", BACKOFF)


def backoff_delay(attempt: int, rng: Callable[[], float] = random.random) -> float:
    """2 s doubling to 60 s, spread by plus or minus the jitter fraction."""
    base = min(
        const.PUSH_BACKOFF_MIN_SECONDS * (2 ** max(attempt - 1, 0)),
        const.PUSH_BACKOFF_MAX_SECONDS,
    )
    spread = const.PUSH_BACKOFF_JITTER
    return base * (1 - spread + 2 * spread * rng())


class Pusher:
    """Keeps the integration's copy of the zones current."""

    def __init__(
        self,
        options: dict,
        *,
        etag_for: Callable[[bytes], str],
        token: str | None = None,
        opener: Callable = urllib.request.urlopen,
        monotonic: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._etag_for = etag_for
        self._token = os.environ.get(const.SUPERVISOR_TOKEN_ENV, "") if token is None else token
        self._enabled = bool(options.get("push_to_core", True)) and bool(self._token)
        self._opener = opener
        self._monotonic = monotonic
        self._rng = rng
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._state = PENDING
        self._detail: str | None = None
        self._attempt = 0
        self._rejected_etag: str | None = None
        # Nothing sent since this start: the first run always sends, whatever was
        # stored (the integration may have lost its copy meanwhile).
        self._last_ok = -float("inf")
        self._logged_state: str | None = None
        self._delivered_etag, self._delivered_at = self._load_state()

    # --- public ---------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    def wake(self) -> None:
        """A save happened. Never blocks."""
        self._event.set()

    def start(self) -> None:
        if not self._enabled:
            _LOGGER.info(
                "Not sending zones to Home Assistant (push_to_core is off, or there is no Supervisor)."
            )
            return
        threading.Thread(target=self._loop, name="push", daemon=True).start()

    def status(self) -> dict:
        """The state shown in the editor."""
        current = self._current_etag()
        in_sync = current is not None and current == self._delivered_etag
        with self._lock:
            state, detail = self._state, self._detail
            rejected = self._rejected_etag
        if not self._enabled:
            state, detail = OFF, None
        elif state == DELIVERED and not in_sync:
            state, detail = PENDING, None  # zones changed since
        elif state == REJECTED and rejected is not None and rejected != current:
            state, detail = PENDING, None  # those exact zones were refused; they have changed
        return {
            "state": state,
            "detail": detail,
            "delivered_at": self._delivered_at,
            "in_sync": in_sync,
        }

    def run_once(self) -> float | None:
        """One delivery attempt. Seconds until the next, or None to wait for a save."""
        if not self._enabled:
            return None
        data = self._read_zones()
        if data is None:
            self._note(RETRYING, "The zones file could not be read.")
            return const.PUSH_WAITING_RETRY_SECONDS
        etag = self._etag_for(data)
        if etag == self._rejected_etag:
            return None  # refused for exactly these bytes; wait for a change
        if etag == self._delivered_etag:
            due = self._last_ok + const.PUSH_RESYNC_SECONDS - self._monotonic()
            if due > 0:
                self._note(DELIVERED, None)
                return due
        outcome = classify(*self._post(data))
        return self._apply(outcome, etag)

    # --- internals ------------------------------------------------------

    def _loop(self) -> None:  # pragma: no cover - drives run_once with a real thread
        while True:
            self._event.clear()
            delay = self.run_once()
            self._event.wait(timeout=delay)

    def _apply(self, outcome: Outcome, etag: str) -> float | None:
        if outcome.state == DELIVERED:
            self._attempt = 0
            self._rejected_etag = None
            self._last_ok = self._monotonic()
            self._delivered_etag = etag
            self._delivered_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._save_state()
            self._note(DELIVERED, None)
            return const.PUSH_RESYNC_SECONDS
        if outcome.schedule == BACKOFF:
            self._attempt += 1
        else:
            self._attempt = 0
        if outcome.schedule == UNTIL_CHANGED:
            self._rejected_etag = etag
        self._note(outcome.state, outcome.detail)
        if outcome.schedule == FIXED:
            return outcome.delay
        if outcome.schedule == BACKOFF:
            return backoff_delay(self._attempt, self._rng)
        return None

    def _note(self, state: str, detail: str | None) -> None:
        """Record the state; log only when it changes, never per retry."""
        with self._lock:
            self._state, self._detail = state, detail
            changed = state != self._logged_state
            self._logged_state = state
        if not changed:
            return
        if state in (REJECTED, RETRYING):
            _LOGGER.warning("Zones not delivered to Home Assistant (%s): %s", state, detail)
        elif state == WAITING:
            _LOGGER.info("Waiting for the integration to accept zones: %s", detail)
        else:  # DELIVERED: the only other state that is noted
            _LOGGER.info("Zones delivered to Home Assistant.")

    def _read_zones(self) -> bytes | None:
        try:
            with open(const.ZONES_FILE, "rb") as f:
                return f.read()
        except OSError:
            return None

    def _current_etag(self) -> str | None:
        data = self._read_zones()
        return None if data is None else self._etag_for(data)

    def _post(self, data: bytes) -> tuple[int | None, bytes]:
        request = urllib.request.Request(
            const.PUSH_ENDPOINT,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener(request, timeout=const.PUSH_TIMEOUT_SECONDS) as response:
                return response.status, response.read(const.PUSH_REPLY_LIMIT_BYTES)
        except urllib.error.HTTPError as err:
            try:
                return err.code, err.read(const.PUSH_REPLY_LIMIT_BYTES)
            except (OSError, http.client.HTTPException):
                return err.code, b""
        except (OSError, http.client.HTTPException):
            return None, b""

    def _load_state(self) -> tuple[str | None, str | None]:
        try:
            with open(const.PUSH_STATE_FILE, encoding="utf-8") as f:
                stored = json.load(f)
        except (OSError, ValueError):
            return None, None
        if not isinstance(stored, dict):
            return None, None
        etag, at = stored.get("delivered_etag"), stored.get("delivered_at")
        return (etag if isinstance(etag, str) else None, at if isinstance(at, str) else None)

    def _save_state(self) -> None:
        try:
            atomic_write_json(
                const.PUSH_STATE_FILE,
                {"delivered_etag": self._delivered_etag, "delivered_at": self._delivered_at},
            )
        except (OSError, ValueError):
            _LOGGER.warning(
                "Could not record the last delivery; the zones will be sent again on restart."
            )
