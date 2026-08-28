"""Binary sensor: entertainment active state."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import BRIDGE_MODEL_ID, BRIDGE_SW_VERSION, DOMAIN, SIGNAL_ENTERTAINMENT_CHANGED

if TYPE_CHECKING:
    from . import HueEntertainmentConfigEntry, HueEntertainmentData


def bridge_device_info(data: HueEntertainmentData) -> DeviceInfo:
    """Device that groups every entity of the emulated bridge."""
    return DeviceInfo(
        identifiers={(DOMAIN, data.bridge_id)},
        name="Hue Entertainment Bridge",
        manufacturer="Hue Entertainment Bridge (emulated)",
        model=BRIDGE_MODEL_ID,
        sw_version=BRIDGE_SW_VERSION,
        entry_type=DeviceEntryType.SERVICE,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HueEntertainmentConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the entertainment active binary sensor."""
    async_add_entities([HueEntertainmentBinarySensor(entry)])


class HueEntertainmentBinarySensor(BinarySensorEntity):
    """Reports whether the bridge is currently driving these lights.

    True for a DTLS entertainment stream OR classic-mode (plain per-light
    REST, no stream) — both write real commands to the lights, so an
    automation deciding "should I treat these lights as claimed right now"
    needs both. False while paused or releasing, even if the underlying
    session/traffic hasn't actually stopped — the whole point of pausing or
    releasing is "don't count on the bridge right now."

    This only ever answers yes/no. For *why* it's no (paused vs releasing,
    for how long, waiting on what) see HueEntertainmentStatusSensor.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "entertainment_active"
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_should_poll = False

    def __init__(self, entry: HueEntertainmentConfigEntry) -> None:
        data = entry.runtime_data
        self._engine = data.engine
        self._api_server = data.api_server
        self._attr_unique_id = f"{entry.entry_id}_entertainment_active"
        self._attr_device_info = bridge_device_info(data)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_ENTERTAINMENT_CHANGED, self._on_changed)
        )

    @callback
    def _on_changed(self) -> None:
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return self._engine.is_driving_lights

    @property
    def extra_state_attributes(self) -> dict:
        if not self._engine.is_active:
            return {}
        owner = self._api_server.entertainment_owner
        return {"owner": owner} if owner else {}
