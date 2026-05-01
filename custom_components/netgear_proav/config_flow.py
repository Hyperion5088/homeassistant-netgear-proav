"""Config flow for NETGEAR Pro AV switches."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from ipaddress import ip_network
from typing import Any
from urllib.parse import urlparse

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import NetgearProAvAuthError, NetgearProAvClient, NetgearProAvError
from .const import (
    CONF_AUTO_PROTECT_TIMEOUT,
    CONF_ENABLE_ADMIN_BOUNCE,
    CONF_ENABLE_ADMIN_CONTROLS,
    CONF_ENABLE_FAN_MODE_CONTROL,
    CONF_ENABLE_REBOOT_CONTROL,
    CONF_ENABLE_SAVE_CONFIG,
    CONF_ENABLE_POE_CONTROLS,
    CONF_ENABLE_POE_RESET,
    CONF_PORT,
    CONF_PROTECTION_MARKERS,
    CONF_SCAN_INTERVAL,
    CONF_SUBNET,
    CONF_VERIFY_SSL,
    CONF_VLANS,
    CRITICAL_NEIGHBOR_MARKERS,
    DEFAULT_AUTO_PROTECT_TIMEOUT_SECONDS,
    DEFAULT_PORT,
    DEFAULT_PROTECTION_MARKERS,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DEFAULT_SCAN_SUBNET,
    DEFAULT_VERIFY_SSL,
    DEFAULT_VLANS,
    DOMAIN,
)
from .helpers import first_detail, switch_name
from .options import auto_protect_timeout, control_option_enabled, default_control_options, option_enabled


def _parse_vlans(value: str) -> list[int]:
    """Parse a comma-separated VLAN list."""
    vlans: list[int] = []
    for raw in value.split(","):
        raw = raw.strip()
        if not raw:
            continue
        vlan = int(raw)
        if vlan < 1 or vlan > 4094:
            raise ValueError
        vlans.append(vlan)
    return vlans


def _vlans_to_string(vlans: list[int]) -> str:
    """Render VLAN IDs for the options form."""
    return ", ".join(str(vlan) for vlan in vlans)


def _parse_markers(value: str) -> list[str]:
    """Parse comma-separated protection markers."""
    markers: list[str] = []
    for raw in value.split(","):
        marker = raw.strip().lower()
        if marker and marker not in markers:
            markers.append(marker)
    return markers


def _markers_to_string(markers: list[str]) -> str:
    """Render protection markers for the options form."""
    return ", ".join(markers)


def _setup_schema(host: str | None = None) -> vol.Schema:
    """Return the setup form schema."""
    fields: dict[Any, Any] = {
        vol.Required(CONF_HOST, default=host) if host else vol.Required(CONF_HOST): str,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Optional(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
        vol.Optional(CONF_VLANS, default=DEFAULT_VLANS): str,
        **_control_schema_fields(),
    }
    return vol.Schema(fields)


def _subnet_scan_schema() -> vol.Schema:
    """Return the subnet scan form schema."""
    return vol.Schema(
        {
            vol.Required(CONF_SUBNET, default=DEFAULT_SCAN_SUBNET): str,
            vol.Required(CONF_USERNAME): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
            vol.Optional(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
            vol.Optional(CONF_VLANS, default=DEFAULT_VLANS): str,
            **_control_schema_fields(),
        }
    )


def _control_schema_fields() -> dict[Any, Any]:
    """Return control option fields shared by add and scan flows."""
    defaults = default_control_options()
    return {
        vol.Optional(
            CONF_ENABLE_ADMIN_CONTROLS,
            default=defaults[CONF_ENABLE_ADMIN_CONTROLS],
        ): bool,
        vol.Optional(
            CONF_ENABLE_POE_CONTROLS,
            default=defaults[CONF_ENABLE_POE_CONTROLS],
        ): bool,
        vol.Optional(
            CONF_ENABLE_POE_RESET,
            default=defaults[CONF_ENABLE_POE_RESET],
        ): bool,
        vol.Optional(
            CONF_ENABLE_ADMIN_BOUNCE,
            default=defaults[CONF_ENABLE_ADMIN_BOUNCE],
        ): bool,
        vol.Optional(
            CONF_ENABLE_FAN_MODE_CONTROL,
            default=defaults[CONF_ENABLE_FAN_MODE_CONTROL],
        ): bool,
        vol.Optional(
            CONF_ENABLE_SAVE_CONFIG,
            default=defaults[CONF_ENABLE_SAVE_CONFIG],
        ): bool,
        vol.Optional(
            CONF_ENABLE_REBOOT_CONTROL,
            default=defaults[CONF_ENABLE_REBOOT_CONTROL],
        ): bool,
    }


def _control_options_from_input(user_input: dict[str, Any]) -> dict[str, bool]:
    """Return control options selected during setup."""
    defaults = default_control_options()
    return {key: bool(user_input.get(key, value)) for key, value in defaults.items()}


def _host_from_ssdp(discovery_info: Any) -> str | None:
    """Return a host from SSDP discovery information."""
    location = getattr(discovery_info, "ssdp_location", None)
    if location:
        parsed = urlparse(location)
        if parsed.hostname:
            return parsed.hostname
    return getattr(discovery_info, "host", None)


class NetgearProAvConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for NETGEAR Pro AV switches."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered_host: str | None = None
        self._scan_data: dict[str, Any] | None = None
        self._scan_results: dict[str, dict[str, Any]] = {}

    def is_matching(self, other_flow: "NetgearProAvConfigFlow") -> bool:
        """Return whether this flow matches another in-progress flow."""
        return bool(self._discovered_host and self._discovered_host == other_flow._discovered_host)

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Handle the initial step."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["manual", "subnet_scan"],
        )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None):
        """Handle manual setup."""
        return await self._async_step_setup("manual", user_input)

    async def async_step_subnet_scan(self, user_input: dict[str, Any] | None = None):
        """Scan a subnet for NETGEAR Pro AV switches."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                _parse_vlans(user_input.get(CONF_VLANS, ""))
            except ValueError:
                errors["base"] = "invalid_vlan_list"
            else:
                try:
                    network = ip_network(user_input[CONF_SUBNET], strict=False)
                except ValueError:
                    errors["base"] = "invalid_subnet"
                    network = None
                if network is None:
                    pass
                elif network.num_addresses > 1024:
                    errors["base"] = "subnet_too_large"
                else:
                    self._scan_data = user_input
                    self._scan_results = await self._async_scan_subnet(user_input, network)
                    if not self._scan_results:
                        errors["base"] = "no_switches_found"
                    else:
                        return await self.async_step_subnet_pick()

        return self.async_show_form(
            step_id="subnet_scan",
            data_schema=_subnet_scan_schema(),
            errors=errors,
        )

    async def async_step_subnet_pick(self, user_input: dict[str, Any] | None = None):
        """Pick a discovered switch from a subnet scan."""
        if self._scan_data is None or not self._scan_results:
            return await self.async_step_subnet_scan()

        errors: dict[str, str] = {}
        if user_input is not None:
            selected_host = user_input[CONF_HOST]
            data = {**self._scan_data, CONF_HOST: selected_host}
            return await self._async_step_setup("subnet_pick", data)

        titles = {
            host: result["title"]
            for host, result in sorted(
                self._scan_results.items(),
                key=lambda item: item[1]["title"],
            )
        }
        return self.async_show_form(
            step_id="subnet_pick",
            data_schema=vol.Schema({vol.Required(CONF_HOST): vol.In(titles)}),
            errors=errors,
        )

    async def async_step_zeroconf(self, discovery_info: Any):
        """Handle Zeroconf/mDNS discovery."""
        self._discovered_host = discovery_info.host
        self._async_abort_entries_match({CONF_HOST: self._discovered_host})
        if self.hass.config_entries.flow.async_has_matching_flow(self):
            return self.async_abort(reason="already_in_progress")
        return await self.async_step_discovery_confirm()

    async def async_step_ssdp(self, discovery_info: Any):
        """Handle SSDP discovery."""
        self._discovered_host = _host_from_ssdp(discovery_info)
        if not self._discovered_host:
            return self.async_abort(reason="cannot_connect")
        self._async_abort_entries_match({CONF_HOST: self._discovered_host})
        if self.hass.config_entries.flow.async_has_matching_flow(self):
            return self.async_abort(reason="already_in_progress")
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(self, user_input: dict[str, Any] | None = None):
        """Confirm and authenticate a discovered switch."""
        return await self._async_step_setup("discovery_confirm", user_input, self._discovered_host)

    async def _async_step_setup(
        self,
        step_id: str,
        user_input: dict[str, Any] | None = None,
        discovered_host: str | None = None,
    ):
        """Handle setup from manual or discovered flows."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                vlans = _parse_vlans(user_input.get(CONF_VLANS, ""))
                session = async_create_clientsession(self.hass)
                client = NetgearProAvClient(
                    host=user_input[CONF_HOST],
                    username=user_input[CONF_USERNAME],
                    password=user_input[CONF_PASSWORD],
                    port=user_input[CONF_PORT],
                    verify_ssl=user_input[CONF_VERIFY_SSL],
                    session=session,
                )
                data = await client.async_device_info()
                await client.async_logout()
                info = data.get("deviceInfo", {})
                detail = first_detail(info)
                serial = (
                    detail.get("sn")
                    or info.get("serialNumber")
                    or info.get("mac")
                    or user_input[CONF_HOST]
                )
                await self.async_set_unique_id(str(serial))
                self._abort_if_unique_id_configured()
            except ValueError:
                errors["base"] = "invalid_vlan_list"
            except NetgearProAvAuthError:
                errors["base"] = "invalid_auth"
            except (NetgearProAvError, aiohttp.ClientError):
                errors["base"] = "cannot_connect"
            else:
                model = detail.get("model") or info.get("model") or "NETGEAR Pro AV"
                title = switch_name(info) or f"{model} {user_input[CONF_HOST]}"
                return self.async_create_entry(
                    title=title,
                    data={
                        CONF_HOST: user_input[CONF_HOST],
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_PORT: user_input[CONF_PORT],
                        CONF_VERIFY_SSL: user_input[CONF_VERIFY_SSL],
                        CONF_VLANS: vlans,
                    },
                    options={
                        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL_SECONDS,
                        CONF_AUTO_PROTECT_TIMEOUT: DEFAULT_AUTO_PROTECT_TIMEOUT_SECONDS,
                        **_control_options_from_input(user_input),
                    },
                )

        return self.async_show_form(
            step_id=step_id,
            data_schema=_setup_schema(discovered_host),
            errors=errors,
        )

    async def _async_scan_subnet(
        self,
        user_input: dict[str, Any],
        network: Any,
    ) -> dict[str, dict[str, Any]]:
        """Return AVUI switches found in a subnet."""
        session = async_create_clientsession(self.hass)
        semaphore = asyncio.Semaphore(24)
        results: dict[str, dict[str, Any]] = {}
        configured_hosts = {
            str(entry.data.get(CONF_HOST))
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if entry.data.get(CONF_HOST)
        }
        configured_serials = {
            str(entry.unique_id)
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if entry.unique_id
        }

        async def probe(host: str) -> None:
            if host in configured_hosts:
                return
            async with semaphore:
                client = NetgearProAvClient(
                    host=host,
                    username=user_input[CONF_USERNAME],
                    password=user_input[CONF_PASSWORD],
                    port=user_input[CONF_PORT],
                    verify_ssl=user_input[CONF_VERIFY_SSL],
                    session=session,
                    timeout=4,
                )
                try:
                    data = await client.async_device_info()
                except NetgearProAvError:
                    return
                with suppress(NetgearProAvError):
                    await client.async_logout()
                info = data.get("deviceInfo", {})
                detail = first_detail(info)
                serial = detail.get("sn") or info.get("serialNumber") or info.get("mac")
                if serial and str(serial) in configured_serials:
                    return
                model = detail.get("model") or info.get("model") or "NETGEAR Pro AV"
                name = switch_name(info) or str(host)
                title = f"{name} ({model}, {host})"
                results[host] = {
                    "serial": str(serial or host),
                    "title": title,
                }

        await asyncio.gather(*(probe(str(host)) for host in network.hosts()))
        return results

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return NetgearProAvOptionsFlow(config_entry)


class NetgearProAvOptionsFlow(config_entries.OptionsFlow):
    """Handle options for an existing NETGEAR Pro AV switch."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Manage integration options."""
        errors: dict[str, str] = {}
        data = {**self._config_entry.data, **self._config_entry.options}
        if user_input is not None:
            try:
                vlans = _parse_vlans(user_input.get(CONF_VLANS, ""))
                session = async_create_clientsession(self.hass)
                client = NetgearProAvClient(
                    host=user_input[CONF_HOST],
                    username=user_input[CONF_USERNAME],
                    password=user_input[CONF_PASSWORD],
                    port=user_input[CONF_PORT],
                    verify_ssl=user_input[CONF_VERIFY_SSL],
                    session=session,
                )
                await client.async_device_info()
                with suppress(NetgearProAvError):
                    await client.async_logout()
            except ValueError:
                errors["base"] = "invalid_vlan_list"
            except NetgearProAvAuthError:
                errors["base"] = "invalid_auth"
            except (NetgearProAvError, aiohttp.ClientError):
                errors["base"] = "cannot_connect"
            else:
                self.hass.config_entries.async_update_entry(
                    self._config_entry,
                    data={
                        CONF_HOST: user_input[CONF_HOST],
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_PORT: user_input[CONF_PORT],
                        CONF_VERIFY_SSL: user_input[CONF_VERIFY_SSL],
                        CONF_VLANS: vlans,
                    },
                )
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_SCAN_INTERVAL: user_input[CONF_SCAN_INTERVAL],
                        CONF_PROTECTION_MARKERS: _parse_markers(user_input.get(CONF_PROTECTION_MARKERS, "")),
                        CONF_AUTO_PROTECT_TIMEOUT: user_input[CONF_AUTO_PROTECT_TIMEOUT],
                        CONF_ENABLE_ADMIN_CONTROLS: user_input[CONF_ENABLE_ADMIN_CONTROLS],
                        CONF_ENABLE_POE_CONTROLS: user_input[CONF_ENABLE_POE_CONTROLS],
                        CONF_ENABLE_POE_RESET: user_input[CONF_ENABLE_POE_RESET],
                        CONF_ENABLE_ADMIN_BOUNCE: user_input[CONF_ENABLE_ADMIN_BOUNCE],
                        CONF_ENABLE_FAN_MODE_CONTROL: user_input[CONF_ENABLE_FAN_MODE_CONTROL],
                        CONF_ENABLE_SAVE_CONFIG: user_input[CONF_ENABLE_SAVE_CONFIG],
                        CONF_ENABLE_REBOOT_CONTROL: user_input[CONF_ENABLE_REBOOT_CONTROL],
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_HOST,
                    default=data.get(CONF_HOST, ""),
                ): str,
                vol.Required(
                    CONF_USERNAME,
                    default=data.get(CONF_USERNAME, ""),
                ): str,
                vol.Required(
                    CONF_PASSWORD,
                    default=data.get(CONF_PASSWORD, ""),
                ): str,
                vol.Optional(
                    CONF_PORT,
                    default=data.get(CONF_PORT, DEFAULT_PORT),
                ): int,
                vol.Optional(
                    CONF_VERIFY_SSL,
                    default=data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
                ): bool,
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SECONDS),
                ): vol.All(int, vol.Range(min=30, max=3600)),
                vol.Optional(
                    CONF_VLANS,
                    default=_vlans_to_string(data.get(CONF_VLANS, [])),
                ): str,
                vol.Optional(
                    CONF_PROTECTION_MARKERS,
                    default=_markers_to_string(
                        data[CONF_PROTECTION_MARKERS]
                        if data.get(CONF_PROTECTION_MARKERS)
                        else list(CRITICAL_NEIGHBOR_MARKERS)
                    )
                    or DEFAULT_PROTECTION_MARKERS,
                ): str,
                vol.Optional(
                    CONF_AUTO_PROTECT_TIMEOUT,
                    default=auto_protect_timeout(self._config_entry),
                ): vol.All(int, vol.Range(min=0, max=86400)),
                vol.Optional(
                    CONF_ENABLE_ADMIN_CONTROLS,
                    default=control_option_enabled(self._config_entry, CONF_ENABLE_ADMIN_CONTROLS),
                ): bool,
                vol.Optional(
                    CONF_ENABLE_POE_CONTROLS,
                    default=control_option_enabled(self._config_entry, CONF_ENABLE_POE_CONTROLS),
                ): bool,
                vol.Optional(
                    CONF_ENABLE_POE_RESET,
                    default=control_option_enabled(self._config_entry, CONF_ENABLE_POE_RESET),
                ): bool,
                vol.Optional(
                    CONF_ENABLE_ADMIN_BOUNCE,
                    default=control_option_enabled(self._config_entry, CONF_ENABLE_ADMIN_BOUNCE),
                ): bool,
                vol.Optional(
                    CONF_ENABLE_FAN_MODE_CONTROL,
                    default=control_option_enabled(self._config_entry, CONF_ENABLE_FAN_MODE_CONTROL),
                ): bool,
                vol.Optional(
                    CONF_ENABLE_SAVE_CONFIG,
                    default=option_enabled(self._config_entry, CONF_ENABLE_SAVE_CONFIG),
                ): bool,
                vol.Optional(
                    CONF_ENABLE_REBOOT_CONTROL,
                    default=option_enabled(self._config_entry, CONF_ENABLE_REBOOT_CONTROL),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
