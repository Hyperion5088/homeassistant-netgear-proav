"""Sensors for NETGEAR Pro AV switches."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NetgearProAvCoordinator, NetgearProAvData
from .description import (
    description_change_summary,
    description_suggestion as build_description_suggestion,
)
from .helpers import (
    device_info as build_device_info,
    firmware,
    first_detail,
    first_not_none,
    link_up,
    mac_address,
    model,
    percent,
    port_identity,
    port_label,
    port_sort_key,
    power_watts,
    serial,
    should_expose_port,
    truthy_enabled,
    uptime_seconds,
)


def _percent(value: Any) -> float | None:
    """Convert NETGEAR percent strings into numbers."""
    return percent(value)


def _uptime_seconds(value: Any) -> int | None:
    """Parse NETGEAR uptime strings such as '21 Days 20 Hrs 29 Mins 16 Secs'."""
    return uptime_seconds(value)


def _uptime_display(value: Any) -> str | None:
    """Format switch uptime as days plus clock time."""
    seconds = uptime_seconds(value)
    if seconds is None:
        return None
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    day_label = "day" if days == 1 else "days"
    return f"{days} {day_label} {hours:02}:{minutes:02}:{seconds:02}"


def _switch_details(info: dict[str, Any]) -> dict[str, Any]:
    """Return the first physical-unit detail block."""
    return first_detail(info)


def _serial(info: dict[str, Any], fallback: str) -> str:
    """Return a stable switch serial identifier."""
    return serial(info, fallback)


def _model(info: dict[str, Any]) -> str | None:
    """Return the switch model."""
    return model(info)


def _firmware(info: dict[str, Any]) -> str | None:
    """Return the switch firmware version."""
    return firmware(info)


def _mac(info: dict[str, Any]) -> str | None:
    """Return the switch MAC address."""
    return mac_address(info)


def _average_percent(rows: Any) -> float | None:
    """Average percent usage values from NETGEAR row lists."""
    if not isinstance(rows, list):
        return None
    values = [_percent(row.get("usage")) for row in rows if isinstance(row, dict)]
    values = [value for value in values if value is not None]
    if not values:
        return None
    return round(sum(values) / len(values), 1)


def _temperature(info: dict[str, Any]) -> int | float | None:
    """Return the highest reported unit temperature."""
    temps: list[int | float] = []
    sensors = info.get("sensor")
    if isinstance(sensors, list):
        for sensor in sensors:
            if not isinstance(sensor, dict):
                continue
            for detail in sensor.get("details", []) or []:
                if isinstance(detail, dict) and isinstance(detail.get("temp"), int | float):
                    temps.append(detail["temp"])

    legacy_sensors = info.get("temperatureSensors")
    if isinstance(legacy_sensors, list):
        temps.extend(
            sensor["sensorTemp"]
            for sensor in legacy_sensors
            if isinstance(sensor, dict) and isinstance(sensor.get("sensorTemp"), int | float)
        )
    return max(temps, default=None)


def _fan_rows(info: dict[str, Any]) -> list[dict[str, Any]]:
    """Return flattened fan detail rows from device_info."""
    rows: list[dict[str, Any]] = []
    for unit in info.get("fan", []) or []:
        if not isinstance(unit, dict):
            continue
        unit_id = unit.get("unit")
        for detail in unit.get("details", []) or []:
            if isinstance(detail, dict):
                rows.append({**detail, "unit": unit_id})
    return rows


def _temperature_rows(info: dict[str, Any]) -> list[dict[str, Any]]:
    """Return flattened temperature sensor detail rows from device_info."""
    rows: list[dict[str, Any]] = []
    for unit in info.get("sensor", []) or []:
        if not isinstance(unit, dict):
            continue
        unit_id = unit.get("unit")
        for detail in unit.get("details", []) or []:
            if isinstance(detail, dict):
                rows.append({**detail, "unit": unit_id})
    return rows


def _fan_state(value: Any) -> str | None:
    """Return a readable fan state."""
    states = {
        0: "normal",
        1: "warning",
        2: "critical",
    }
    if isinstance(value, int):
        return states.get(value, str(value))
    return str(value) if value not in (None, "") else None


def _temperature_state(value: Any) -> str | None:
    """Return a readable temperature sensor state."""
    states = {
        0: "normal",
        1: "warning",
        2: "critical",
    }
    if isinstance(value, int):
        return states.get(value, str(value))
    return str(value) if value not in (None, "") else None


def _active_ports(data: NetgearProAvData) -> int:
    """Count physical ports reporting link up."""
    return sum(1 for port in data.port_states.values() if link_up(port.get("linkState")))


def _milliwatts_to_watts(value: Any) -> float | None:
    """Convert NETGEAR PoE milliwatt values to watts."""
    return power_watts(value)


def _switch_poe_capable(data: NetgearProAvData) -> bool:
    """Return whether the switch reports PoE support."""
    if data.device_info.get("poe") is True:
        return True
    for key in ("consumedPower", "totalPowerAvailable", "thresholdPower"):
        value = power_watts(data.poe_info.get(key))
        if value is not None and value > 0:
            return True
    return False


def _first_not_none(*values: Any) -> Any:
    """Return the first non-null value."""
    return first_not_none(*values)


def _vlan_member_ports(vlan: dict[str, Any], *keys: str) -> list[Any]:
    """Return unique physical ports participating in VLAN membership keys."""
    ports = set()
    for key in keys:
        for item in vlan.get(key, []) or []:
            if isinstance(item, dict):
                ports.add(item.get("port") or item.get("portid") or item.get("portNum"))
            else:
                ports.add(item)
    return sorted(
        {port for port in ports if port is not None},
        key=lambda port: (str(port).count("/") == 0, str(port)),
    )


def _member_ports(vlan: dict[str, Any]) -> list[Any]:
    """Return all unique VLAN member ports."""
    return _vlan_member_ports(vlan, "assignedUntagPort", "assignedtagPort", "portMembers", "pvidMembers")


def _member_count(vlan: dict[str, Any]) -> int:
    """Count unique physical ports participating in a VLAN."""
    return len(_member_ports(vlan))


def _poe_capable(config: dict[str, Any]) -> bool:
    """Return whether a port reports PoE data."""
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


def _port_is_fiber(port: dict[str, Any], config: dict[str, Any], state: dict[str, Any]) -> bool:
    """Return whether a port appears to be an optical/fiber interface."""
    for value in (
        port.get("mediaType"),
        config.get("mediaType"),
        state.get("mediaType"),
        port.get("portType"),
        config.get("portType"),
        state.get("portType"),
        port.get("physicalMode"),
        config.get("physicalMode"),
        state.get("physicalMode"),
    ):
        text = str(value or "").strip().lower()
        if any(marker in text for marker in ("fiber", "fibre", "sfp", "sfp+", "optical")):
            return True
        if "copper" in text or "1000base-t" in text or "10gbase-t" in text:
            return False
    return False


def _poe_delivering(config: dict[str, Any]) -> bool:
    """Return whether a port appears to be delivering PoE power."""
    value = config.get("powerUsage") or config.get("currentPower")
    return isinstance(value, int | float) and value > 0


def _port_power_watts(config: dict[str, Any]) -> float | None:
    """Return per-port PoE power in watts."""
    value = first_not_none(config.get("powerUsage"), config.get("currentPower"))
    if not isinstance(value, int | float):
        return None
    return round(value / 1000, 2)


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


def _fiber_value(row: dict[str, Any], *keys: str) -> Any:
    """Return the first populated fiber optic value."""
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _config_save_needed(data: NetgearProAvData) -> bool | None:
    """Return whether the switch reports unsaved running config changes."""
    for source in (data.device_info, data.image_info):
        for key in ("configSaveNeeded", "saveNeeded", "unsavedChanges", "runningConfigChanged"):
            value = source.get(key)
            if value is not None:
                return truthy_enabled(value)
    return None


def _power_supply_status(data: NetgearProAvData) -> str | None:
    """Return any power supply status exposed by the switch."""
    for key in ("powerSupplyStatus", "psuStatus", "powerStatus"):
        value = data.device_info.get(key)
        if value not in (None, ""):
            return str(value)
    return None


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
    """Return de-duplicated real LLDP neighbors.

    Some NETGEAR AVUI responses include OUI/vendor rows or learned downstream
    devices with no chassis, IP, or remote port. Those rows make raw counts
    misleading, so only count rows with a stable neighbor identity.
    """
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for neighbor in neighbors:
        key = _neighbor_key(neighbor)
        if key is None or key in seen:
            continue
        seen.add(key)
        unique.append(neighbor)
    return unique


@dataclass(frozen=True, kw_only=True)
class NetgearSummaryDescription(SensorEntityDescription):
    """Description for switch-level sensors."""

    value_fn: Callable[[NetgearProAvData], Any]


SUMMARY_DESCRIPTIONS: tuple[NetgearSummaryDescription, ...] = (
    NetgearSummaryDescription(
        key="cpu_usage",
        translation_key="cpu_usage",
        name="CPU Usage",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _first_not_none(
            _average_percent(data.device_info.get("cpu")),
            _percent(data.device_info.get("cpuUsage")),
        ),
    ),
    NetgearSummaryDescription(
        key="memory_usage",
        translation_key="memory_usage",
        name="Memory Usage",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _first_not_none(
            _average_percent(data.device_info.get("memory")),
            _percent(data.device_info.get("memoryUsage")),
        ),
    ),
    NetgearSummaryDescription(
        key="active_ports",
        translation_key="active_ports",
        name="Switch Active Ports",
        value_fn=_active_ports,
    ),
    NetgearSummaryDescription(
        key="uptime",
        translation_key="uptime",
        name="Uptime",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _uptime_display(
            _switch_details(data.device_info).get("upTime") or data.device_info.get("upTime")
        ),
    ),
    NetgearSummaryDescription(
        key="temperature",
        translation_key="temperature",
        name="Switch Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement="°C",
        value_fn=lambda data: _temperature(data.device_info),
    ),
    NetgearSummaryDescription(
        key="poe_consumed",
        translation_key="poe_consumed",
        name="Switch PoE Consumed",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda data: _milliwatts_to_watts(data.poe_info.get("consumedPower")),
    ),
    NetgearSummaryDescription(
        key="poe_available",
        translation_key="poe_available",
        name="PoE Available",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _milliwatts_to_watts(data.poe_info.get("totalPowerAvailable")),
    ),
    NetgearSummaryDescription(
        key="poe_threshold",
        translation_key="poe_threshold",
        name="PoE Threshold",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _milliwatts_to_watts(data.poe_info.get("thresholdPower")),
    ),
    NetgearSummaryDescription(
        key="lldp_neighbors",
        translation_key="lldp_neighbors",
        name="LLDP Neighbors",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: len(_unique_neighbors(data.neighbors)),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up NETGEAR Pro AV sensors."""
    coordinator: NetgearProAvCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        NetgearSummarySensor(coordinator, entry, description)
        for description in SUMMARY_DESCRIPTIONS
    ]
    entities.append(NetgearLastPollSensor(coordinator, entry))
    entities.append(NetgearPollRateSensor(coordinator, entry))
    entities.append(NetgearPortFlapSensor(coordinator, entry))
    entities.append(NetgearPortDescriptionsSensor(coordinator, entry))

    if _switch_poe_capable(coordinator.data):
        for port_id, config in coordinator.data.port_configs.items():
            if not should_expose_port(
                port_id,
                coordinator.data.ports.get(port_id) or config,
                coordinator.data.lag_configs,
            ):
                continue
            if not _port_is_fiber(
                coordinator.data.ports.get(port_id, {}),
                config,
                coordinator.data.port_states.get(port_id, {}),
            ) and _poe_capable(config):
                entities.append(NetgearPortPoeSensor(coordinator, entry, port_id))

    for fan in _fan_rows(coordinator.data.device_info):
        if fan.get("id") is not None:
            entities.append(NetgearFanSpeedSensor(coordinator, entry, int(fan["id"])))

    for vlan_id in coordinator.data.vlans:
        entities.append(NetgearVlanSensor(coordinator, entry, vlan_id))

    async_add_entities(entities)


class NetgearBaseEntity(CoordinatorEntity[NetgearProAvCoordinator]):
    """Base entity for NETGEAR Pro AV sensors."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: NetgearProAvCoordinator, entry: ConfigEntry) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.entry = entry

    @property
    def device_info(self) -> DeviceInfo:
        """Return switch device info."""
        info = self.coordinator.data.device_info
        return build_device_info(info, self.entry.title, self.entry.entry_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return useful switch attributes."""
        info = self.coordinator.data.device_info
        return {
            "model": _model(info),
            "firmware": _firmware(info),
            "firmware_image": self.coordinator.data.image_info.get("runningImage"),
            "active_image": self.coordinator.data.image_info.get("activeImage"),
            "backup_image": self.coordinator.data.image_info.get("backupImage"),
            "config_save_needed": _config_save_needed(self.coordinator.data),
            "stacking": self.coordinator.data.stacking_info,
            "power_supply_status": _power_supply_status(self.coordinator.data),
            "temperature_sensors": [
                {
                    "id": row.get("id"),
                    "description": row.get("desc"),
                    "temperature": row.get("temp"),
                    "max_temperature": row.get("maxTemp"),
                    "state": _temperature_state(row.get("state")),
                }
                for row in _temperature_rows(info)
            ],
            "serial_number": _serial(info, self.entry.entry_id),
            "mac_address": _mac(info),
            "avui_version": info.get("avuiVer"),
        }


class NetgearSummarySensor(NetgearBaseEntity, SensorEntity):
    """Switch-level status sensor."""

    entity_description: NetgearSummaryDescription

    def __init__(
        self,
        coordinator: NetgearProAvCoordinator,
        entry: ConfigEntry,
        description: NetgearSummaryDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self.entity_description = description
        serial = _serial(coordinator.data.device_info, entry.entry_id)
        self._attr_unique_id = f"{serial}_{description.key}"

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.coordinator.data)


class NetgearLastPollSensor(NetgearBaseEntity, SensorEntity):
    """Switch-level last successful poll diagnostic sensor."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_name = "Last Poll"

    def __init__(self, coordinator: NetgearProAvCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        switch_serial = _serial(coordinator.data.device_info, entry.entry_id)
        self._attr_unique_id = f"{switch_serial}_last_poll"

    @property
    def native_value(self) -> Any:
        """Return the last successful API poll timestamp."""
        return self.coordinator.last_poll_time

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return polling state context."""
        return {
            **super().extra_state_attributes,
            "polling_paused": self.coordinator.polling_paused,
            "polling_pause_remaining_seconds": self.coordinator.polling_pause_remaining_seconds,
            "polling_pause_until": self.coordinator.polling_pause_until,
            "update_interval_seconds": int(self.coordinator.update_interval.total_seconds())
            if self.coordinator.update_interval
            else None,
        }


class NetgearPollRateSensor(NetgearBaseEntity, SensorEntity):
    """Switch-level recent poll rate diagnostic sensor."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_name = "Polls Last Minute"

    def __init__(self, coordinator: NetgearProAvCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        switch_serial = _serial(coordinator.data.device_info, entry.entry_id)
        self._attr_unique_id = f"{switch_serial}_polls_last_minute"

    @property
    def native_value(self) -> int:
        """Return successful API polls recorded in the last minute."""
        return self.coordinator.polls_last_minute

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return recent poll rate context."""
        return {
            **super().extra_state_attributes,
            "average_poll_interval_seconds": self.coordinator.average_poll_interval_seconds,
            "average_polls_per_minute": self.coordinator.average_polls_per_minute,
            "last_poll": self.coordinator.last_poll_time,
            "polling_paused": self.coordinator.polling_paused,
            "polling_pause_remaining_seconds": self.coordinator.polling_pause_remaining_seconds,
        }


class NetgearPortDescriptionsSensor(NetgearBaseEntity, SensorEntity):
    """Switch-level port description diagnostic sensor."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Port Descriptions"

    def __init__(self, coordinator: NetgearProAvCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        switch_serial = _serial(coordinator.data.device_info, entry.entry_id)
        self._attr_unique_id = f"{switch_serial}_port_descriptions"

    @property
    def native_value(self) -> int:
        """Return the number of pending description suggestions."""
        return len([row for row in self._description_suggestions() if row.get("can_apply")])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return current descriptions and suggested changes."""
        suggestions = self._description_suggestions()
        pending = [row for row in suggestions if row.get("can_apply")]
        descriptions = self._description_rows()
        return {
            **super().extra_state_attributes,
            "described_port_count": len([row for row in descriptions if row.get("description")]),
            "port_count": len(descriptions),
            "descriptions": descriptions,
            "pending_change_count": len(pending),
            "pending_changes": [description_change_summary(row) for row in pending],
            "skipped": [
                {
                    "port": row.get("port"),
                    "reason": row.get("skip_reason"),
                }
                for row in suggestions
                if row.get("skip_reason") not in ("already_matches", "no_lldp_neighbor")
            ],
        }

    def _description_rows(self) -> list[dict[str, Any]]:
        """Return current descriptions for exposed physical ports."""
        rows: list[dict[str, Any]] = []
        for port_id in self._port_ids():
            port = self.coordinator.data.ports.get(port_id, {})
            config = self.coordinator.data.port_configs.get(port_id, {})
            rows.append(
                {
                    "port": port_label(port or config, port_id),
                    "description": port.get("description") or config.get("description") or "",
                }
            )
        return rows

    def _description_suggestions(self) -> list[dict[str, Any]]:
        """Return LLDP-derived description suggestions for exposed ports."""
        suggestions: list[dict[str, Any]] = []
        for port_id in self._port_ids():
            port = self.coordinator.data.ports.get(port_id, {})
            config = self.coordinator.data.port_configs.get(port_id, {})
            suggestions.append(
                build_description_suggestion(
                    port_id,
                    port,
                    config,
                    self.coordinator.data.lag_configs,
                    _unique_neighbors(self.coordinator.data.neighbors_by_port.get(port_id, [])),
                )
            )
        return suggestions

    def _port_ids(self) -> list[int]:
        """Return exposed port IDs in stable order."""
        port_ids = sorted(
            set(self.coordinator.data.ports) | set(self.coordinator.data.port_configs),
            key=port_sort_key,
        )
        return [
            port_id
            for port_id in port_ids
            if should_expose_port(
                port_id,
                self.coordinator.data.ports.get(port_id) or self.coordinator.data.port_configs.get(port_id, {}),
                self.coordinator.data.lag_configs,
            )
        ]


class NetgearPortFlapSensor(NetgearBaseEntity, SensorEntity):
    """Switch-level port flap diagnostic sensor."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: NetgearProAvCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_name = "Port Flap Events"
        switch_serial = _serial(coordinator.data.device_info, entry.entry_id)
        self._attr_unique_id = f"{switch_serial}_port_flap_events"

    @property
    def native_value(self) -> int:
        """Return the number of currently flapping ports."""
        return self.coordinator.flap_summary()["flapping_port_count"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return recent flap details."""
        return self.coordinator.flap_summary()


class NetgearPortPoeSensor(NetgearBaseEntity, SensorEntity):
    """Per-port PoE power sensor."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(
        self,
        coordinator: NetgearProAvCoordinator,
        entry: ConfigEntry,
        port_id: int,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self.port_id = port_id
        port = coordinator.data.ports.get(port_id, {})
        config = coordinator.data.port_configs.get(port_id, {})
        self._attr_name = f"PoE State {port_label(port or config, port_id)}"
        switch_serial = _serial(coordinator.data.device_info, entry.entry_id)
        self._attr_unique_id = f"{switch_serial}_port_{port_id}_poe_power"

    @property
    def native_value(self) -> float | None:
        """Return current PoE power in watts."""
        config = self.coordinator.data.port_configs.get(self.port_id, {})
        return _port_power_watts(config)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return PoE context for this port."""
        port = self.coordinator.data.ports.get(self.port_id, {})
        config = self.coordinator.data.port_configs.get(self.port_id, {})
        return {
            **port_identity(port, config, self.port_id),
            "description": port.get("description") or config.get("description"),
            "link_up": link_up(first_not_none(port.get("linkState"), config.get("linkState"))),
            "admin_enabled": truthy_enabled(port.get("adminState") or config.get("adminState")),
            "poe_status": _poe_status(config),
            "poe_is_valid": config.get("poeIsValid"),
            "poe_class": first_not_none(config.get("classification"), config.get("poeClass")),
            "poe_delivering": _poe_delivering(config),
            "power_limit_mode": config.get("powerLimitMode"),
            "power_limit": _port_power_watts({"powerUsage": config.get("powerLimit")}),
            "port_type": config.get("portType"),
            "raw_power_usage": config.get("powerUsage"),
            "raw_status": first_not_none(config.get("status"), config.get("poeStatus")),
        }


class NetgearFanSpeedSensor(NetgearBaseEntity, SensorEntity):
    """Fan speed sensor."""

    _attr_native_unit_of_measurement = "rpm"

    def __init__(
        self,
        coordinator: NetgearProAvCoordinator,
        entry: ConfigEntry,
        fan_id: int,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self.fan_id = fan_id
        fan = self._fan_row
        self._attr_name = f"Fan {fan.get('desc') or fan_id} Speed"
        switch_serial = _serial(coordinator.data.device_info, entry.entry_id)
        self._attr_unique_id = f"{switch_serial}_fan_{fan_id}_speed"

    @property
    def _fan_row(self) -> dict[str, Any]:
        """Return the current fan row."""
        for fan in _fan_rows(self.coordinator.data.device_info):
            if fan.get("id") == self.fan_id:
                return fan
        return {}

    @property
    def native_value(self) -> int | None:
        """Return fan speed in RPM."""
        speed = self._fan_row.get("speed")
        return speed if isinstance(speed, int) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return fan context."""
        fan = self._fan_row
        return {
            "fan_id": self.fan_id,
            "unit": fan.get("unit"),
            "description": fan.get("desc"),
            "duty_level": fan.get("dutyLevel"),
            "state_code": fan.get("state"),
            "state": _fan_state(fan.get("state")),
        }


class NetgearVlanSensor(NetgearBaseEntity, SensorEntity):
    """VLAN membership summary sensor."""

    def __init__(self, coordinator: NetgearProAvCoordinator, entry: ConfigEntry, vlan_id: int) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self.vlan_id = vlan_id
        self._attr_name = f"VLAN {vlan_id} Member Ports"
        serial = _serial(coordinator.data.device_info, entry.entry_id)
        self._attr_unique_id = f"{serial}_vlan_{vlan_id}_member_ports"

    @property
    def native_value(self) -> int:
        """Return VLAN member port count."""
        return _member_count(self.coordinator.data.vlans.get(self.vlan_id, {}))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return VLAN membership context."""
        vlan = self.coordinator.data.vlans.get(self.vlan_id, {})
        tagged_ports = _vlan_member_ports(vlan, "assignedtagPort")
        untagged_ports = _vlan_member_ports(vlan, "assignedUntagPort", "pvidMembers")
        member_ports = _member_ports(vlan)
        attrs: dict[str, Any] = {
            "vlan_id": self.vlan_id,
            "member_ports": member_ports,
        }
        if tagged_ports:
            attrs["tagged_ports"] = tagged_ports
        if untagged_ports and untagged_ports != member_ports:
            attrs["untagged_ports"] = untagged_ports
        return attrs
