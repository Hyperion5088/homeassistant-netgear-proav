"""Text entities for NETGEAR Pro AV switches."""

from __future__ import annotations

from typing import Any

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NetgearProAvCoordinator
from .helpers import device_info as build_device_info
from .helpers import serial


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up NETGEAR Pro AV text entities."""
    coordinator: NetgearProAvCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([NetgearPortDescriptionInputText(coordinator, entry)])


class NetgearPortDescriptionInputText(CoordinatorEntity[NetgearProAvCoordinator], TextEntity):
    """Single manual description input for the selected port."""

    _attr_has_entity_name = True
    _attr_mode = TextMode.TEXT
    _attr_name = "Port Description Input"
    _attr_native_max = 64

    def __init__(self, coordinator: NetgearProAvCoordinator, entry: ConfigEntry) -> None:
        """Initialize the text entity."""
        super().__init__(coordinator)
        self.entry = entry
        switch_serial = serial(coordinator.data.device_info, entry.entry_id)
        self._attr_unique_id = f"{switch_serial}_port_description_input"

    @property
    def device_info(self) -> DeviceInfo:
        """Return switch device info."""
        return build_device_info(self.coordinator.data.device_info, self.entry.title, self.entry.entry_id)

    @property
    def native_value(self) -> str | None:
        """Return the pending description."""
        return self.coordinator.description_input

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return selected target context."""
        return self.coordinator.selected_description_context()

    async def async_set_value(self, value: str) -> None:
        """Set the pending description text."""
        self.coordinator.set_description_input(value)
