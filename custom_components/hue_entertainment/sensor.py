"""Sensor: bridge status — the diagnostic counterpart to the active/driving boolean."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .binary_sensor import bridge_device_info
from .const import SIGNAL_ENTERTAINMENT_CHANGED

if TYPE_CHECKING:
    from . import HueEntertainmentConfigEntry

# Keep in sync with EntertainmentEngine.status — that property is the single
# source of truth this sensor reads from; this list only needs to match its
# possible return values.
STATUS_OPTIONS = ["idle", "streaming", "classic", "paused", "releasing"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HueEntertainmentConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the bridge status sensor."""
    async_add_entities([HueEntertainmentStatusSensor(entry)])


class HueEntertainmentStatusSensor(SensorEntity):
    """What the bridge is actually doing right now.

    HueEntertainmentBinarySensor answers one question — "should an
    automation treat these lights as claimed" — and collapses paused,
    releasing, and genuinely idle all down to the same `off`, because for
    that question they're the same answer. This sensor is where they become
    distinguishable: which of idle/streaming/classic/paused/releasing, plus
    timing detail in the attributes (how much longer a pause has left, or
    whether a release is still waiting politely on the TV vs. forcing).

    Deliberately one entity with a small fixed vocabulary (`device_class:
    enum`), not several independent booleans — a caller checking "is it
    paused" and "is it releasing" as two separate flags could, under a bug,
    see both true at once, or catch an in-between reading no state should
    ever produce. One value can't disagree with itself.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUS_OPTIONS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, entry: HueEntertainmentConfigEntry) -> None:
        data = entry.runtime_data
        self._engine = data.engine
        self._attr_unique_id = f"{entry.entry_id}_status"
        self._attr_device_info = bridge_device_info(data)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_ENTERTAINMENT_CHANGED, self._on_changed)
        )

    @callback
    def _on_changed(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        return self._engine.status

    @property
    def extra_state_attributes(self) -> dict:
        return self._engine.status_attributes
