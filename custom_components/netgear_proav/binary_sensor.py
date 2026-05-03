"""Binary sensors for NETGEAR Pro AV switches."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NetgearProAvCoordinator
from .description import (
    description_suggestion as build_description_suggestion,
)
from .helpers import device_info as build_device_info
from .helpers import (
    first_not_none,
    lag_member_ports,
    link_up,
    port_display_name,
    port_identity,
    port_sort_key,
    serial,
    should_expose_port,
    truthy_enabled,
)


def _number(value: Any) -> float | int | None:
    """Convert numeric strings into numbers."""
    if isinstance(value, int | float):
        return value
    if value in (None, ""):
        return None
    try:
        number = float(str(value).strip().rstrip("%"))
    except ValueError:
        return None
    if number.is_integer():
        return int(number)
    return number


def _milliwatts_to_watts(value: Any) -> float | None:
    """Convert milliwatts to watts."""
    if not isinstance(value, int | float):
        return None
    return round(value / 1000, 2)


def _speed_mbps(value: Any) -> int | None:
    """Extract a speed in Mbps from NETGEAR speed text."""
    if value in (None, ""):
        return None
    if isinstance(value, int | float):
        return int(value)
    text = str(value).strip().lower().replace("_", " ")
    match = re.search(r"(\d+(?:\.\d+)?)\s*(g|gb|gbps|gig|gigabit)", text)
    if match:
        return int(float(match.group(1)) * 1000)
    match = re.search(r"(\d+(?:\.\d+)?)\s*(m|mb|mbps)", text)
    if match:
        return int(float(match.group(1)))
    match = re.search(r"\b(10|100|1000|2500|5000|10000|25000|40000|100000)\b", text)
    if match:
        return int(match.group(1))
    return None


def _supported_speeds(*values: Any) -> list[int]:
    """Return sorted supported speed values in Mbps."""
    speeds: set[int] = set()
    for value in values:
        if isinstance(value, list | tuple | set):
            speeds.update(speed for item in value if (speed := _speed_mbps(item)) is not None)
            continue
        if value in (None, ""):
            continue
        for part in re.split(r"[,;/|]+", str(value)):
            speed = _speed_mbps(part)
            if speed is not None:
                speeds.add(speed)
    return sorted(speeds)


def _speed_display(speed_mbps: int | float | None) -> str | None:
    """Return a readable speed from a Mbps value."""
    if speed_mbps is None:
        return None
    if speed_mbps >= 1000 and speed_mbps % 1000 == 0:
        return f"{int(speed_mbps / 1000)} Gbps"
    if speed_mbps >= 1000:
        return f"{speed_mbps / 1000:g} Gbps"
    return f"{int(speed_mbps)} Mbps"


def _poe_status(config: dict[str, Any]) -> str | None:
    """Return a readable PoE status."""
    value = first_not_none(config.get("status"), config.get("poeStatus"), config.get("poeIsValid"))
    states = {
        -1: "not_supported",
        0: "disabled",
        1: "searching",
        2: "delivering",
        3: "fault",
        4: "test",
        5: "deny",
        6: "overload",
        7: "low_power",
    }
    if isinstance(value, int):
        return states.get(value, str(value))
    return str(value) if value not in (None, "") else None


def _port_poe_capable(config: dict[str, Any]) -> bool:
    """Return whether a port reports useful PoE support."""
    poe_status = first_not_none(config.get("poeIsValid"), config.get("status"), config.get("poeStatus"))
    if poe_status in (-1, "-1"):
        return False
    if any(key in config for key in ("enable", "powerLimitMode", "powerLimit", "classification")):
        return True
    if poe_status not in (None, 0, "0", False, ""):
        return True
    for key in ("powerUsage", "currentPower"):
        value = _milliwatts_to_watts(config.get(key))
        if value is not None and value > 0:
            return True
    return False


def _fiber_value(row: dict[str, Any], *keys: str) -> Any:
    """Return the first populated fiber optic value."""
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _port_is_fiber(
    port: dict[str, Any],
    config: dict[str, Any],
    state: dict[str, Any],
    *fiber_rows: dict[str, Any],
) -> bool:
    """Return whether a port appears to be an optical/fiber interface."""
    media_values = (
        port.get("mediaType"),
        config.get("mediaType"),
        state.get("mediaType"),
        port.get("portType"),
        config.get("portType"),
        state.get("portType"),
        port.get("physicalMode"),
        config.get("physicalMode"),
        state.get("physicalMode"),
    )
    for value in media_values:
        text = str(value or "").strip().lower()
        if any(marker in text for marker in ("fiber", "fibre", "sfp", "sfp+", "optical")):
            return True
        if "copper" in text or "1000base-t" in text or "10gbase-t" in text:
            return False

    populated_fiber_keys = (
        "temperature",
        "voltage",
        "current",
        "inputPower",
        "outputPower",
        "vendorName",
        "vendor_name",
        "partNumber",
        "vendor_pn",
        "serialNumber",
        "vendor_sn",
        "possibleSpeedDetected",
        "diagInfo",
    )
    return any(
        any(row.get(key) not in (None, "", [], "-") for key in populated_fiber_keys)
        for row in fiber_rows
        if row
    )


def _neighbor_attributes(neighbors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return dashboard-friendly LLDP neighbor attributes."""
    return [
        {
            "friendly_name": neighbor.get("friendlyName") or neighbor.get("friendly_name"),
            "host_name": neighbor.get("hostName"),
            "host_ip": neighbor.get("hostIpAddress"),
            "host_mac": neighbor.get("hostMacAddress"),
            "source": neighbor.get("source"),
            "local_port": neighbor.get("portName") or neighbor.get("port"),
            "remote_port": neighbor.get("remotePortId"),
            "remote_chassis_id": neighbor.get("remoteChassisId"),
            "system_description": neighbor.get("systemDescription") or neighbor.get("remoteSystemDescription"),
        }
        for neighbor in neighbors
    ]


def _neighbor_key(neighbor: dict[str, Any]) -> tuple[str, str] | None:
    """Return a stable key for a useful LLDP neighbor row."""
    if str(neighbor.get("source") or "").strip().lower() != "lldp":
        return None
    chassis = str(neighbor.get("remoteChassisId") or "").strip()
    if chassis:
        return ("chassis", chassis.lower())
    host_ip = str(neighbor.get("hostIpAddress") or "").strip()
    if host_ip:
        return ("ip", host_ip)
    remote_port = str(neighbor.get("remotePortId") or "").strip()
    if remote_port:
        return ("remote_port", remote_port.lower())
    return None


def _unique_neighbors(neighbors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return de-duplicated real LLDP neighbors."""
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for neighbor in neighbors:
        key = _neighbor_key(neighbor)
        if key is None or key in seen:
            continue
        seen.add(key)
        unique.append(neighbor)
    return unique


def _neighbor_name(neighbor: dict[str, Any]) -> str | None:
    """Return the best display name for an LLDP neighbor."""
    for key in (
        "friendlyName",
        "friendly_name",
        "hostName",
        "hostIpAddress",
        "systemDescription",
        "remoteSystemDescription",
    ):
        value = str(neighbor.get(key) or "").strip()
        if value:
            return value
    return None


def _neighbor_display(neighbors: list[dict[str, Any]]) -> str | None:
    """Return a comma-separated display string for unique LLDP neighbors."""
    names: list[str] = []
    seen: set[str] = set()
    for neighbor in _unique_neighbors(neighbors):
        name = _neighbor_name(neighbor)
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        names.append(name)
    return ", ".join(names) if names else None


def _description_suggestion(
    port_id: int,
    port: dict[str, Any],
    config: dict[str, Any],
    lag_configs: dict[int, dict[str, Any]],
    raw_neighbors: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return an LLDP-derived description suggestion without creating controls."""
    return build_description_suggestion(
        port_id,
        port,
        config,
        lag_configs,
        _unique_neighbors(raw_neighbors),
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up NETGEAR Pro AV binary sensors."""
    coordinator: NetgearProAvCoordinator = hass.data[DOMAIN][entry.entry_id]
    port_ids = [
        port_id
        for port_id in sorted(
            set(coordinator.data.ports) | set(coordinator.data.port_states) | set(coordinator.data.port_configs),
            key=port_sort_key,
        )
        if should_expose_port(
            port_id,
            coordinator.data.ports.get(port_id) or coordinator.data.port_configs.get(port_id, {}),
            coordinator.data.lag_configs,
        )
    ]
    entities: list[BinarySensorEntity] = []
    for port_id in port_ids:
        entities.append(NetgearPortLinkSensor(coordinator, entry, port_id))
    async_add_entities(entities)


class NetgearPortLinkSensor(CoordinatorEntity[NetgearProAvCoordinator], BinarySensorEntity):
    """Single entity representing a physical switch port."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(
        self,
        coordinator: NetgearProAvCoordinator,
        entry: ConfigEntry,
        port_id: int,
    ) -> None:
        """Initialize the port entity."""
        super().__init__(coordinator)
        self.entry = entry
        self.port_id = port_id
        port = coordinator.data.ports.get(port_id, {})
        config = coordinator.data.port_configs.get(port_id, {})
        state = coordinator.data.port_states.get(port_id, {})
        optics = coordinator.data.fiber_optics.get(port_id, {})
        self._attr_name = f"{port_display_name(port_id, port, config, state, optics)} Link State"
        switch_serial = serial(coordinator.data.device_info, entry.entry_id)
        self._attr_unique_id = f"{switch_serial}_port_{port_id}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return switch device info."""
        return build_device_info(self.coordinator.data.device_info, self.entry.title, self.entry.entry_id)

    @property
    def is_on(self) -> bool | None:
        """Return whether the port link is up."""
        port = self.coordinator.data.ports.get(self.port_id, {})
        return link_up(port.get("linkState"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return port context useful for dashboards."""
        port = self.coordinator.data.ports.get(self.port_id, {})
        config = self.coordinator.data.port_configs.get(self.port_id, {})
        state = self.coordinator.data.port_states.get(self.port_id, {})
        lag_config = self.coordinator.data.lag_configs.get(self.port_id, {})
        stats_in = self.coordinator.data.port_statistics_in.get(self.port_id, {})
        stats_out = self.coordinator.data.port_statistics_out.get(self.port_id, {})
        optics = self.coordinator.data.fiber_optics.get(self.port_id, {})
        fiber_diag = self.coordinator.data.fiber_diag.get(self.port_id, {})
        fiber_eeprom = self.coordinator.data.fiber_eeprom.get(self.port_id, {})
        stp = self.coordinator.data.stp_ports.get(self.port_id, {})
        multicast = self.coordinator.data.multicast_groups_by_port.get(
            self.port_id,
            {"group_count": 0, "vlans": [], "groups": [], "subscribers": [], "rows": []},
        )
        raw_neighbors = self.coordinator.data.neighbors_by_port.get(self.port_id, [])
        neighbors = _unique_neighbors(raw_neighbors)
        is_fiber = _port_is_fiber(port, config, state, optics, fiber_diag, fiber_eeprom)
        supported_speeds = _supported_speeds(
            port.get("physicalMode"),
            port.get("physicalStatus"),
            port.get("speed"),
            config.get("linkSpeed"),
            config.get("speed"),
            optics.get("possibleSpeedDetected"),
            optics.get("nominalBitRate"),
        )
        flap_attributes = self.coordinator.port_flap_attributes(self.port_id)
        attrs: dict[str, Any] = {
            **port_identity(port, config, self.port_id),
            "description": port.get("description") or config.get("description"),
            "profile": port.get("profileName") or config.get("profileName"),
            "admin_enabled": truthy_enabled(port.get("adminState") or config.get("adminState")),
            "physical_status": port.get("physicalStatus") or config.get("linkSpeed"),
            "speed": first_not_none(port.get("physicalStatus"), port.get("speed"), config.get("linkSpeed")),
            "supported_speeds": [_speed_display(speed) for speed in supported_speeds] or None,
            "max_speed": _speed_display(max(supported_speeds)) if supported_speeds else None,
            "duplex": first_not_none(port.get("duplexMode"), config.get("duplexMode")),
            "media_type": port.get("mediaType"),
            "stp_state": port.get("stpFwdState"),
            "stp": {
                "mode": stp.get("stpMode") or port.get("stpMode"),
                "edge_port_status": stp.get("edgePortStatus"),
                "forward_state": stp.get("forwardState") or port.get("stpFwdState"),
                "role": stp.get("role"),
                "protocol": self.coordinator.data.stp_config.get("protocolSpecification"),
                "root_bridge": self.coordinator.data.stp_config.get("designatedRoot"),
                "root_port": self.coordinator.data.stp_config.get("rootPort"),
                "topology_changes": self.coordinator.data.stp_config.get("topChanges"),
            },
            "multicast": multicast,
            "pvid": config.get("pvid") or state.get("pvid"),
            "vlans": config.get("vlan") or state.get("vlan") or state.get("tagged"),
            "tagged_vlans": state.get("tagged"),
            "lag_group_id": lag_config.get("groupId"),
            "lag_members": lag_member_ports(lag_config),
            "traffic": {
                "rx_bitrate_mbps": _number(
                    first_not_none(stats_in.get("inBitRate"), config.get("bandwidthDownload"))
                ),
                "tx_bitrate_mbps": _number(
                    first_not_none(stats_out.get("outBitRate"), stats_out.get("inBitRate"), config.get("bandwidthUpload"))
                ),
                "rx_errors": _number(stats_in.get("rxError")),
                "tx_errors": _number(first_not_none(stats_out.get("txError"), stats_out.get("rxError"))),
                "rx_drops": _number(stats_in.get("inDropPkts")),
                "tx_drops": _number(first_not_none(stats_out.get("outDropPkts"), stats_out.get("inDropPkts"))),
                "rx_octets": _number(stats_in.get("inOctets")),
                "tx_octets": _number(first_not_none(stats_out.get("outOctets"), stats_out.get("inOctets"))),
                "rx_packets": _number(stats_in.get("inTotalPkts")),
                "tx_packets": _number(first_not_none(stats_out.get("outTotalPkts"), stats_out.get("inTotalPkts"))),
                "rx_utilization": _number(stats_in.get("rxBwUtil%")),
                "tx_utilization": _number(first_not_none(stats_out.get("txBwUtil%"), stats_out.get("rxBwUtil%"))),
            },
            "lldp": {
                "neighbor": _neighbor_display(raw_neighbors),
                "neighbor_count": len(neighbors),
                "raw_neighbor_rows": len(raw_neighbors),
                "neighbors": _neighbor_attributes(neighbors),
            },
            "description_suggestion": _description_suggestion(
                self.port_id,
                port,
                config,
                self.coordinator.data.lag_configs,
                raw_neighbors,
            ),
            "flapping": flap_attributes["flapping"],
            "recent_link_change_count": flap_attributes["recent_link_change_count"],
            "recent_link_change_times": flap_attributes["recent_link_change_times"],
            "flap_window_seconds": flap_attributes["flap_window_seconds"],
            "flap_threshold": flap_attributes["flap_threshold"],
            "link_flaps": flap_attributes,
        }

        if not is_fiber and _port_poe_capable(config):
            attrs["poe"] = {
                "power_w": _milliwatts_to_watts(first_not_none(config.get("powerUsage"), config.get("currentPower"))),
                "status": _poe_status(config),
                "class": first_not_none(config.get("classification"), config.get("poeClass")),
                "is_valid": config.get("poeIsValid"),
                "power_limit_w": _milliwatts_to_watts(config.get("powerLimit")),
                "power_limit_mode": config.get("powerLimitMode"),
            }

        if is_fiber:
            attrs["fiber"] = {
                "temperature": _number(optics.get("temperature")),
                "voltage": _number(optics.get("voltage")),
                "current": _number(optics.get("current")),
                "rx_power": _number(optics.get("inputPower")),
                "tx_power": _number(optics.get("outputPower")),
                "fault_status": optics.get("faultStatus"),
                "tx_fault": optics.get("txFault"),
                "loss_of_signal": optics.get("los"),
                "vendor": _fiber_value(optics | fiber_eeprom, "vendorName", "vendor_name"),
                "part_number": _fiber_value(optics | fiber_eeprom, "partNumber", "vendor_pn"),
                "serial_number": _fiber_value(optics | fiber_eeprom, "serialNumber", "vendor_sn"),
                "supported_speeds": optics.get("possibleSpeedDetected"),
                "supported": optics.get("supported"),
                "compliance": optics.get("compliance"),
                "diagnostics": fiber_diag.get("diagInfo"),
            }

        return attrs
