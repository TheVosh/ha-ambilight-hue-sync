"""Diagnostics support for Hue Entertainment Bridge."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

if TYPE_CHECKING:
    from . import HueEntertainmentConfigEntry

TO_REDACT = frozenset(
    {
        "clientkey",
        "initial_users",
        "hue_app_key",
        "hue_client_key",
        "tv_username",
        "tv_password",
        "username",
        "password",
        "authorization",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "app_key",
        "psk",
    }
)


def redact_diagnostics(value: Any, key: str = "") -> Any:
    """Copy diagnostic data while recursively redacting credential material."""
    normalized = key.lower().replace("-", "_")
    sensitive = normalized in TO_REDACT or any(
        part in normalized
        for part in (
            "password",
            "clientkey",
            "credential",
            "authorization",
            "token",
            "secret",
            "api_key",
            "app_key",
            "psk",
        )
    )
    if sensitive:
        return "**REDACTED**"
    if isinstance(value, dict):
        return {
            item_key: redact_diagnostics(item, str(item_key)) for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_diagnostics(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_diagnostics(item) for item in value)
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HueEntertainmentConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry (no PSKs or full usernames)."""
    data = entry.runtime_data
    return {
        "entry": {
            "data": redact_diagnostics(dict(entry.data)),
            "options": redact_diagnostics(dict(entry.options)),
        },
        "bridge": {
            "bridge_id": data.bridge_id,
            "entertainment_active": data.api_server.entertainment_active,
            "entertainment_owner": _short(data.api_server.entertainment_owner),
            "paired_users": [
                {"username": _short(name), "devicetype": info.get("devicetype")}
                for name, info in data.user_store.users.items()
            ],
        },
        "engine": data.engine.stats,
        "output_backend": getattr(data.backend, "stats", {"type": type(data.backend).__name__}),
        "dtls": {"frames_coalesced_in_mailbox": data.mailbox.coalesced},
        "jointspace": data.jointspace_source.stats if data.jointspace_source else None,
    }


def _short(username: str | None) -> str | None:
    return None if username is None else f"{username[:6]}…"
