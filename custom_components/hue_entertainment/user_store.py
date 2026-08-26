"""Persisted PSK credentials for paired clients (backed by HA's Store helper)."""

from __future__ import annotations

import logging
import threading
from typing import Any

_LOGGER = logging.getLogger(__name__)


class UserStore:
    """Paired users (username → clientkey/devicetype).

    Reads happen from the DTLS server thread (PSK lookup during the handshake)
    while writes happen on the event loop (pairing), hence the lock.  Without
    ``ha_store`` the store is in-memory only — used by the pairing step of the
    config flow, whose users are copied into the entry.
    """

    def __init__(self, ha_store: Any = None) -> None:
        self._ha_store = ha_store
        self._lock = threading.Lock()
        self._users: dict[str, dict] = {}

    async def async_load(self) -> None:
        """Load users from the HA Store."""
        if self._ha_store is None:
            return
        data = await self._ha_store.async_load()
        if isinstance(data, dict):
            with self._lock:
                self._users = data
            _LOGGER.debug("Loaded %d user(s) from HA store", len(data))

    async def async_save(self) -> None:
        """Persist users to the HA Store (no-op for the in-memory store)."""
        if self._ha_store is None:
            return
        with self._lock:
            snapshot = dict(self._users)
        await self._ha_store.async_save(snapshot)

    def add(self, username: str, clientkey: str, devicetype: str = "unknown") -> None:
        """Add or update a user in memory; call ``async_save`` to persist."""
        with self._lock:
            self._users[username] = {"clientkey": clientkey, "devicetype": devicetype}

    def get_psk(self, username: str) -> str | None:
        """Return the clientkey for a username, or None if not found."""
        with self._lock:
            user = self._users.get(username)
            return user["clientkey"] if user else None

    def get_by_devicetype(self, devicetype: str) -> tuple[str, str] | None:
        """Return (username, clientkey) of the most recently added user with this devicetype."""
        with self._lock:
            for username, info in reversed(list(self._users.items())):
                if info.get("devicetype") == devicetype:
                    return (username, info["clientkey"])
            return None

    @property
    def users(self) -> dict[str, dict]:
        """Shallow copy of the users dict."""
        with self._lock:
            return dict(self._users)
