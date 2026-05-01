"""Diagnostics support for NETGEAR Pro AV switches."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

TO_REDACT = {
    "host",
    "hostMacAddress",
    "mac",
    "macAddress",
    "mac_address",
    "password",
    "serial",
    "serialNumber",
    "serial_number",
    "session",
    "session_token",
    "sn",
    "username",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    return async_redact_data(
        {
            "entry": {
                "data": entry.data,
                "options": entry.options,
                "title": entry.title,
            },
            "data": {
                "device_info": coordinator.data.device_info,
                "port_count": len(coordinator.data.ports),
                "port_config_count": len(coordinator.data.port_configs),
                "neighbor_count": len(coordinator.data.neighbors),
                "vlan_count": len(coordinator.data.vlans),
                "poe_info": coordinator.data.poe_info,
            },
        },
        TO_REDACT,
    )
