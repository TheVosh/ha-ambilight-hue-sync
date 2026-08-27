"""Safe reuse of public Home Assistant Hue config-entry connection data."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant


@dataclass(frozen=True)
class KnownHueBridge:
    """Non-secret bridge metadata exposed by an official HA Hue entry."""
    entry_id: str
    host: str
    name: str
    bridge_id: str | None


def async_known_hue_bridges(hass: HomeAssistant) -> list[KnownHueBridge]:
    """Return usable official Hue bridge entries without reading runtime internals."""
    bridges = []
    for entry in hass.config_entries.async_entries("hue"):
        host = entry.data.get(CONF_HOST)
        if not isinstance(host, str) or not host:
            continue
        bridges.append(KnownHueBridge(entry.entry_id, host, entry.title or host, entry.unique_id))
    return bridges
