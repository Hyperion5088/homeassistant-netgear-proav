"""Switch entities for guarded NETGEAR Pro AV controls."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ENABLE_ADMIN_CONTROLS, CONF_ENABLE_POE_CONTROLS, DOMAIN
from .coordinator import POLL_PAUSE_SECONDS, NetgearProAvCoordinator
from .helpers import (
    device_info as build_device_info,
    first_not_none,
    port_identity,
    port_label,
    port_sort_key,
    power_watts,
    serial,
    should_expose_port,
    truthy_enabled,
)
from .options import control_option_enabled


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up NETGEAR Pro AV switch entities."""
    coordinator: NetgearProAvCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchEntity] = [NetgearPollingPauseSwitch(coordinator, entry)]
    port_ids = [
        port_id
        for port_id in sorted(
            set(coordinator.data.ports) | set(coordinator.data.port_configs),
            key=port_sort_key,
        )
        if should_expose_port(
            port_id,
            coordinator.data.ports.get(port_id) or coordinator.data.port_configs.get(port_id, {}),
            coordinator.data.lag_configs,
        )
    ]
    for port_id in port_ids:
        if control_option_enabled(entry, CONF_ENABLE_ADMIN_CONTROLS):
            entities.append(NetgearPortAdminSwitch(coordinator, entry, port_id))
        if control_option_enabled(entry, CONF_ENABLE_POE_CONTROLS) and _poe_capable(
            coordinator.data.port_configs.get(port_id, {})
        ):
            entities.append(NetgearPortPoeSwitch(coordinator, entry, port_id))
    async_add_entities(entities)


def _poe_capable(config: dict[str, Any]) -> bool:
    """Return whether a port reports useful PoE support."""
    poe_status = first_not_none(config.get("poeIsValid"), config.get("status"), config.get("poeStatus"))
    if poe_status in (-1, "-1"):
        return False
    if any(key in config for key in ("enable", "powerLimitMode", "powerLimit", "classification")):
        return True
    if poe_status not in (None, 0, "0", False, ""):
        return True
    for key in ("powerUsage", "currentPower"):
        power = power_watts(config.get(key))
        if power is not None and power > 0:
            return True
    return False


def _poe_enabled(config: dict[str, Any]) -> bool | None:
    """Return whether PoE appears administratively enabled."""
    enabled = config.get("enable")
    if isinstance(enabled, bool):
        return enabled
    status = first_not_none(config.get("status"), config.get("poeStatus"), config.get("poeIsValid"))
    if status in (-1, "-1", None, ""):
        return None
    if status in (0, "0", False):
        return False
    return True


class NetgearPollingPauseSwitch(CoordinatorEntity[NetgearProAvCoordinator], SwitchEntity):
    """Pause switch polling while using the vendor web UI."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Pause Polling"

    def __init__(self, coordinator: NetgearProAvCoordinator, entry: ConfigEntry) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self.entry = entry
        switch_serial = serial(coordinator.data.device_info, entry.entry_id)
        self._attr_unique_id = f"{switch_serial}_pause_polling"

    @property
    def device_info(self) -> DeviceInfo:
        """Return switch device info."""
        return build_device_info(self.coordinator.data.device_info, self.entry.title, self.entry.entry_id)

    @property
    def is_on(self) -> bool:
        """Return whether polling is paused."""
        return self.coordinator.polling_paused

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return pause context."""
        return {
            "auto_resume_seconds": POLL_PAUSE_SECONDS,
            "pause_remaining_seconds": self.coordinator.polling_pause_remaining_seconds,
            "pause_until": self.coordinator.polling_pause_until,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Pause polling for the default maintenance window."""
        self.coordinator.pause_polling(POLL_PAUSE_SECONDS)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Resume polling and refresh data."""
        self.coordinator.resume_polling()
        await self.coordinator.async_request_refresh()


class NetgearPortControlBase(CoordinatorEntity[NetgearProAvCoordinator], SwitchEntity):
    """Base class for guarded per-port controls."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: NetgearProAvCoordinator, entry: ConfigEntry, port_id: int) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.entry = entry
        self.port_id = port_id

    @property
    def device_info(self) -> DeviceInfo:
        """Return switch device info."""
        return build_device_info(self.coordinator.data.device_info, self.entry.title, self.entry.entry_id)

    @property
    def available(self) -> bool:
        """Return whether this protected control can currently be used."""
        return super().available and self.coordinator.can_change_port(self.port_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return control context."""
        port = self.coordinator.data.ports.get(self.port_id, {})
        config = self.coordinator.data.port_configs.get(self.port_id, {})
        reason = self.coordinator.port_protection_reason(self.port_id)
        return {
            **port_identity(port, config, self.port_id),
            "description": port.get("description") or config.get("description"),
            "auto_protected": reason is not None,
            "protected_reason": reason,
            "control_unlocked": self.coordinator.is_port_unlocked(self.port_id),
        }


class NetgearPortAdminSwitch(NetgearPortControlBase):
    """Switch for port administrative state."""

    def __init__(self, coordinator: NetgearProAvCoordinator, entry: ConfigEntry, port_id: int) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, entry, port_id)
        port = coordinator.data.ports.get(port_id, {})
        config = coordinator.data.port_configs.get(port_id, {})
        self._attr_name = f"Admin Control {port_label(port or config, port_id)}"
        switch_serial = serial(coordinator.data.device_info, entry.entry_id)
        self._attr_unique_id = f"{switch_serial}_port_{port_id}_admin_enabled"

    @property
    def is_on(self) -> bool | None:
        """Return whether the port is administratively enabled."""
        if (pending := self.coordinator.pending_admin_state(self.port_id)) is not None:
            return pending
        port = self.coordinator.data.ports.get(self.port_id, {})
        config = self.coordinator.data.port_configs.get(self.port_id, {})
        return truthy_enabled(first_not_none(port.get("adminState"), config.get("adminState")))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the port."""
        await self.coordinator.async_set_port_admin_state(self.port_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the port if unlocked."""
        await self.coordinator.async_set_port_admin_state(self.port_id, False)


class NetgearPortPoeSwitch(NetgearPortControlBase):
    """Switch for port PoE administrative state."""

    def __init__(self, coordinator: NetgearProAvCoordinator, entry: ConfigEntry, port_id: int) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, entry, port_id)
        port = coordinator.data.ports.get(port_id, {})
        config = coordinator.data.port_configs.get(port_id, {})
        self._attr_name = f"PoE Switch {port_label(port or config, port_id)}"
        switch_serial = serial(coordinator.data.device_info, entry.entry_id)
        self._attr_unique_id = f"{switch_serial}_port_{port_id}_poe_enabled"

    @property
    def is_on(self) -> bool | None:
        """Return whether PoE is enabled."""
        config = self.coordinator.data.port_configs.get(self.port_id, {})
        return _poe_enabled(config)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable PoE on the port."""
        await self.coordinator.async_set_poe_enabled(self.port_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable PoE on the port if unlocked."""
        await self.coordinator.async_set_poe_enabled(self.port_id, False)
