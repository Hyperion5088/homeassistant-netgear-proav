"""Small async client for NETGEAR Pro AV switch REST endpoints."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
from typing import Any

import aiohttp


class NetgearProAvError(Exception):
    """Base error for NETGEAR Pro AV API failures."""


class NetgearProAvAuthError(NetgearProAvError):
    """Raised when authentication fails."""


class NetgearProAvResponseError(NetgearProAvError):
    """Raised when the switch returns a malformed response."""


@dataclass(slots=True)
class NetgearProAvClient:
    """Read-only client for the REST agent exposed on Pro AV switches."""

    host: str
    username: str
    password: str
    session: aiohttp.ClientSession
    port: int = 443
    verify_ssl: bool = False
    timeout: int = 15
    session_token: str | None = None
    session_token_created: float | None = None
    session_token_ttl: int = 23 * 60 * 60

    @property
    def base_url(self) -> str:
        """Return the REST agent base URL."""
        if self.port == 443:
            return f"https://{self.host}/api/v1"
        if self.port == 80:
            return f"http://{self.host}/api/v1"
        return f"https://{self.host}:{self.port}/api/v1"

    @property
    def legacy_base_url(self) -> str:
        """Return the legacy REST agent base URL."""
        return f"https://{self.host}:8443/api/v1"

    async def async_login(self) -> None:
        """Create a documented AVUI API session."""
        data = await self._async_request(
            "post",
            "login",
            json={"user": {"name": self.username, "password": self.password}},
            authenticated=False,
        )
        token = data.get("user", {}).get("session")
        if not token:
            raise NetgearProAvAuthError("login did not return a session token")
        self.session_token = token
        self.session_token_created = time.monotonic()

    async def async_logout(self) -> None:
        """Clear the AVUI API session."""
        if not self.session_token:
            return
        try:
            await self._async_request("post", "logout")
        finally:
            self.session_token = None
            self.session_token_created = None

    async def async_get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET a JSON endpoint."""
        if not self.session_token or self._session_expired():
            await self.async_login()
        try:
            return await self._async_request("get", path, params=params)
        except NetgearProAvAuthError:
            self.session_token = None
            self.session_token_created = None
            await self.async_login()
            return await self._async_request("get", path, params=params)

    def _session_expired(self) -> bool:
        """Return whether the cached AVUI session should be renewed."""
        return (
            self.session_token_created is None
            or time.monotonic() - self.session_token_created >= self.session_token_ttl
        )

    async def _async_request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        """Make an API request and validate the NETGEAR response envelope."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {"Accept": "application/json"}
        if authenticated:
            if not self.session_token:
                raise NetgearProAvAuthError("missing session token")
            headers["session"] = self.session_token
        try:
            async with asyncio.timeout(self.timeout):
                response = await self.session.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    ssl=self.verify_ssl,
                    headers=headers,
                )
                if response.status in (401, 403):
                    raise NetgearProAvAuthError("authentication failed")
                response.raise_for_status()
                data = await response.json(content_type=None)
        except NetgearProAvAuthError:
            raise
        except TimeoutError as err:
            raise NetgearProAvError(f"timed out calling {path}") from err
        except aiohttp.ClientError as err:
            raise NetgearProAvError(f"network error calling {path}: {err}") from err
        except Exception as err:  # noqa: BLE001 - surface device/API errors to coordinator
            raise NetgearProAvError(f"error calling {path}: {err}") from err

        if not isinstance(data, dict):
            raise NetgearProAvResponseError(f"{path} did not return a JSON object")
        resp = data.get("resp") if isinstance(data.get("resp"), dict) else data
        if isinstance(resp, dict) and resp.get("status") in {"failure", "fail", "error"}:
            if resp.get("respCode") in (12001, 12002, 403):
                raise NetgearProAvAuthError(resp.get("respMsg") or "authentication failed")
            raise NetgearProAvError(resp.get("respMsg") or f"NETGEAR API request failed for {path}")
        return data

    async def async_device_info(self) -> dict[str, Any]:
        """Read switch inventory and health summary."""
        return await self.async_get_json("device_info")

    async def async_port_config(self, port_id: int) -> dict[str, Any]:
        """Read one port configuration/status record."""
        return await self.async_get_json("swcfg_port", {"portid": port_id})

    async def async_port_config_all(self) -> dict[str, Any]:
        """Read full port configuration/status records."""
        return await self.async_get_json("swcfg_port")

    async def async_ports_status(self) -> dict[str, Any]:
        """Read all switch port status rows."""
        return await self.async_get_json("swcfg_ports_status", {"indexPage": 1, "pageSize": 9999})

    async def async_port_status(self) -> dict[str, Any]:
        """Read compact port state/VLAN rows."""
        return await self.async_get_json("port_status", {"pageSize": 9999})

    async def async_port_statistics(self, traffic_type: str) -> dict[str, Any]:
        """Read port traffic and error statistics."""
        return await self.async_get_json(
            "port_statistics",
            {"type": traffic_type, "indexPage": 1, "pageSize": 9999},
        )

    async def async_fiber_optics(self) -> dict[str, Any]:
        """Read fiber optic transceiver status."""
        return await self.async_get_json("fiber_optics")

    async def async_fiber_optics_diag(self) -> dict[str, Any]:
        """Read fiber optic diagnostic messages."""
        return await self.async_get_json("fiber_optics_diag")

    async def async_fiber_optics_eeprom(self) -> dict[str, Any]:
        """Read fiber optic EEPROM metadata."""
        return await self.async_get_json("fiber_optics_eeprom")

    async def async_stp_config(self) -> dict[str, Any]:
        """Read switch-level Spanning Tree Protocol details."""
        return await self.async_get_json("stp_config")

    async def async_stp_port_info(self) -> dict[str, Any]:
        """Read per-port Spanning Tree Protocol details."""
        return await self.async_get_json("stp_port_info")

    async def async_multicast_groups(
        self,
        index_page: int = 1,
        page_size: int = 9999,
        unit: int | None = 1,
    ) -> dict[str, Any]:
        """Read multicast group membership rows."""
        params: dict[str, Any] = {"indexPage": index_page, "pageSize": page_size}
        if unit is not None:
            params["unit"] = unit
        return await self.async_get_json("multicast_groups", params)

    async def async_dns_lookup(self, domain_name: str) -> dict[str, Any]:
        """Run a DNS lookup from the switch."""
        return await self.async_get_json("dns_lookup", {"domainName": domain_name})

    async def async_ping_test(self, ip_addr: str) -> dict[str, Any]:
        """Run a ping test from the switch."""
        return await self.async_get_json("ping_test", {"ipAddr": ip_addr})

    async def async_trace_test(self, ip_addr: str) -> dict[str, Any]:
        """Run a traceroute test from the switch."""
        return await self.async_get_json("trace_test", {"ipAddr": ip_addr})

    async def async_cable_test(self, ports: list[int]) -> dict[str, Any]:
        """Run a cable test against selected physical ports."""
        return await self.async_get_json("cable_test", {"ports": ports})

    async def async_image_info(self) -> dict[str, Any]:
        """Read firmware image information."""
        return await self.async_get_json("imageInfo")

    async def async_neighbors(self) -> dict[str, Any]:
        """Read LLDP/neighbor rows."""
        return await self.async_get_json("neighbor", {"indexPage": 1, "pageSize": 99999})

    async def async_profile_list(self) -> dict[str, Any]:
        """Read active VLAN/profile list."""
        return await self.async_get_json("profile/list")

    async def async_poe_info(self) -> dict[str, Any]:
        """Read switch PoE budget/consumption information."""
        return await self.async_get_json("swcfg_poe_info")

    async def async_poe_port_config(self, port_id: int) -> dict[str, Any]:
        """Read PoE configuration for one port."""
        try:
            return await self.async_get_legacy_json("swcfg_poe", {"portid": str(port_id)})
        except NetgearProAvError:
            return await self.async_get_json("swcfg_poe", {"portid": str(port_id)})

    async def async_lag_config(self) -> dict[str, Any]:
        """Read link aggregation group configuration."""
        return await self.async_get_json("sw_lag_cfg")

    async def async_stacking_info(self) -> dict[str, Any]:
        """Read switch stacking information."""
        return await self.async_get_json("stacking")

    async def async_set_port_admin_state(
        self,
        port_id: int,
        enabled: bool,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        """Set a port administrative state."""
        row = {
            **config,
            "port": str(config.get("port") or port_id),
            "portNum": port_id,
            "portStr": config.get("portStr") or config.get("portName") or str(port_id),
            "unit": config.get("unit") or config.get("unitId") or 1,
            "adminState": 1 if enabled else 0,
            "description": config.get("description") or "",
            "flowControl": config.get("flowControl") or "Disable",
        }
        payload = {
            "switchPortConfig": {
                "rows": [row],
            }
        }
        return await self.async_post_json("swcfg_ports_ex", payload)

    async def async_set_port_description(
        self,
        port_id: int,
        description: str,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        """Set a port description."""
        row = {
            **config,
            "port": str(config.get("port") or port_id),
            "portNum": port_id,
            "portStr": config.get("portStr") or config.get("portName") or str(port_id),
            "unit": config.get("unit") or config.get("unitId") or 1,
            "adminState": config.get("adminState", 1),
            "description": description,
            "flowControl": config.get("flowControl") or "Disable",
        }
        try:
            return await self.async_post_json(
                "swcfg_ports_ex",
                {
                    "switchPortConfig": {
                        "rows": [row],
                    }
                },
            )
        except NetgearProAvAuthError:
            raise
        except NetgearProAvError:
            pass

        port_config = {
            "portNum": [port_id],
            "lagId": [int(config["lagId"])] if config.get("lagId") is not None else [],
            "description": description,
        }
        for key in (
            "adminState",
            "frameSize",
            "profileTemplate",
            "profileName",
            "physicalMode",
            "stpMode",
            "stpEdgeMode",
            "stpTcnGuard",
            "stpBPDUFilterMode",
            "broadcastStormControl",
            "speed",
            "duplexMode",
            "flowControl",
            "autonegotiation",
        ):
            if config.get(key) not in (None, ""):
                port_config[key] = config[key]
        return await self.async_post_json("swcfg_ports", {"switchPortConfig": port_config})

    async def async_set_poe_enabled(
        self,
        port_id: int,
        enabled: bool,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        """Enable or disable PoE on a port."""
        legacy_row = self._legacy_poe_config_payload(port_id, config, enable=enabled, reset=False)
        try:
            return await self.async_post_legacy_json(
                "swcfg_poe",
                {"poePortConfig": legacy_row},
                {"portid": str(port_id)},
            )
        except NetgearProAvError:
            row = self._poe_config_payload(port_id, config, enable=enabled, reset=False)
            return await self.async_post_json("swcfg_poe", {"poePortConfig": [row]}, {"portid": str(port_id)})

    async def async_reset_poe(self, port_id: int, config: dict[str, Any]) -> dict[str, Any]:
        """Power-cycle PoE on a port."""
        legacy_row = self._legacy_poe_config_payload(port_id, config, enable=True, reset=True)
        try:
            return await self.async_post_legacy_json(
                "swcfg_poe",
                {"poePortConfig": legacy_row},
                {"portid": str(port_id)},
            )
        except NetgearProAvError:
            row = self._poe_config_payload(port_id, config, enable=True, reset=True)
            return await self.async_post_json("swcfg_poe", {"poePortConfig": [row]}, {"portid": str(port_id)})

    def _poe_config_payload(self, port_id: int, config: dict[str, Any], *, enable: bool, reset: bool) -> dict[str, Any]:
        """Return a firmware-compatible PoE config row."""
        payload = {
            **config,
            "portNum": port_id,
            "port": str(config.get("port") or port_id),
            "enable": enable,
            "powerLimitMode": config.get("powerLimitMode", 1),
            "classification": config.get("classification", config.get("poeClass", 0)),
            "currentPower": config.get("currentPower", config.get("powerUsage", 0)),
            "powerLimit": config.get("powerLimit", 32000),
            "status": config.get("status", config.get("poeStatus", config.get("poeIsValid", 1))),
            "detectionType": config.get("detectionType", 2),
            "priority": config.get("priority", 1),
            "powerMode": config.get("powerMode", 3),
            "schedule": config.get("schedule", "None"),
            "reset": reset,
        }
        return {key: value for key, value in payload.items() if value is not None}

    def _legacy_poe_config_payload(
        self,
        port_id: int,
        config: dict[str, Any],
        *,
        enable: bool,
        reset: bool,
    ) -> dict[str, Any]:
        """Return a legacy REST-agent PoE config row."""
        payload = {
            **config,
            "portid": int(config.get("portid") or config.get("portNum") or port_id),
            "enable": enable,
            "powerLimitMode": config.get("powerLimitMode", 1),
            "classification": config.get("classification", config.get("poeClass", 0)),
            "currentPower": config.get("currentPower", config.get("powerUsage", 0)),
            "powerLimit": config.get("powerLimit", 32000),
            "status": config.get("status", config.get("poeStatus", config.get("poeIsValid", 1))),
            "reset": reset,
        }
        payload.pop("portNum", None)
        payload.pop("port", None)
        return {key: value for key, value in payload.items() if value is not None}

    async def async_set_fan_mode(self, fan_mode: int) -> dict[str, Any]:
        """Set the switch fan mode."""
        if not self.session_token or self._session_expired():
            await self.async_login()
        try:
            return await self._async_request("post", "device_fan", json={"fanMode": fan_mode})
        except NetgearProAvAuthError:
            self.session_token = None
            self.session_token_created = None
            await self.async_login()
            return await self._async_request("post", "device_fan", json={"fanMode": fan_mode})

    async def async_save_config(self) -> dict[str, Any]:
        """Save running configuration to persistent storage."""
        if not self.session_token or self._session_expired():
            await self.async_login()
        try:
            return await self._async_request("post", "switch_config")
        except NetgearProAvAuthError:
            self.session_token = None
            self.session_token_created = None
            await self.async_login()
            return await self._async_request("post", "switch_config")

    async def async_reboot(self, save: bool = True) -> None:
        """Reboot the switch, optionally saving config first."""
        payload = {"power": {"type": "reboot", "save": save}}
        if not self.session_token or self._session_expired():
            await self.async_login()
        try:
            await self._async_request_allow_empty("post", "device_power", json=payload)
        except NetgearProAvAuthError:
            self.session_token = None
            self.session_token_created = None
            await self.async_login()
            await self._async_request_allow_empty("post", "device_power", json=payload)

    async def _async_request_allow_empty(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Make an authenticated request where the switch may close without JSON."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        if not self.session_token:
            raise NetgearProAvAuthError("missing session token")
        headers = {"Accept": "application/json", "session": self.session_token}
        try:
            async with asyncio.timeout(self.timeout):
                response = await self.session.request(
                    method,
                    url,
                    json=json,
                    ssl=self.verify_ssl,
                    headers=headers,
                )
                if response.status in (401, 403):
                    raise NetgearProAvAuthError("authentication failed")
                if response.status in (200, 202, 204):
                    text = await response.text()
                    return await response.json(content_type=None) if text.strip() else None
                response.raise_for_status()
        except NetgearProAvAuthError:
            raise
        except TimeoutError:
            return None
        except aiohttp.ClientError as err:
            raise NetgearProAvError(f"network error calling {path}: {err}") from err
        return None

    async def async_post_json(
        self,
        path: str,
        payload: dict[str, Any],
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST a JSON endpoint with session refresh."""
        if not self.session_token or self._session_expired():
            await self.async_login()
        try:
            return await self._async_request("post", path, params=params, json=payload)
        except NetgearProAvAuthError:
            self.session_token = None
            self.session_token_created = None
            await self.async_login()
            return await self._async_request("post", path, params=params, json=payload)

    async def async_get_legacy_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET a JSON endpoint from the legacy REST agent."""
        return await self._async_legacy_request("get", path, params=params)

    async def async_post_legacy_json(
        self,
        path: str,
        payload: dict[str, Any],
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST a JSON endpoint to the legacy REST agent."""
        return await self._async_legacy_request("post", path, params=params, json=payload)

    async def async_vlan_membership(self, vlan_id: int) -> dict[str, Any]:
        """Read VLAN configuration for a VLAN ID."""
        return await self.async_get_json("vlan", {"vlan_id": vlan_id})

    async def _async_legacy_request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make a request to the legacy REST agent and validate the response."""
        url = f"{self.legacy_base_url}/{path.lstrip('/')}"
        try:
            async with asyncio.timeout(self.timeout):
                response = await self.session.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    ssl=self.verify_ssl,
                    auth=aiohttp.BasicAuth(self.username, self.password),
                    headers={"Accept": "application/json"},
                )
                if response.status in (401, 403):
                    raise NetgearProAvAuthError("legacy authentication failed")
                response.raise_for_status()
                data = await response.json(content_type=None)
        except NetgearProAvAuthError:
            raise
        except TimeoutError as err:
            raise NetgearProAvError(f"timed out calling legacy {path}") from err
        except aiohttp.ClientError as err:
            raise NetgearProAvError(f"network error calling legacy {path}: {err}") from err
        except Exception as err:  # noqa: BLE001 - surface device/API errors to coordinator
            raise NetgearProAvError(f"error calling legacy {path}: {err}") from err

        if not isinstance(data, dict):
            raise NetgearProAvResponseError(f"legacy {path} did not return a JSON object")
        resp = data.get("resp") if isinstance(data.get("resp"), dict) else data
        if isinstance(resp, dict) and resp.get("status") in {"failure", "fail", "error"}:
            raise NetgearProAvError(resp.get("respMsg") or f"NETGEAR legacy API request failed for {path}")
        return data
