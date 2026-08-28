"""Light control surface for Ambilight Hue synchronization."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .binary_sensor import bridge_device_info
from .const import SIGNAL_ENTERTAINMENT_CHANGED

if TYPE_CHECKING:
    from . import HueEntertainmentConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HueEntertainmentConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the compact sync control automatically."""
    async_add_entities([AmbilightHueSyncLight(entry)])


class AmbilightHueSyncLight(LightEntity):
    """Power and intensity controls backed by the runtime sync controller."""

    _attr_name = "Ambilight Hue Sync"
    _attr_has_entity_name = False
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_should_poll = False

    def __init__(self, entry: HueEntertainmentConfigEntry) -> None:
        data = entry.runtime_data
        self._control = data.control
        self._attr_unique_id = f"{entry.entry_id}_ambilight_hue_sync"
        self._attr_device_info = bridge_device_info(data)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_ENTERTAINMENT_CHANGED, self._on_changed)
        )

    @callback
    def _on_changed(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._control.available

    @property
    def is_on(self) -> bool:
        return self._control.enabled

    @property
    def brightness(self) -> int:
        return round(self._control.intensity * 255)

    async def async_turn_on(self, **kwargs: Any) -> None:
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        if brightness is not None:
            resolved = int(brightness)
            if resolved <= 0:
                await self._control.async_set_enabled(False)
                return
            await self._control.async_set_intensity(resolved / 255)
        await self._control.async_set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._control.async_set_enabled(False)
