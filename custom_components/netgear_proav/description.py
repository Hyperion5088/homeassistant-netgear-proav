"""Port description helpers for NETGEAR Pro AV switches."""

from __future__ import annotations

import re
from typing import Any

from .helpers import is_lag_port, lag_member_ports, port_identity, port_label

MAX_DESCRIPTION_LENGTH = 64
DESCRIPTION_DELIMITER = " | "
_MAC_ADDRESS_RE = re.compile(r"^(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}$", re.IGNORECASE)


def remote_host(neighbor: dict[str, Any]) -> str:
    """Return a short remote host name from LLDP data."""
    for key in ("friendlyName", "friendly_name", "hostName", "hostIpAddress"):
        value = str(neighbor.get(key) or "").strip()
        if value and value != "?":
            return value.split(".", 1)[0]
    return ""


def remote_port(neighbor: dict[str, Any]) -> str:
    """Return a useful remote interface name from LLDP data."""
    for key in ("remotePortId", "remote_port", "remotePortName", "remote_port_name"):
        value = str(neighbor.get(key) or "").strip()
        if value and value != "?" and not _is_bad_remote_port(value):
            return value
    return ""


def description_from_neighbor(neighbor: dict[str, Any]) -> str:
    """Return a compact switch-port description from one LLDP neighbor."""
    host = remote_host(neighbor)
    port = remote_port(neighbor)
    description = f"{host}{DESCRIPTION_DELIMITER}{port}" if port else host
    return description[:MAX_DESCRIPTION_LENGTH]


def description_change_summary(suggestion: dict[str, Any]) -> dict[str, Any]:
    """Return compact description change details for attributes."""
    return {
        "port": suggestion.get("port"),
        "current": suggestion.get("current_description"),
        "proposed": suggestion.get("proposed_description"),
        "change": suggestion.get("description_change"),
    }


def description_suggestion(
    port_id: int,
    port: dict[str, Any],
    config: dict[str, Any],
    lag_configs: dict[int, dict[str, Any]],
    neighbors: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return one port's safe LLDP description suggestion."""
    label = port_label(port or config, port_id)
    current = str(port.get("description") or config.get("description") or "")
    base: dict[str, Any] = {
        **port_identity(port, config, port_id),
        "current_description": current,
        "delimiter": DESCRIPTION_DELIMITER,
        "max_length": MAX_DESCRIPTION_LENGTH,
        "bypasses_port_config_protection": True,
    }
    if is_lag_port(port or config, port_id, lag_configs):
        return {**base, "can_apply": False, "skip_reason": "lag_port"}
    if _port_is_lag_member(port_id, label, lag_configs):
        return {**base, "can_apply": False, "skip_reason": "lag_member"}

    useful = [neighbor for neighbor in neighbors if remote_host(neighbor)]
    candidates = [
        {"remote_host": remote_host(neighbor), "remote_interface": remote_port(neighbor)}
        for neighbor in useful
    ]
    if not useful:
        return {**base, "can_apply": False, "skip_reason": "no_lldp_neighbor"}
    if len(useful) > 1:
        return {
            **base,
            "can_apply": False,
            "skip_reason": "multiple_lldp_neighbors",
            "candidate_neighbors": candidates,
        }

    neighbor = useful[0]
    proposed = description_from_neighbor(neighbor)
    return {
        **base,
        "can_apply": bool(proposed) and proposed != current,
        "skip_reason": None if proposed and proposed != current else "already_matches",
        "proposed_description": proposed,
        "description_change": f"{current} > {proposed}",
        "remote_host": remote_host(neighbor),
        "remote_interface": remote_port(neighbor),
        "candidate_neighbors": candidates,
    }


def _port_is_lag_member(port_id: int, port_label_value: str, lag_configs: dict[int, dict[str, Any]]) -> bool:
    """Return whether a physical port is listed as a LAG member."""
    port_refs = {str(port_id), port_label_value}
    for lag_config in lag_configs.values():
        if any(str(member) in port_refs for member in lag_member_ports(lag_config)):
            return True
    return False


def _is_bad_remote_port(value: str) -> bool:
    """Return whether a remote port value should be ignored for descriptions."""
    normalized = value.strip().strip('"')
    if not normalized:
        return True
    if _MAC_ADDRESS_RE.match(normalized):
        return True
    if normalized.lower() in {"null", "none", "unknown", "not received", "not available"}:
        return True
    return False
