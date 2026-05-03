"""Lock entities for guarded NETGEAR Pro AV controls."""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NetgearProAvCoordinator
from .helpers import device_info as build_device_info
from .helpers import port_display_name, port_identity, port_sort_key, serial, should_expose_port
from .options import any_port_controls_enabled


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up NETGEAR Pro AV lock entities."""
    if not any_port_controls_enabled(entry):
        return
    coordinator: NetgearProAvCoordinator = hass.data[DOMAIN][entry.entry_id]
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
    async_add_entities([NetgearPortControlLock(coordinator, entry, port_id) for port_id in port_ids])


class NetgearPortControlLock(CoordinatorEntity[NetgearProAvCoordinator], LockEntity):
    """Manual protection used to guard port configuration controls."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True

    def __init__(self, coordinator: NetgearProAvCoordinator, entry: ConfigEntry, port_id: int) -> None:
        """Initialize the lock."""
        super().__init__(coordinator)
        self.entry = entry
        self.port_id = port_id
        port = coordinator.data.ports.get(port_id, {})
        config = coordinator.data.port_configs.get(port_id, {})
        state = coordinator.data.port_states.get(port_id, {})
        self._attr_name = f"{port_display_name(port_id, port, config, state)} Protection"
        switch_serial = serial(coordinator.data.device_info, entry.entry_id)
        self._attr_unique_id = f"{switch_serial}_port_{port_id}_control_lock"

    @property
    def device_info(self) -> DeviceInfo:
        """Return switch device info."""
        return build_device_info(self.coordinator.data.device_info, self.entry.title, self.entry.entry_id)

    @property
    def is_locked(self) -> bool:
        """Return whether port configuration controls are protected."""
        return self.coordinator.is_port_locked(self.port_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return lock context."""
        port = self.coordinator.data.ports.get(self.port_id, {})
        config = self.coordinator.data.port_configs.get(self.port_id, {})
        reason = self.coordinator.port_protection_reason(self.port_id)
        return {
            **port_identity(port, config, self.port_id),
            "auto_protected": reason is not None,
            "protected_reason": reason,
            "manual_protection": self.coordinator.is_port_manually_locked(self.port_id),
            "temporarily_unlocked": self.coordinator.is_port_temporarily_unlocked(self.port_id),
            "auto_reprotect_seconds": self.coordinator.auto_protect_timeout,
        }

    async def async_lock(self, **kwargs: Any) -> None:
        """Protect port configuration controls."""
        self.coordinator.lock_port(self.port_id)

    async def async_unlock(self, **kwargs: Any) -> None:
        """Temporarily allow port configuration controls unless auto-protected."""
        self.coordinator.unlock_port(self.port_id)
