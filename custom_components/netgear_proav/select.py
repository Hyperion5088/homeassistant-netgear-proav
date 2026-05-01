"""Select entities for NETGEAR Pro AV switches."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ENABLE_FAN_MODE_CONTROL, CONF_ENABLE_REBOOT_CONTROL, DOMAIN
from .coordinator import NetgearProAvCoordinator
from .helpers import device_info as build_device_info, serial
from .options import control_option_enabled, option_enabled

FAN_MODE_TO_NAME = {
    1: "Off",
    2: "Quiet",
    3: "Cool",
}
FAN_NAME_TO_MODE = {name: mode for mode, name in FAN_MODE_TO_NAME.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up NETGEAR Pro AV select entities."""
    coordinator: NetgearProAvCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SelectEntity] = [NetgearDescriptionTargetSelect(coordinator, entry)]
    if (
        control_option_enabled(entry, CONF_ENABLE_FAN_MODE_CONTROL)
        and _fan_mode(coordinator.data.device_info) is not None
    ):
        entities.append(NetgearFanModeSelect(coordinator, entry))
    if option_enabled(entry, CONF_ENABLE_REBOOT_CONTROL):
        entities.append(NetgearRebootConfirmationSelect(coordinator, entry))
    async_add_entities(entities)


def _fan_mode(info: dict[str, Any]) -> int | None:
    """Return the current fan mode code."""
    value = info.get("fanMode")
    return value if isinstance(value, int) else None


class NetgearFanModeSelect(CoordinatorEntity[NetgearProAvCoordinator], SelectEntity):
    """Switch fan mode select."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Control Fan Mode"
    _attr_options = list(FAN_NAME_TO_MODE)

    def __init__(self, coordinator: NetgearProAvCoordinator, entry: ConfigEntry) -> None:
        """Initialize the select."""
        super().__init__(coordinator)
        self.entry = entry
        switch_serial = serial(coordinator.data.device_info, entry.entry_id)
        self._attr_unique_id = f"{switch_serial}_fan_mode"

    @property
    def device_info(self) -> DeviceInfo:
        """Return switch device info."""
        return build_device_info(self.coordinator.data.device_info, self.entry.title, self.entry.entry_id)

    @property
    def current_option(self) -> str | None:
        """Return the current fan mode."""
        mode = _fan_mode(self.coordinator.data.device_info)
        if mode is None:
            return None
        return FAN_MODE_TO_NAME.get(mode, str(mode))

    async def async_select_option(self, option: str) -> None:
        """Set the fan mode."""
        await self.coordinator.client.async_set_fan_mode(FAN_NAME_TO_MODE[option])
        await self.coordinator.async_request_refresh()


class NetgearDescriptionTargetSelect(CoordinatorEntity[NetgearProAvCoordinator], SelectEntity):
    """Select the port used by the manual description input."""

    _attr_has_entity_name = True
    _attr_name = "Port Description Target"

    def __init__(self, coordinator: NetgearProAvCoordinator, entry: ConfigEntry) -> None:
        """Initialize the select."""
        super().__init__(coordinator)
        self.entry = entry
        switch_serial = serial(coordinator.data.device_info, entry.entry_id)
        self._attr_unique_id = f"{switch_serial}_description_target"

    @property
    def device_info(self) -> DeviceInfo:
        """Return switch device info."""
        return build_device_info(self.coordinator.data.device_info, self.entry.title, self.entry.entry_id)

    @property
    def options(self) -> list[str]:
        """Return selectable port labels."""
        return self.coordinator.description_target_options()

    @property
    def current_option(self) -> str | None:
        """Return the selected port label."""
        return self.coordinator.description_target_option

    async def async_select_option(self, option: str) -> None:
        """Select a port for manual description editing."""
        self.coordinator.set_description_target(option)


class NetgearRebootConfirmationSelect(CoordinatorEntity[NetgearProAvCoordinator], SelectEntity):
    """Two-step reboot confirmation select."""

    _attr_has_entity_name = True
    _attr_name = "Reboot Confirmation"
    _attr_options = ["Cancel", "Reboot"]

    def __init__(self, coordinator: NetgearProAvCoordinator, entry: ConfigEntry) -> None:
        """Initialize the select."""
        super().__init__(coordinator)
        self.entry = entry
        switch_serial = serial(coordinator.data.device_info, entry.entry_id)
        self._attr_unique_id = f"{switch_serial}_reboot_confirmation"

    @property
    def device_info(self) -> DeviceInfo:
        """Return switch device info."""
        return build_device_info(self.coordinator.data.device_info, self.entry.title, self.entry.entry_id)

    @property
    def current_option(self) -> str | None:
        """Return the current confirmation state."""
        return self.coordinator.reboot_confirmation

    async def async_select_option(self, option: str) -> None:
        """Arm or disarm reboot."""
        self.coordinator.set_reboot_confirmation(option)
