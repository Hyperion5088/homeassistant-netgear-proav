"""Option helpers for NETGEAR Pro AV switches."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_AUTO_PROTECT_TIMEOUT,
    CONF_ENABLE_ADMIN_BOUNCE,
    CONF_ENABLE_ADMIN_CONTROLS,
    CONF_ENABLE_FAN_MODE_CONTROL,
    CONF_ENABLE_REBOOT_CONTROL,
    CONF_ENABLE_SAVE_CONFIG,
    CONF_ENABLE_POE_CONTROLS,
    CONF_ENABLE_POE_RESET,
    DEFAULT_AUTO_PROTECT_TIMEOUT_SECONDS,
    DEFAULT_ENABLE_ADMIN_BOUNCE,
    DEFAULT_ENABLE_ADMIN_CONTROLS,
    DEFAULT_ENABLE_FAN_MODE_CONTROL,
    DEFAULT_ENABLE_REBOOT_CONTROL,
    DEFAULT_ENABLE_SAVE_CONFIG,
    DEFAULT_ENABLE_POE_CONTROLS,
    DEFAULT_ENABLE_POE_RESET,
)


CONTROL_DEFAULTS = {
    CONF_ENABLE_ADMIN_CONTROLS: DEFAULT_ENABLE_ADMIN_CONTROLS,
    CONF_ENABLE_POE_CONTROLS: DEFAULT_ENABLE_POE_CONTROLS,
    CONF_ENABLE_POE_RESET: DEFAULT_ENABLE_POE_RESET,
    CONF_ENABLE_ADMIN_BOUNCE: DEFAULT_ENABLE_ADMIN_BOUNCE,
    CONF_ENABLE_FAN_MODE_CONTROL: DEFAULT_ENABLE_FAN_MODE_CONTROL,
    CONF_ENABLE_SAVE_CONFIG: DEFAULT_ENABLE_SAVE_CONFIG,
    CONF_ENABLE_REBOOT_CONTROL: DEFAULT_ENABLE_REBOOT_CONTROL,
}

CONTROL_OPTION_KEYS = tuple(CONTROL_DEFAULTS)
PORT_CONTROL_OPTION_KEYS = (
    CONF_ENABLE_ADMIN_CONTROLS,
    CONF_ENABLE_POE_CONTROLS,
    CONF_ENABLE_POE_RESET,
    CONF_ENABLE_ADMIN_BOUNCE,
)


def default_control_options() -> dict[str, bool]:
    """Return conservative defaults for newly added switches."""
    return dict(CONTROL_DEFAULTS)


def control_option_enabled(entry: ConfigEntry, key: str) -> bool:
    """Return whether a control family is enabled for an entry.

    Existing entries created before these options are preserved as enabled so
    controls do not disappear on upgrade.
    """
    if key in entry.options:
        return bool(entry.options[key])
    return True


def option_enabled(entry: ConfigEntry, key: str, default: bool = False) -> bool:
    """Return whether an optional feature is enabled."""
    return bool(entry.options.get(key, default))


def any_port_controls_enabled(entry: ConfigEntry) -> bool:
    """Return whether any protected per-port controls are enabled."""
    return any(control_option_enabled(entry, key) for key in PORT_CONTROL_OPTION_KEYS)


def auto_protect_timeout(entry: ConfigEntry) -> int:
    """Return the manual unlock timeout in seconds."""
    try:
        return int(entry.options.get(CONF_AUTO_PROTECT_TIMEOUT, DEFAULT_AUTO_PROTECT_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_AUTO_PROTECT_TIMEOUT_SECONDS
