"""Binary sensor: entertainment active state."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_ENTERTAINMENT_CHANGED
from .entertainment import EntertainmentEngine
from .hue_api import HueAPIServer


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the entertainment active binary sensor."""
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([HueEntertainmentBinarySensor(data["engine"], data["api_server"], entry)])


class HueEntertainmentBinarySensor(BinarySensorEntity):
    """Reports whether Hue Entertainment mode is currently active."""

    _attr_has_entity_name = True
    _attr_translation_key = "entertainment_active"
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_should_poll = False

    def __init__(
        self,
        engine: EntertainmentEngine,
        api_server: HueAPIServer,
        entry: ConfigEntry,
    ) -> None:
        self._engine = engine
        self._api_server = api_server
        self._attr_unique_id = f"{entry.entry_id}_entertainment_active"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_ENTERTAINMENT_CHANGED, self._on_changed)
        )

    @callback
    def _on_changed(self) -> None:
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return self._engine.is_active

    @property
    def extra_state_attributes(self) -> dict:
        if not self._engine.is_active:
            return {}
        owner = self._api_server.entertainment_owner
        return {"owner": owner} if owner else {}
