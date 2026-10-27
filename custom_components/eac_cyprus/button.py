"""Manual portal refresh, usable on device pages and custom dashboards."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import EacCoordinator, ServicePoint
from .sensor import sp_device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EacCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    @callback
    def add_buttons() -> None:
        buttons = []
        for state in coordinator.data.points.values():
            sp = state.service_point
            if sp.active and sp.device_exists and sp.id not in known:
                known.add(sp.id)
                buttons.append(EacRefreshButton(coordinator, sp))
        if buttons:
            async_add_entities(buttons)

    add_buttons()
    entry.async_on_unload(coordinator.async_add_listener(add_buttons))


class EacRefreshButton(ButtonEntity):
    """Run the normal account poll, including its history lock and debounce.

    Deliberately remains available after a failed poll so the user can retry.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "refresh_data"
    _attr_icon = "mdi:refresh"

    def __init__(self, coordinator: EacCoordinator, sp: ServicePoint) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{sp.id}_refresh_data"
        self._attr_device_info = sp_device_info(sp)

    async def async_press(self) -> None:
        await self.coordinator.async_request_refresh()
