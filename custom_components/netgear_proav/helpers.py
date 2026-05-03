"""Shared helpers for NETGEAR Pro AV entities."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN


def first_detail(info: dict[str, Any]) -> dict[str, Any]:
    """Return the first physical-unit detail block."""
    details = info.get("details")
    if isinstance(details, list) and details and isinstance(details[0], dict):
        return details[0]
    return {}


def serial(info: dict[str, Any], fallback: str) -> str:
    """Return a stable switch serial identifier."""
    return str(first_detail(info).get("sn") or info.get("serialNumber") or fallback)


def model(info: dict[str, Any]) -> str | None:
    """Return the switch model."""
    return first_detail(info).get("model") or info.get("model")


def switch_name(info: dict[str, Any]) -> str | None:
    """Return the configured switch name."""
    name = info.get("name")
    return str(name).strip() if name not in (None, "") else None


def firmware(info: dict[str, Any]) -> str | None:
    """Return the switch firmware version."""
    return first_detail(info).get("fwVer") or info.get("swVer")


def mac_address(info: dict[str, Any]) -> str | None:
    """Return the switch MAC address."""
    return info.get("mac") or info.get("macAddr")


def device_info(info: dict[str, Any], entry_title: str, fallback: str) -> DeviceInfo:
    """Build the Home Assistant device registry entry."""
    switch_serial = serial(info, fallback)
    mac = mac_address(info)
    return DeviceInfo(
        identifiers={(DOMAIN, switch_serial)},
        name=entry_title,
        manufacturer="NETGEAR",
        model=model(info),
        sw_version=firmware(info),
        serial_number=switch_serial,
        connections={("mac", mac)} if mac else set(),
    )


def percent(value: Any) -> float | None:
    """Convert NETGEAR percent strings into numbers."""
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip().rstrip("%")
    try:
        return float(text)
    except ValueError:
        return None


def uptime_seconds(value: Any) -> int | None:
    """Parse NETGEAR uptime strings such as '21 Days 20 Hrs 29 Mins 16 Secs'."""
    if not value:
        return None
    total = 0
    units = {
        "day": 86400,
        "days": 86400,
        "hr": 3600,
        "hrs": 3600,
        "hour": 3600,
        "hours": 3600,
        "min": 60,
        "mins": 60,
        "sec": 1,
        "secs": 1,
    }
    for amount, unit in re.findall(r"(\d+)\s*([A-Za-z]+)", str(value)):
        total += int(amount) * units.get(unit.lower(), 0)
    return total or None


def first_not_none(*values: Any) -> Any:
    """Return the first non-null value."""
    for value in values:
        if value is not None:
            return value
    return None


def truthy_enabled(value: Any) -> bool | None:
    """Convert NETGEAR enabled/link values into booleans."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value == 1
    normalized = str(value).strip().lower()
    if normalized in {"1", "up", "on", "true", "enable", "enabled", "delivering"}:
        return True
    if normalized in {"0", "down", "off", "false", "disable", "disabled", "searching", "fault"}:
        return False
    return None


def link_up(value: Any) -> bool | None:
    """Convert NETGEAR link state values into booleans.

    AVUI port link state uses 0 for up and 1 for down, unlike admin-style
    enabled values where 1 usually means enabled.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return not value
    if isinstance(value, int | float):
        return value == 0
    normalized = str(value).strip().lower()
    if normalized in {"0", "up", "on", "true", "connected"}:
        return True
    if normalized in {"1", "down", "off", "false", "disconnected"}:
        return False
    return None


def power_watts(value: Any) -> float | None:
    """Return a PoE power value in watts.

    The AVUI Swagger describes switch-level PoE values as watts, while some
    port-level examples use milliwatts. Treat large values as milliwatts and
    normal-sized values as watts so both shapes remain readable.
    """
    if not isinstance(value, int | float):
        return None
    if abs(value) > 1000:
        return round(value / 1000, 2)
    return round(float(value), 2)


def port_label(port: dict[str, Any], port_id: int) -> str:
    """Return a readable port label."""
    return str(port.get("portStr") or port.get("portName") or port.get("port") or port_id)


def port_connector_prefix(*rows: dict[str, Any]) -> str:
    """Return the display prefix for a copper or optical switch port."""
    for row in rows:
        for key in (
            "mediaType",
            "portType",
            "physicalMode",
            "type",
            "connector",
            "possibleSpeedDetected",
        ):
            text = str(row.get(key) or "").strip().lower()
            if not text:
                continue
            if "qsfp" in text:
                return "QSFP"
            if "sfp+" in text or ("10g" in text and "sfp" in text):
                return "SFP+"
            if "sfp" in text or "fiber" in text or "fibre" in text or "optical" in text:
                return "SFP"
    return "Port"


def port_display_name(port_id: int, *rows: dict[str, Any]) -> str:
    """Return a grouped Home Assistant display name for a physical port."""
    source = next((row for row in rows if row), {})
    return f"{port_connector_prefix(*rows)} {port_label(source, port_id)}"


def port_identity(port: dict[str, Any], config: dict[str, Any] | None, port_id: int) -> dict[str, Any]:
    """Return stack-aware identity fields for a physical port."""
    config = config or {}
    label = port_label(port or config, port_id)
    label_parts = str(label).split("/")
    is_stack_style_label = len(label_parts) >= 3
    slot = first_not_none(port.get("slot"), config.get("slot"))
    if slot is None and is_stack_style_label:
        slot = label_parts[1]
    unit = first_not_none(port.get("unit"), config.get("unit"), label_parts[0] if is_stack_style_label else None)
    control_target = {
        "port_number": port_id,
        "unit": unit,
        "port_name": first_not_none(port.get("portName"), port.get("portStr"), config.get("portName"), config.get("portStr")),
    }
    if slot is not None:
        control_target["slot"] = slot
    return {
        "port_number": port_id,
        "port": label,
        "unit": unit,
        "slot": slot if is_stack_style_label else None,
        "port_name": first_not_none(port.get("portName"), port.get("portStr"), config.get("portName"), config.get("portStr")),
        "control_target": control_target,
    }


def is_lag_port(port: dict[str, Any], port_id: int, lag_configs: dict[int, dict[str, Any]] | None = None) -> bool:
    """Return whether a row represents a LAG pseudo-port."""
    label = port_label(port, port_id).lower()
    return port_id in (lag_configs or {}) or label.startswith(("lag", "ch")) or "lag" in label


def lag_member_ports(lag_config: dict[str, Any]) -> list[Any]:
    """Return configured member ports for a LAG config row."""
    members = lag_config.get("members")
    if isinstance(members, list):
        return [member for member in members if member not in (None, "", [])]
    member_names = lag_config.get("memberName")
    if isinstance(member_names, list):
        return [member for member in member_names if member not in (None, "", [])]
    return []


def should_expose_port(
    port_id: int,
    port: dict[str, Any],
    lag_configs: dict[int, dict[str, Any]],
) -> bool:
    """Return whether a port should be represented by an entity."""
    if not is_lag_port(port, port_id, lag_configs):
        return True
    return bool(lag_member_ports(lag_configs.get(port_id, {})))


def port_sort_key(port_id: int | str) -> int:
    """Sort port ids numerically where possible."""
    try:
        return int(port_id)
    except (TypeError, ValueError):
        return 99999
