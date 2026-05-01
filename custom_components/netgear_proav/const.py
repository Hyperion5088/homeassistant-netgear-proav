"""Constants for the NETGEAR Pro AV integration."""

from datetime import timedelta

DOMAIN = "netgear_proav"

CONF_PORT = "port"
CONF_VERIFY_SSL = "verify_ssl"
CONF_VLANS = "vlans"
CONF_SUBNET = "subnet"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_PROTECTION_MARKERS = "protection_markers"
CONF_ENABLE_ADMIN_CONTROLS = "enable_admin_controls"
CONF_ENABLE_POE_CONTROLS = "enable_poe_controls"
CONF_ENABLE_POE_RESET = "enable_poe_reset"
CONF_ENABLE_ADMIN_BOUNCE = "enable_admin_bounce"
CONF_ENABLE_FAN_MODE_CONTROL = "enable_fan_mode_control"
CONF_ENABLE_SAVE_CONFIG = "enable_save_config"
CONF_ENABLE_PORT_DESCRIPTION_CONTROL = "enable_port_description_control"
CONF_ENABLE_REBOOT_CONTROL = "enable_reboot_control"
CONF_AUTO_PROTECT_TIMEOUT = "auto_protect_timeout"

DEFAULT_PORT = 443
DEFAULT_VERIFY_SSL = False
DEFAULT_SCAN_INTERVAL = timedelta(seconds=60)
DEFAULT_SCAN_INTERVAL_SECONDS = 60
DEFAULT_VLANS = "1"
DEFAULT_SCAN_SUBNET = ""
DEFAULT_PROTECTION_MARKERS = ""
DEFAULT_ENABLE_ADMIN_CONTROLS = True
DEFAULT_ENABLE_POE_CONTROLS = True
DEFAULT_ENABLE_POE_RESET = True
DEFAULT_ENABLE_ADMIN_BOUNCE = True
DEFAULT_ENABLE_FAN_MODE_CONTROL = True
DEFAULT_ENABLE_SAVE_CONFIG = True
DEFAULT_ENABLE_PORT_DESCRIPTION_CONTROL = False
DEFAULT_ENABLE_REBOOT_CONTROL = True
DEFAULT_AUTO_PROTECT_TIMEOUT_SECONDS = 300

CRITICAL_NEIGHBOR_MARKERS = (
    "access point",
    "esxi",
    "firewall",
    "gateway",
    "host",
    "hypervisor",
    "m4250",
    "m4300",
    "m4350",
    "m4500",
    "nas",
    "netgear",
    "proxmox",
    "router",
    "server",
    "switch",
    "swi",
    "synology",
    "truenas",
    "uap",
    "ubiquiti",
    "unifi",
)
