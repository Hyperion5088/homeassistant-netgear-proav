"""Button entities for NETGEAR Pro AV switches."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_ENABLE_ADMIN_BOUNCE,
    CONF_ENABLE_PORT_DESCRIPTION_CONTROL,
    CONF_ENABLE_POE_RESET,
    CONF_ENABLE_REBOOT_CONTROL,
    CONF_ENABLE_SAVE_CONFIG,
    DOMAIN,
)
from .coordinator import NetgearProAvCoordinator
from .description import (
    description_change_summary,
    description_suggestion as build_description_suggestion,
    remote_port,
    remote_host,
)
from .helpers import device_info as build_device_info
from .helpers import first_not_none, power_watts
from .helpers import (
    port_display_name,
    port_identity,
    port_sort_key,
    serial,
    should_expose_port,
)
from .options import control_option_enabled, option_enabled


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up NETGEAR Pro AV button entities."""
    coordinator: NetgearProAvCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[ButtonEntity] = [NetgearFullPollButton(coordinator, entry)]
    if option_enabled(entry, CONF_ENABLE_SAVE_CONFIG):
        entities.append(NetgearSaveConfigButton(coordinator, entry))
    if option_enabled(entry, CONF_ENABLE_PORT_DESCRIPTION_CONTROL):
        entities.append(NetgearUpdateDescriptionsButton(coordinator, entry))
        entities.append(NetgearSetSelectedDescriptionButton(coordinator, entry))
    if option_enabled(entry, CONF_ENABLE_REBOOT_CONTROL):
        entities.append(NetgearRebootButton(coordinator, entry))
    port_ids = sorted(set(coordinator.data.ports) | set(coordinator.data.port_configs), key=port_sort_key)
    for port_id in port_ids:
        if not should_expose_port(
            port_id,
            coordinator.data.ports.get(port_id) or coordinator.data.port_configs.get(port_id, {}),
            coordinator.data.lag_configs,
        ):
            continue
        if control_option_enabled(entry, CONF_ENABLE_ADMIN_BOUNCE):
            entities.append(NetgearAdminBounceButton(coordinator, entry, port_id))
        config = coordinator.data.port_configs.get(port_id, {})
        if control_option_enabled(entry, CONF_ENABLE_POE_RESET) and _poe_capable(config):
            entities.append(NetgearPoeResetButton(coordinator, entry, port_id))
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


def _unique_lldp_neighbors(neighbors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return useful de-duplicated LLDP neighbors."""
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for neighbor in neighbors:
        if str(neighbor.get("source") or "").strip().lower() != "lldp":
            continue
        host = remote_host(neighbor)
        neighbor_port = remote_port(neighbor)
        if not host:
            continue
        key = (host.lower(), neighbor_port.lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(neighbor)
    return unique


def _description_suggestion(
    coordinator: NetgearProAvCoordinator,
    port_id: int,
) -> dict[str, Any]:
    """Return one port's safe LLDP description suggestion."""
    port = coordinator.data.ports.get(port_id, {})
    config = coordinator.data.port_configs.get(port_id, {})
    neighbors = _unique_lldp_neighbors(coordinator.data.neighbors_by_port.get(port_id, []))
    return build_description_suggestion(
        port_id,
        port,
        config,
        coordinator.data.lag_configs,
        neighbors,
    )


def _description_suggestions(coordinator: NetgearProAvCoordinator) -> list[dict[str, Any]]:
    """Return safe description suggestions for every exposed port."""
    port_ids = sorted(set(coordinator.data.ports) | set(coordinator.data.port_configs), key=port_sort_key)
    suggestions: list[dict[str, Any]] = []
    for port_id in port_ids:
        if not should_expose_port(
            port_id,
            coordinator.data.ports.get(port_id) or coordinator.data.port_configs.get(port_id, {}),
            coordinator.data.lag_configs,
        ):
            continue
        suggestions.append(_description_suggestion(coordinator, port_id))
    return suggestions


class NetgearFullPollButton(CoordinatorEntity[NetgearProAvCoordinator], ButtonEntity):
    """Request a full switch poll."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_name = "System Full Poll"

    def __init__(self, coordinator: NetgearProAvCoordinator, entry: ConfigEntry) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self.entry = entry
        switch_serial = serial(coordinator.data.device_info, entry.entry_id)
        self._attr_unique_id = f"{switch_serial}_full_poll"

    @property
    def device_info(self) -> DeviceInfo:
        """Return switch device info."""
        return build_device_info(self.coordinator.data.device_info, self.entry.title, self.entry.entry_id)

    async def async_press(self) -> None:
        """Trigger a full switch poll."""
        await self.coordinator.async_full_poll()


class NetgearSaveConfigButton(CoordinatorEntity[NetgearProAvCoordinator], ButtonEntity):
    """Save the running switch configuration."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_name = "Save Config"

    def __init__(self, coordinator: NetgearProAvCoordinator, entry: ConfigEntry) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self.entry = entry
        switch_serial = serial(coordinator.data.device_info, entry.entry_id)
        self._attr_unique_id = f"{switch_serial}_save_config"

    @property
    def device_info(self) -> DeviceInfo:
        """Return switch device info."""
        return build_device_info(self.coordinator.data.device_info, self.entry.title, self.entry.entry_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return save context."""
        return {
            "config_save_needed": self.coordinator.data.device_info.get("configSaveNeeded"),
        }

    async def async_press(self) -> None:
        """Save the running configuration."""
        await self.coordinator.async_save_config()


class NetgearUpdateDescriptionsButton(CoordinatorEntity[NetgearProAvCoordinator], ButtonEntity):
    """Apply all clear LLDP-derived port description suggestions."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_name = "Update Port Descriptions"

    def __init__(self, coordinator: NetgearProAvCoordinator, entry: ConfigEntry) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self.entry = entry
        switch_serial = serial(coordinator.data.device_info, entry.entry_id)
        self._attr_unique_id = f"{switch_serial}_update_port_descriptions"

    @property
    def device_info(self) -> DeviceInfo:
        """Return switch device info."""
        return build_device_info(self.coordinator.data.device_info, self.entry.title, self.entry.entry_id)

    @property
    def available(self) -> bool:
        """Return whether any description changes can be applied."""
        return super().available and self.coordinator.data is not None and bool(self.coordinator.data.neighbors)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return pending and skipped description suggestions."""
        suggestions = _description_suggestions(self.coordinator)
        pending = [suggestion for suggestion in suggestions if suggestion["can_apply"]]
        skipped = [suggestion for suggestion in suggestions if not suggestion["can_apply"]]
        return {
            "pending_change_count": len(pending),
            "pending_changes": [description_change_summary(suggestion) for suggestion in pending],
            "skipped_count": len(skipped),
            "skipped": [
                {
                    "port": suggestion.get("port"),
                    "reason": suggestion.get("skip_reason"),
                }
                for suggestion in skipped
                if suggestion.get("skip_reason") not in ("already_matches", "no_lldp_neighbor")
            ],
            "bypasses_port_config_protection": True,
        }

    async def async_press(self) -> None:
        """Apply every clear description suggestion."""
        updates = {
            int(suggestion["port_number"]): str(suggestion["proposed_description"])
            for suggestion in _description_suggestions(self.coordinator)
            if suggestion["can_apply"]
        }
        if updates:
            await self.coordinator.async_set_port_descriptions(updates)


class NetgearSetSelectedDescriptionButton(CoordinatorEntity[NetgearProAvCoordinator], ButtonEntity):
    """Apply the manual description input to the selected port."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_name = "Set Port Description"

    def __init__(self, coordinator: NetgearProAvCoordinator, entry: ConfigEntry) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self.entry = entry
        switch_serial = serial(coordinator.data.device_info, entry.entry_id)
        self._attr_unique_id = f"{switch_serial}_set_port_description"

    @property
    def device_info(self) -> DeviceInfo:
        """Return switch device info."""
        return build_device_info(self.coordinator.data.device_info, self.entry.title, self.entry.entry_id)

    @property
    def available(self) -> bool:
        """Return whether a selected manual description can be applied."""
        context = self.coordinator.selected_description_context()
        return (
            super().available
            and bool(context.get("target_selected"))
            and bool(context.get("proposed"))
            and context.get("current") != context.get("proposed")
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return selected description context."""
        return self.coordinator.selected_description_context()

    async def async_press(self) -> None:
        """Apply the pending manual description."""
        await self.coordinator.async_set_selected_port_description()


class NetgearRebootButton(CoordinatorEntity[NetgearProAvCoordinator], ButtonEntity):
    """Reboot the switch after two-step confirmation."""

    _attr_entity_registry_enabled_default = False
    _attr_has_entity_name = True
    _attr_name = "Reboot"

    def __init__(self, coordinator: NetgearProAvCoordinator, entry: ConfigEntry) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self.entry = entry
        switch_serial = serial(coordinator.data.device_info, entry.entry_id)
        self._attr_unique_id = f"{switch_serial}_reboot"

    @property
    def device_info(self) -> DeviceInfo:
        """Return switch device info."""
        return build_device_info(self.coordinator.data.device_info, self.entry.title, self.entry.entry_id)

    @property
    def available(self) -> bool:
        """Only allow reboot once explicitly armed."""
        return super().available and self.coordinator.reboot_armed()

    @property
    def extra_state_attributes(self) -> dict[str, str | bool]:
        """Return reboot context."""
        return {
            "confirmation": self.coordinator.reboot_confirmation,
            "armed": self.coordinator.reboot_armed(),
            "save_config_before_reboot": True,
        }

    async def async_press(self) -> None:
        """Reboot the switch if armed."""
        await self.coordinator.async_reboot()


class NetgearPoeResetButton(CoordinatorEntity[NetgearProAvCoordinator], ButtonEntity):
    """Guarded PoE reset button."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: NetgearProAvCoordinator, entry: ConfigEntry, port_id: int) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self.entry = entry
        self.port_id = port_id
        port = coordinator.data.ports.get(port_id, {})
        config = coordinator.data.port_configs.get(port_id, {})
        state = coordinator.data.port_states.get(port_id, {})
        self._attr_name = f"{port_display_name(port_id, port, config, state)} PoE Reset"
        switch_serial = serial(coordinator.data.device_info, entry.entry_id)
        self._attr_unique_id = f"{switch_serial}_port_{port_id}_poe_reset"

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
        """Return reset context."""
        port = self.coordinator.data.ports.get(self.port_id, {})
        config = self.coordinator.data.port_configs.get(self.port_id, {})
        reason = self.coordinator.port_protection_reason(self.port_id)
        return {
            **port_identity(port, config, self.port_id),
            "auto_protected": reason is not None,
            "protected_reason": reason,
            "control_unlocked": self.coordinator.is_port_unlocked(self.port_id),
        }

    async def async_press(self) -> None:
        """Reset PoE if the port is unlocked and not auto-protected."""
        await self.coordinator.async_reset_poe(self.port_id)


class NetgearAdminBounceButton(CoordinatorEntity[NetgearProAvCoordinator], ButtonEntity):
    """Guarded admin down/up bounce button."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: NetgearProAvCoordinator, entry: ConfigEntry, port_id: int) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self.entry = entry
        self.port_id = port_id
        port = coordinator.data.ports.get(port_id, {})
        config = coordinator.data.port_configs.get(port_id, {})
        state = coordinator.data.port_states.get(port_id, {})
        self._attr_name = f"{port_display_name(port_id, port, config, state)} Admin Bounce"
        switch_serial = serial(coordinator.data.device_info, entry.entry_id)
        self._attr_unique_id = f"{switch_serial}_port_{port_id}_admin_bounce"

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
        """Return bounce context."""
        port = self.coordinator.data.ports.get(self.port_id, {})
        config = self.coordinator.data.port_configs.get(self.port_id, {})
        reason = self.coordinator.port_protection_reason(self.port_id)
        return {
            **port_identity(port, config, self.port_id),
            "auto_protected": reason is not None,
            "protected_reason": reason,
            "control_unlocked": self.coordinator.is_port_unlocked(self.port_id),
            "bounce_seconds": 5,
        }

    async def async_press(self) -> None:
        """Bounce admin state if the port is unlocked and not auto-protected."""
        await self.coordinator.async_bounce_port_admin(self.port_id)
