"""JSON-backed user store for persisted PSK credentials."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)


class UserStore:
    """Thread-safe user store with optional JSON persistence.

    Pass ``path=None`` and no ``ha_store`` for in-memory-only mode (useful in tests).
    Pass a ``Path`` to a JSON file to persist users via direct file I/O.
    Pass an HA ``Store`` instance (``ha_store``) to persist via HA's storage helper.
    """

    def __init__(self, path: Path | None = None, ha_store: Any = None) -> None:
        self._path = path
        self._ha_store = ha_store
        self._lock = threading.Lock()
        self._users: dict[str, dict] = {}
        if path is not None and path.exists():
            self._load()

    async def async_load(self) -> None:
        """Load users from HA Store (or fall back to sync file load)."""
        if self._ha_store is not None:
            data = await self._ha_store.async_load()
            if isinstance(data, dict):
                with self._lock:
                    self._users = data
                _LOGGER.debug("Loaded %d user(s) from HA store", len(data))
        elif self._path is not None and self._path.exists():
            self._load()

    async def async_save(self) -> None:
        """Persist users to HA Store (or no-op if not configured)."""
        if self._ha_store is not None:
            with self._lock:
                snapshot = dict(self._users)
            await self._ha_store.async_save(snapshot)

    def add(self, username: str, clientkey: str, devicetype: str = "unknown") -> None:
        """Add or update a user and persist to disk (sync file path only)."""
        with self._lock:
            self._users[username] = {"clientkey": clientkey, "devicetype": devicetype}
            self._save()

    def get_psk(self, username: str) -> str | None:
        """Return the clientkey for a username, or None if not found."""
        with self._lock:
            user = self._users.get(username)
            return user["clientkey"] if user else None

    def get_by_devicetype(self, devicetype: str) -> tuple[str, str] | None:
        """Return (username, clientkey) for an existing user with this devicetype.

        Returns the most recently added match (last in insertion order).
        Returns None if no match.
        """
        with self._lock:
            for username, info in reversed(list(self._users.items())):
                if info.get("devicetype") == devicetype:
                    return (username, info["clientkey"])
            return None

    @property
    def users(self) -> dict[str, dict]:
        """Return a shallow copy of the users dict (for inspection/tests)."""
        with self._lock:
            return dict(self._users)

    def _save(self) -> None:
        """Persist users to the JSON file (called with lock held)."""
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._users, indent=2))
        except OSError:
            _LOGGER.exception("Failed to save user store to %s", self._path)

    def _load(self) -> None:
        """Load users from the JSON file."""
        try:
            data = json.loads(self._path.read_text())  # type: ignore[union-attr]
            if isinstance(data, dict):
                self._users = data
                _LOGGER.debug("Loaded %d user(s) from %s", len(data), self._path)
        except (OSError, json.JSONDecodeError):
            _LOGGER.exception("Failed to load user store from %s", self._path)
