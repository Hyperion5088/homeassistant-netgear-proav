"""NETGEAR Pro AV switch integration."""

from __future__ import annotations

import re

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later

from .api import NetgearProAvClient
from .const import (
    CONF_PORT,
    CONF_PROTECTION_MARKERS,
    CONF_SCAN_INTERVAL,
    CONF_VERIFY_SSL,
    CONF_VLANS,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
)
from .coordinator import NetgearProAvCoordinator
from .helpers import port_display_name
from .options import auto_protect_timeout

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.LOCK,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a NETGEAR Pro AV switch from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    session = async_get_clientsession(hass, verify_ssl=entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL))
    client = NetgearProAvClient(
        host=entry.data[CONF_HOST],
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        port=entry.data.get(CONF_PORT, DEFAULT_PORT),
        verify_ssl=entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
        session=session,
    )
    coordinator = NetgearProAvCoordinator(
        hass=hass,
        client=client,
        vlans=entry.data.get(CONF_VLANS, []),
        scan_interval=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SECONDS),
        protection_markers=entry.options.get(CONF_PROTECTION_MARKERS),
        auto_protect_timeout=auto_protect_timeout(entry),
        storage_key=entry.entry_id,
    )
    await coordinator.async_load_state()
    await coordinator.async_config_entry_first_refresh()
    hass.data[DOMAIN][entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_migrate_entity_ids(hass, entry)
    entry.async_on_unload(async_call_later(hass, 10, lambda _: _async_migrate_entity_ids(hass, entry)))
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a NETGEAR Pro AV switch config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
        if coordinator is not None:
            coordinator.cancel_auto_protect_timers()
            coordinator.cancel_polling_pause_timer()
            await coordinator.client.async_logout()
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _async_migrate_entity_ids(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Shorten old generated NETGEAR control entity IDs."""
    registry = er.async_get(hass)
    coordinator: NetgearProAvCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    for entity in list(registry.entities.values()):
        if entity.config_entry_id != entry.entry_id or entity.platform != DOMAIN:
            continue
        entity_id = entity.entity_id
        if (
            entity_id.startswith("button.")
            and "_set_description_from_lldp" in entity_id
            or entity_id.startswith("text.")
            and "_description_control" in entity_id
        ):
            registry.async_remove(entity_id)
            continue
        new_entity_id = entity_id
        replacements = (
            ("_configuration_port_", "_control_port_"),
            ("_admin_enabled", "_admin"),
            ("_poe_enabled", "_poe"),
            ("_control_lock", "_lock"),
            ("_diag_", "_"),
        )
        for old, new in replacements:
            new_entity_id = new_entity_id.replace(old, new)
        if re.match(r"^binary_sensor\..+_port_(?:\d+_)+\d+$", new_entity_id):
            new_entity_id = f"{new_entity_id}_1_link"
        if new_entity_id.endswith("_poe_power") and not new_entity_id.endswith("_2_poe_power"):
            new_entity_id = f"{new_entity_id[:-10]}_2_poe_power"
        if new_entity_id.endswith("_poe_reset") and not new_entity_id.endswith("_4_poe_reset"):
            new_entity_id = f"{new_entity_id[:-10]}_4_poe_reset"
        if new_entity_id.endswith("_poe") and not new_entity_id.endswith("_3_poe"):
            new_entity_id = f"{new_entity_id[:-4]}_3_poe"
        port_group_patterns = (
            (r"^(binary_sensor\..+)_port_((?:\d+_)+\d+)$", r"\1_link_state_\2"),
            (r"^(binary_sensor\..+)_port_((?:\d+_)+\d+)_1_link$", r"\1_link_state_\2"),
            (r"^(sensor\..+)_port_((?:\d+_)+\d+)_poe_power$", r"\1_poe_state_\2"),
            (r"^(sensor\..+)_port_((?:\d+_)+\d+)_2_poe_power$", r"\1_poe_state_\2"),
            (r"^(switch\..+)_control_port_((?:\d+_)+\d+)_admin$", r"\1_admin_control_\2"),
            (r"^(switch\..+)_control_port_((?:\d+_)+\d+)_poe$", r"\1_poe_switch_\2"),
            (r"^(switch\..+)_control_port_((?:\d+_)+\d+)_3_poe$", r"\1_poe_switch_\2"),
            (r"^(button\..+)_port_((?:\d+_)+\d+)_poe_reset$", r"\1_poe_reset_\2"),
            (r"^(button\..+)_port_((?:\d+_)+\d+)_4_poe_reset$", r"\1_poe_reset_\2"),
            (r"^(lock\..+)_control_port_((?:\d+_)+\d+)_lock$", r"\1_port_config_protection_\2"),
            (r"^(lock\..+)_port_lock_((?:\d+_)+\d+)$", r"\1_port_config_protection_\2"),
            (r"^(lock\..+)_port_config_protection_((?:\d+_)+\d+)$", r"\1_port_protection_\2"),
        )
        for pattern, replacement in port_group_patterns:
            migrated_entity_id = re.sub(pattern, replacement, new_entity_id)
            if migrated_entity_id != new_entity_id:
                new_entity_id = migrated_entity_id
                break
        if "_control_port_" in new_entity_id and new_entity_id.endswith("_poe"):
            prefix, port_suffix = new_entity_id.rsplit("_control_port_", 1)
            new_entity_id = f"{prefix}_poe_switch_{port_suffix.removesuffix('_poe')}"
        updates = _registry_metadata_updates(new_entity_id, entity.unique_id, coordinator)
        if new_entity_id != entity_id:
            if registry.async_get(new_entity_id) is not None:
                continue
            updates["new_entity_id"] = new_entity_id
        if updates:
            registry.async_update_entity(entity_id, **updates)


def _registry_metadata_updates(
    entity_id: str,
    unique_id: str | None,
    coordinator: NetgearProAvCoordinator | None,
) -> dict[str, object | None]:
    """Return registry metadata fixes for existing NETGEAR entities."""
    updates: dict[str, object | None] = {}
    names_by_suffix = {
        "_cpu_usage": "System CPU Usage",
        "_memory_usage": "System Memory Usage",
        "_active_ports": "Active Ports",
        "_uptime": "System Uptime",
        "_temperature": "System Temperature",
        "_poe_available": "System PoE Available",
        "_poe_threshold": "System PoE Threshold",
        "_lldp_neighbors": "System LLDP Neighbors",
        "_last_poll": "System Last Poll",
        "_polls_last_minute": "System Polls Last Minute",
        "_full_poll": "System Full Poll",
        "_port_flap_events": "System Port Flap Events",
        "_pause_polling": "System Pause Polling",
        "_port_descriptions": "System Port Descriptions",
    }
    for suffix, name in names_by_suffix.items():
        if entity_id.endswith(suffix):
            updates["original_name"] = name
            break
    normal_entity_patterns = (
        r"_active_ports$",
        r"_fan_\d+_speed$",
        r"_vlan_\d+_member_ports$",
        r"_admin_control_(?:\d+_)+\d+$",
        r"_admin_bounce_(?:\d+_)+\d+$",
        r"_poe_switch_(?:\d+_)+\d+$",
        r"_poe_reset_(?:\d+_)+\d+$",
    )
    if any(re.search(pattern, entity_id) for pattern in normal_entity_patterns):
        updates["entity_category"] = None
    elif entity_id.endswith(tuple(names_by_suffix)):
        updates["entity_category"] = EntityCategory.DIAGNOSTIC
    port_names_by_pattern = (
        (r"_link_state_((?:\d+_)+\d+)$", "{port} Link State"),
        (r"_poe_state_((?:\d+_)+\d+)$", "{port} PoE State"),
        (r"_admin_control_((?:\d+_)+\d+)$", "{port} Admin Control"),
        (r"_admin_bounce_((?:\d+_)+\d+)$", "{port} Admin Bounce"),
        (r"_poe_switch_((?:\d+_)+\d+)$", "{port} PoE Control"),
        (r"_poe_reset_((?:\d+_)+\d+)$", "{port} PoE Reset"),
        (r"_port_protection_((?:\d+_)+\d+)$", "{port} Protection"),
        (r"_port_config_protection_((?:\d+_)+\d+)$", "{port} Protection"),
    )
    for pattern, template in port_names_by_pattern:
        if match := re.search(pattern, entity_id):
            updates["original_name"] = template.format(
                port=_port_display_from_registry(match.group(1), unique_id, coordinator)
            )
            break
    return updates


def _port_display_from_registry(
    suffix: str,
    unique_id: str | None,
    coordinator: NetgearProAvCoordinator | None,
) -> str:
    """Return the best available display label for an existing port entity."""
    port_id = _port_id_from_unique_id(unique_id)
    if port_id is not None and coordinator is not None:
        port = coordinator.data.ports.get(port_id, {})
        config = coordinator.data.port_configs.get(port_id, {})
        state = coordinator.data.port_states.get(port_id, {})
        optics = coordinator.data.fiber_optics.get(port_id, {})
        return port_display_name(port_id, port, config, state, optics)
    return f"Port {_port_label_from_entity_suffix(suffix)}"


def _port_id_from_unique_id(unique_id: str | None) -> int | None:
    """Return the numeric port id encoded in a NETGEAR port entity unique id."""
    if not unique_id:
        return None
    if match := re.search(r"_port_(\d+)(?:_|$)", unique_id):
        return int(match.group(1))
    return None


def _port_label_from_entity_suffix(suffix: str) -> str:
    """Return a display port label from an entity ID suffix."""
    return suffix.replace("_", "/")
