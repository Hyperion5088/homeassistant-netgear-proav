"""Data coordinator for NETGEAR Pro AV switches."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import timedelta
import logging
import re
import time
from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import NetgearProAvClient, NetgearProAvError
from .const import CRITICAL_NEIGHBOR_MARKERS, DEFAULT_SCAN_INTERVAL, DOMAIN
from .description import description_suggestion
from .helpers import port_label, port_sort_key, should_expose_port

LOGGER = logging.getLogger(__name__)
DETAIL_REFRESH_INTERVAL = 10 * 60
ADMIN_BOUNCE_SECONDS = 5
FLAP_WINDOW_SECONDS = 5 * 60
FLAP_THRESHOLD = 4
POLL_PAUSE_SECONDS = 15 * 60
PENDING_CONTROL_SECONDS = 15


@dataclass(slots=True)
class NetgearProAvData:
    """Normalized coordinator data."""

    device_info: dict[str, Any]
    ports: dict[int, dict[str, Any]]
    port_states: dict[int, dict[str, Any]]
    port_configs: dict[int, dict[str, Any]]
    port_statistics_in: dict[int, dict[str, Any]]
    port_statistics_out: dict[int, dict[str, Any]]
    lag_configs: dict[int, dict[str, Any]]
    poe_info: dict[str, Any]
    neighbors: list[dict[str, Any]]
    neighbors_by_port: dict[int, list[dict[str, Any]]]
    vlans: dict[int, dict[str, Any]]
    fiber_optics: dict[int, dict[str, Any]]
    fiber_diag: dict[int, dict[str, Any]]
    fiber_eeprom: dict[int, dict[str, Any]]
    stp_config: dict[str, Any]
    stp_ports: dict[int, dict[str, Any]]
    multicast_groups: list[dict[str, Any]]
    multicast_groups_by_port: dict[int, dict[str, Any]]
    image_info: dict[str, Any]
    stacking_info: dict[str, Any]


class NetgearProAvCoordinator(DataUpdateCoordinator[NetgearProAvData]):
    """Fetch device, port, PoE, and selected VLAN data."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: NetgearProAvClient,
        vlans: list[int],
        scan_interval: int,
        protection_markers: list[str] | None = None,
        auto_protect_timeout: int = 0,
        storage_key: str | None = None,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval) if scan_interval > 0 else DEFAULT_SCAN_INTERVAL,
        )
        self.client = client
        self.vlans = vlans
        marker_source = CRITICAL_NEIGHBOR_MARKERS if not protection_markers else protection_markers
        self.protection_markers = tuple(
            marker.strip().lower()
            for marker in marker_source
            if marker and marker.strip()
        )
        self.auto_protect_timeout = max(0, int(auto_protect_timeout or 0))
        self._last_data: NetgearProAvData | None = None
        self._last_poll_time: datetime | None = None
        self._poll_history: list[float] = []
        self._last_detail_refresh = 0.0
        self._force_full_poll = False
        self._manually_locked_ports: set[int] = set()
        self._temporarily_unlocked_ports: set[int] = set()
        self._auto_protect_timers: dict[int, Callable[[], None]] = {}
        self._last_link_states: dict[int, tuple[Any, Any]] = {}
        self._link_flap_history: dict[int, list[float]] = {}
        self._pending_admin_states: dict[int, tuple[bool, float]] = {}
        self._reboot_confirmation = "Cancel"
        self._polling_pause_until: float | None = None
        self._polling_resume_timer: Callable[[], None] | None = None
        self._store: Store[dict[str, Any]] | None = (
            Store(hass, 1, f"{DOMAIN}.{storage_key}") if storage_key else None
        )
        self._description_target_port_id: int | None = None
        self._description_input = ""

    async def async_load_state(self) -> None:
        """Load persisted local control state."""
        if self._store is None:
            return
        stored = await self._store.async_load()
        if not stored:
            return
        self._manually_locked_ports = {
            int(port_id)
            for port_id in stored.get("manually_locked_ports", [])
            if str(port_id).isdigit()
        }
        target = stored.get("description_target_port_id")
        self._description_target_port_id = int(target) if str(target).isdigit() else None
        self._description_input = str(stored.get("description_input") or "")

    def _schedule_save_state(self) -> None:
        """Persist local control state without blocking entity updates."""
        if self._store is None:
            return
        self.hass.create_task(self._async_save_state())

    async def _async_save_state(self) -> None:
        """Persist local control state."""
        if self._store is None:
            return
        await self._store.async_save(
            {
                "manually_locked_ports": sorted(self._manually_locked_ports),
                "description_target_port_id": self._description_target_port_id,
                "description_input": self._description_input,
            }
        )

    async def async_full_poll(self) -> None:
        """Refresh every switch dataset on demand."""
        if self.polling_paused:
            self.async_update_listeners()
            return
        self._force_full_poll = True
        await self.async_request_refresh()

    @property
    def last_poll_time(self) -> datetime | None:
        """Return when the switch API was last successfully polled."""
        return self._last_poll_time

    @property
    def polls_last_minute(self) -> int:
        """Return successful API polls recorded in the last minute."""
        self._prune_poll_history()
        return len(self._poll_history)

    @property
    def average_poll_interval_seconds(self) -> float | None:
        """Return the average interval between recent successful polls."""
        self._prune_poll_history()
        if len(self._poll_history) < 2:
            return None
        intervals = [
            later - earlier
            for earlier, later in zip(self._poll_history, self._poll_history[1:], strict=False)
        ]
        return round(sum(intervals) / len(intervals), 1)

    @property
    def average_polls_per_minute(self) -> float:
        """Return recent successful polls as a per-minute rate."""
        return round(float(self.polls_last_minute), 1)

    @property
    def polling_paused(self) -> bool:
        """Return whether polling is currently paused."""
        if self._polling_pause_until is None:
            return False
        if time.monotonic() >= self._polling_pause_until:
            self.resume_polling()
            return False
        return True

    @property
    def polling_pause_remaining_seconds(self) -> int:
        """Return seconds until polling automatically resumes."""
        if self._polling_pause_until is None:
            return 0
        remaining = int(self._polling_pause_until - time.monotonic())
        return max(0, remaining)

    @property
    def polling_pause_until(self) -> str | None:
        """Return a readable pause expiry timestamp."""
        if self._polling_pause_until is None:
            return None
        wall_clock_expiry = time.time() + self.polling_pause_remaining_seconds
        return _format_timestamp(wall_clock_expiry)

    def pause_polling(self, seconds: int = POLL_PAUSE_SECONDS) -> None:
        """Pause switch API polling for a short maintenance window."""
        self._polling_pause_until = time.monotonic() + max(1, int(seconds))
        self._schedule_polling_resume()
        self.async_update_listeners()

    def resume_polling(self) -> None:
        """Resume switch API polling."""
        self.cancel_polling_pause_timer()
        self._polling_pause_until = None
        self.async_update_listeners()

    def cancel_polling_pause_timer(self) -> None:
        """Cancel a pending polling resume callback."""
        if self._polling_resume_timer is not None:
            self._polling_resume_timer()
            self._polling_resume_timer = None

    def _schedule_polling_resume(self) -> None:
        """Schedule polling to resume automatically."""
        self.cancel_polling_pause_timer()

        def _resume(_: Any) -> None:
            self._polling_resume_timer = None
            self._polling_pause_until = None
            self.async_update_listeners()

        self._polling_resume_timer = async_call_later(
            self.hass,
            self.polling_pause_remaining_seconds,
            _resume,
        )

    def is_port_unlocked(self, port_id: int) -> bool:
        """Return whether a port is manually unlocked for control."""
        return not self.is_port_locked(port_id)

    def is_port_locked(self, port_id: int) -> bool:
        """Return whether destructive controls are locked."""
        return (
            port_id in self._manually_locked_ports
            or (
                self.is_port_auto_protected(port_id)
                and port_id not in self._temporarily_unlocked_ports
            )
        )

    def is_port_manually_locked(self, port_id: int) -> bool:
        """Return whether a port was locked by the user."""
        return port_id in self._manually_locked_ports

    def is_port_temporarily_unlocked(self, port_id: int) -> bool:
        """Return whether an auto-protected port is temporarily unlocked."""
        return port_id in self._temporarily_unlocked_ports

    def is_port_auto_protected(self, port_id: int) -> bool:
        """Return whether LLDP/metadata marks a port as infrastructure."""
        return _port_auto_protection_reason(self.data, port_id, self.protection_markers) is not None if self.data else False

    def port_protection_reason(self, port_id: int) -> str | None:
        """Return why a port is auto-protected."""
        return _port_auto_protection_reason(self.data, port_id, self.protection_markers) if self.data else None

    def unlock_port(self, port_id: int) -> None:
        """Temporarily unlock a port for control."""
        was_manually_locked = port_id in self._manually_locked_ports
        self._manually_locked_ports.discard(port_id)
        if self.is_port_auto_protected(port_id):
            self._temporarily_unlocked_ports.add(port_id)
        self._schedule_auto_protect(port_id, relock_manually=was_manually_locked)
        self._schedule_save_state()
        self.async_update_listeners()

    def lock_port(self, port_id: int) -> None:
        """Lock a port for control."""
        self._temporarily_unlocked_ports.discard(port_id)
        if not self.is_port_auto_protected(port_id):
            self._manually_locked_ports.add(port_id)
        self._cancel_auto_protect(port_id)
        self._schedule_save_state()
        self.async_update_listeners()

    def cancel_auto_protect_timers(self) -> None:
        """Cancel pending auto-protection callbacks."""
        for cancel in self._auto_protect_timers.values():
            cancel()
        self._auto_protect_timers.clear()

    def _schedule_auto_protect(self, port_id: int, *, relock_manually: bool = False) -> None:
        """Schedule manual protection to return after a temporary unlock."""
        self._cancel_auto_protect(port_id)
        if self.auto_protect_timeout <= 0:
            return

        def _auto_lock(_: Any) -> None:
            self._auto_protect_timers.pop(port_id, None)
            self._temporarily_unlocked_ports.discard(port_id)
            if relock_manually and not self.is_port_auto_protected(port_id):
                self._manually_locked_ports.add(port_id)
            self._schedule_save_state()
            self.async_update_listeners()

        self._auto_protect_timers[port_id] = async_call_later(
            self.hass,
            self.auto_protect_timeout,
            _auto_lock,
        )

    def _cancel_auto_protect(self, port_id: int) -> None:
        """Cancel a pending auto-protect callback for one port."""
        if cancel := self._auto_protect_timers.pop(port_id, None):
            cancel()

    def can_change_port(self, port_id: int) -> bool:
        """Return whether configuration controls are allowed for a port."""
        return not self.is_port_locked(port_id)

    def pending_admin_state(self, port_id: int) -> bool | None:
        """Return a recent requested admin state while the switch catches up."""
        return self._pending_control_state(self._pending_admin_states, port_id)

    def _pending_control_state(self, states: dict[int, tuple[bool, float]], port_id: int) -> bool | None:
        """Return a pending control state until it expires."""
        pending = states.get(port_id)
        if pending is None:
            return None
        state, expires = pending
        if time.monotonic() >= expires:
            states.pop(port_id, None)
            return None
        return state

    def _set_pending_admin_state(self, port_id: int, enabled: bool) -> None:
        """Temporarily hold the requested admin state for UI consistency."""
        self._pending_admin_states[port_id] = (enabled, time.monotonic() + PENDING_CONTROL_SECONDS)
        self.async_update_listeners()

    def description_target_options(self) -> list[str]:
        """Return selectable port labels for manual description edits."""
        if self.data is None:
            return []
        return [
            port_label(self.data.ports.get(port_id) or self.data.port_configs.get(port_id, {}), port_id)
            for port_id in self._description_target_port_ids()
        ]

    @property
    def description_target_option(self) -> str | None:
        """Return the currently selected description target label."""
        if self.data is None or self._description_target_port_id is None:
            return None
        port_id = self._description_target_port_id
        if port_id not in self._description_target_port_ids():
            return None
        return port_label(self.data.ports.get(port_id) or self.data.port_configs.get(port_id, {}), port_id)

    @property
    def description_input(self) -> str:
        """Return the pending manual description text."""
        return self._description_input

    def set_description_target(self, option: str) -> None:
        """Select the target port for a manual description edit."""
        if self.data is None:
            return
        for port_id in self._description_target_port_ids():
            port = self.data.ports.get(port_id, {})
            config = self.data.port_configs.get(port_id, {})
            if port_label(port or config, port_id) == option:
                self._description_target_port_id = port_id
                self._description_input = ""
                self._schedule_save_state()
                self.async_update_listeners()
                return

    def set_description_input(self, value: str) -> None:
        """Set pending manual description text."""
        self._description_input = value[:64]
        self._schedule_save_state()
        self.async_update_listeners()

    def selected_description_context(self) -> dict[str, Any]:
        """Return context for the selected manual description edit."""
        if self.data is None or self._description_target_port_id is None:
            return {"target_selected": False}
        port_id = self._description_target_port_id
        port = self.data.ports.get(port_id, {})
        config = self.data.port_configs.get(port_id, {})
        current = str(port.get("description") or config.get("description") or "")
        suggestion = description_suggestion(
            port_id,
            port,
            config,
            self.data.lag_configs,
            _lldp_neighbors(self.data.neighbors_by_port.get(port_id, [])),
        )
        lldp_proposed = str(suggestion.get("proposed_description") or "")
        proposed = lldp_proposed or self._description_input
        return {
            "target_selected": True,
            "port": port_label(port or config, port_id),
            "current": current,
            "proposed": proposed,
            "change": f"{current} > {proposed}",
            "manual_input": self._description_input,
            "lldp_proposed": lldp_proposed,
            "lldp_can_apply": bool(suggestion.get("can_apply")),
            "lldp_skip_reason": suggestion.get("skip_reason"),
            "lldp_remote_host": suggestion.get("remote_host"),
            "lldp_remote_interface": suggestion.get("remote_interface"),
        }

    async def async_set_selected_port_description(self) -> None:
        """Apply the pending manual description edit."""
        if self._description_target_port_id is None:
            return
        context = self.selected_description_context()
        description = str(self._description_input or context.get("lldp_proposed") or "")
        if not description:
            return
        await self.async_set_port_description(self._description_target_port_id, description)

    def _description_target_port_ids(self) -> list[int]:
        """Return exposed ports that can be selected for manual description edits."""
        if self.data is None:
            return []
        return [
            port_id
            for port_id in sorted(set(self.data.ports) | set(self.data.port_configs), key=port_sort_key)
            if should_expose_port(
                port_id,
                self.data.ports.get(port_id) or self.data.port_configs.get(port_id, {}),
                self.data.lag_configs,
            )
        ]

    def port_flap_attributes(self, port_id: int) -> dict[str, Any]:
        """Return recent link-change attributes for one port."""
        changes = self._recent_flap_times(port_id)
        return {
            "recent_link_change_count": len(changes),
            "recent_link_change_times": [_format_timestamp(timestamp) for timestamp in changes],
            "flap_window_seconds": FLAP_WINDOW_SECONDS,
            "flap_threshold": FLAP_THRESHOLD,
            "flapping": len(changes) >= FLAP_THRESHOLD,
        }

    def flap_summary(self) -> dict[str, Any]:
        """Return switch-level flap summary attributes."""
        flapping_ports = [
            port_id
            for port_id in sorted(self._link_flap_history)
            if len(self._recent_flap_times(port_id)) >= FLAP_THRESHOLD
        ]
        changed_ports = [
            port_id
            for port_id in sorted(self._link_flap_history)
            if self._recent_flap_times(port_id)
        ]
        return {
            "flapping_port_count": len(flapping_ports),
            "flapping_ports": [self._port_summary(port_id) for port_id in flapping_ports],
            "ports_with_recent_changes": [self._port_summary(port_id) for port_id in changed_ports],
            "flap_window_seconds": FLAP_WINDOW_SECONDS,
            "flap_threshold": FLAP_THRESHOLD,
        }

    def _recent_flap_times(self, port_id: int) -> list[float]:
        """Return recent link-change timestamps for one port."""
        cutoff = time.time() - FLAP_WINDOW_SECONDS
        changes = [timestamp for timestamp in self._link_flap_history.get(port_id, []) if timestamp >= cutoff]
        self._link_flap_history[port_id] = changes
        return changes

    def _port_summary(self, port_id: int) -> dict[str, Any]:
        """Return a compact port summary for diagnostics."""
        port = self.data.ports.get(port_id, {}) if self.data else {}
        config = self.data.port_configs.get(port_id, {}) if self.data else {}
        label = port.get("portName") or config.get("portName") or port.get("name") or config.get("name") or port_id
        return {
            "port_id": port_id,
            "port": str(label),
            "description": port.get("description") or config.get("description"),
            "recent_link_change_count": len(self._recent_flap_times(port_id)),
            "recent_link_change_times": [
                _format_timestamp(timestamp)
                for timestamp in self._recent_flap_times(port_id)
            ],
        }

    async def async_set_port_admin_state(self, port_id: int, enabled: bool) -> None:
        """Set port admin state with safety checks."""
        if not self.can_change_port(port_id):
            LOGGER.warning("Refusing to change locked or protected NETGEAR port %s", port_id)
            return
        config = self.data.port_configs.get(port_id, {}) if self.data else {}
        self._set_pending_admin_state(port_id, enabled)
        await self.client.async_set_port_admin_state(port_id, enabled, config)
        await self.async_full_poll()

    async def async_set_port_description(self, port_id: int, description: str) -> None:
        """Set a port description.

        Description-only changes do not affect link state, PoE, VLANs, or port
        membership, so they intentionally bypass port config protection.
        """
        config = self.data.port_configs.get(port_id, {}) if self.data else {}
        await self.client.async_set_port_description(port_id, description, config)
        await self.async_full_poll()

    async def async_set_port_descriptions(self, descriptions: dict[int, str]) -> None:
        """Set multiple safe port descriptions, then refresh once."""
        if not self.data:
            return
        for port_id, description in descriptions.items():
            config = self.data.port_configs.get(port_id, {})
            await self.client.async_set_port_description(port_id, description, config)
        await self.async_full_poll()

    async def async_bounce_port_admin(self, port_id: int, seconds: int = ADMIN_BOUNCE_SECONDS) -> None:
        """Disable a port briefly, then re-enable it with safety checks."""
        if not self.can_change_port(port_id):
            LOGGER.warning("Refusing to bounce locked or protected NETGEAR port %s", port_id)
            return
        config = self.data.port_configs.get(port_id, {}) if self.data else {}
        await self.client.async_set_port_admin_state(port_id, False, config)
        await asyncio.sleep(seconds)
        await self.client.async_set_port_admin_state(port_id, True, config)
        await self.async_full_poll()

    async def async_set_poe_enabled(self, port_id: int, enabled: bool) -> None:
        """Set port PoE state with safety checks."""
        if not self.can_change_port(port_id):
            LOGGER.warning("Refusing to change PoE on locked or protected NETGEAR port %s", port_id)
            return
        config = self.data.port_configs.get(port_id, {}) if self.data else {}
        await self.client.async_set_poe_enabled(port_id, enabled, config)
        await self.async_full_poll()

    async def async_reset_poe(self, port_id: int) -> None:
        """Power-cycle PoE with safety checks."""
        if not self.can_change_port(port_id):
            LOGGER.warning("Refusing to reset PoE on locked or protected NETGEAR port %s", port_id)
            return
        config = self.data.port_configs.get(port_id, {}) if self.data else {}
        await self.client.async_reset_poe(port_id, config)
        await self.async_full_poll()

    async def async_save_config(self) -> None:
        """Save the running switch configuration and refresh details."""
        await self.client.async_save_config()
        await self.async_full_poll()

    @property
    def reboot_confirmation(self) -> str:
        """Return the current reboot confirmation state."""
        return self._reboot_confirmation

    def set_reboot_confirmation(self, option: str) -> None:
        """Set the reboot confirmation state."""
        self._reboot_confirmation = option
        self.async_update_listeners()

    def reboot_armed(self) -> bool:
        """Return whether the switch reboot action is armed."""
        return self._reboot_confirmation == "Reboot"

    async def async_reboot(self) -> None:
        """Reboot the switch if the two-step confirmation is armed."""
        if not self.reboot_armed():
            LOGGER.warning("Refusing to reboot NETGEAR switch because reboot confirmation is not armed")
            return
        self.set_reboot_confirmation("Cancel")
        await self.client.async_reboot(save=True)

    async def _async_update_data(self) -> NetgearProAvData:
        """Fetch data from the switch."""
        if self.polling_paused and self._last_data is not None:
            return self._last_data
        try:
            ports_response = await self.client.async_ports_status()
            port_rows = ports_response.get("switchPortStatus", {}).get("rows", []) or []
            ports = {
                int(row["portNum"]): row
                for row in port_rows
                if isinstance(row, dict) and row.get("portNum") is not None
            }

            state_response = await self.client.async_port_status()
            physical_rows = state_response.get("switchPortStatus", {}).get("physical", []) or []
            port_states = {
                int(row["portNum"]): row
                for row in physical_rows
                if isinstance(row, dict) and row.get("portNum") is not None
            }
            self._update_link_flap_history(ports, port_states)

            refresh_details = self._should_refresh_details(ports, port_states)

            if refresh_details:
                data = await self._async_fetch_full_data(ports, port_states)
                self._last_data = data
                self._record_successful_poll()
                self._last_detail_refresh = time.monotonic()
                self._force_full_poll = False
                return data

            if self._last_data is None:
                data = await self._async_fetch_full_data(ports, port_states)
                self._last_data = data
                self._record_successful_poll()
                self._last_detail_refresh = time.monotonic()
                self._force_full_poll = False
                return data

            data = NetgearProAvData(
                device_info=self._last_data.device_info,
                ports=ports,
                port_states=port_states,
                port_configs=self._last_data.port_configs,
                port_statistics_in=self._last_data.port_statistics_in,
                port_statistics_out=self._last_data.port_statistics_out,
                lag_configs=self._last_data.lag_configs,
                poe_info=self._last_data.poe_info,
                neighbors=self._last_data.neighbors,
                neighbors_by_port=self._last_data.neighbors_by_port,
                vlans=self._last_data.vlans,
                fiber_optics=self._last_data.fiber_optics,
                fiber_diag=self._last_data.fiber_diag,
                fiber_eeprom=self._last_data.fiber_eeprom,
                image_info=self._last_data.image_info,
                stacking_info=self._last_data.stacking_info,
            )
            self._last_data = data
            self._record_successful_poll()
            return data
        except NetgearProAvError as err:
            raise UpdateFailed(str(err)) from err

    def _record_successful_poll(self) -> None:
        """Record a successful switch API poll."""
        now = time.time()
        self._last_poll_time = datetime.now(UTC)
        self._poll_history.append(now)
        self._prune_poll_history(now)

    def _prune_poll_history(self, now: float | None = None) -> None:
        """Keep recent poll history bounded to one minute."""
        cutoff = (time.time() if now is None else now) - 60
        self._poll_history = [timestamp for timestamp in self._poll_history if timestamp >= cutoff]

    def _update_link_flap_history(
        self,
        ports: dict[int, dict[str, Any]],
        port_states: dict[int, dict[str, Any]],
    ) -> None:
        """Record link-state changes for flap diagnostics."""
        now = time.time()
        current = _link_state_map(ports, port_states)
        if self._last_link_states:
            for port_id, state in current.items():
                if self._last_link_states.get(port_id) != state:
                    self._link_flap_history.setdefault(port_id, []).append(now)
        self._last_link_states = current
        cutoff = now - FLAP_WINDOW_SECONDS
        for port_id, changes in list(self._link_flap_history.items()):
            recent_changes = [timestamp for timestamp in changes if timestamp >= cutoff]
            if recent_changes:
                self._link_flap_history[port_id] = recent_changes
            else:
                self._link_flap_history.pop(port_id, None)

    def _should_refresh_details(
        self,
        ports: dict[int, dict[str, Any]],
        port_states: dict[int, dict[str, Any]],
    ) -> bool:
        """Return whether cached detail data should be refreshed."""
        if self._force_full_poll or self._last_data is None:
            return True
        if time.monotonic() - self._last_detail_refresh >= DETAIL_REFRESH_INTERVAL:
            return True
        if _link_state_map(ports, port_states) != _link_state_map(self._last_data.ports, self._last_data.port_states):
            return True
        return False

    async def _async_fetch_full_data(
        self,
        ports: dict[int, dict[str, Any]],
        port_states: dict[int, dict[str, Any]],
    ) -> NetgearProAvData:
        """Fetch slow detail datasets and combine them with fresh port state."""
        try:
            raw_info = await self.client.async_device_info()
            info = raw_info.get("deviceInfo", {})

            try:
                config_response = await self.client.async_port_config_all()
                port_configs = _extract_port_config_rows(config_response)
            except NetgearProAvError:
                port_configs = {}

            try:
                stats_in_response = await self.client.async_port_statistics("inbound")
                port_statistics_in = _extract_port_statistics_rows(stats_in_response)
            except NetgearProAvError:
                port_statistics_in = {}

            try:
                stats_out_response = await self.client.async_port_statistics("outbound")
                port_statistics_out = _extract_port_statistics_rows(stats_out_response)
            except NetgearProAvError:
                port_statistics_out = {}

            try:
                poe_response = await self.client.async_poe_info()
                poe_rows = poe_response.get("poeInfo") or []
                if isinstance(poe_rows, list):
                    poe_info = poe_rows[0] if poe_rows else {}
                elif isinstance(poe_rows, dict):
                    poe_info = poe_rows
                else:
                    poe_info = {}
            except NetgearProAvError:
                poe_info = {}

            if _switch_poe_capable(poe_info):
                port_ids = sorted(set(ports) | set(port_states) | set(port_configs))
                for port_id in port_ids:
                    if _has_poe_control_fields(port_configs.get(port_id, {})):
                        continue
                    try:
                        poe_port_response = await self.client.async_poe_port_config(port_id)
                    except NetgearProAvError:
                        continue
                    if poe_port_config := _extract_poe_port_config(poe_port_response, port_id):
                        port_configs[port_id] = {
                            **port_configs.get(port_id, {}),
                            **poe_port_config,
                        }

            try:
                lag_response = await self.client.async_lag_config()
                lag_configs = {
                    int(row["portNum"]): row
                    for row in lag_response.get("switchConfigLagGroup", []) or []
                    if isinstance(row, dict) and row.get("portNum") is not None
                }
            except NetgearProAvError:
                lag_configs = {}

            try:
                neighbor_response = await self.client.async_neighbors()
                neighbors = neighbor_response.get("lldpRemoteDevice", {}).get("rows", []) or []
            except NetgearProAvError:
                neighbors = []
            neighbors_by_port: dict[int, list[dict[str, Any]]] = {}
            for neighbor in neighbors:
                if not isinstance(neighbor, dict) or neighbor.get("portNum") is None:
                    continue
                port_id = int(neighbor["portNum"])
                neighbors_by_port.setdefault(port_id, []).append(neighbor)

            vlan_data: dict[int, dict[str, Any]] = {}
            vlan_ids = await self._async_active_vlan_ids()
            for vlan_id in vlan_ids:
                try:
                    data = await self.client.async_vlan_membership(vlan_id)
                    vlan_data[vlan_id] = data.get("switchConfigVlan", {})
                except NetgearProAvError:
                    continue

            try:
                fiber_response = await self.client.async_fiber_optics()
                fiber_optics = _extract_fiber_rows(fiber_response, "fiberOptics")
            except NetgearProAvError:
                fiber_optics = {}

            try:
                fiber_diag_response = await self.client.async_fiber_optics_diag()
                fiber_diag = _extract_fiber_rows(fiber_diag_response, "fiber_optics_diag")
            except NetgearProAvError:
                fiber_diag = {}

            try:
                fiber_eeprom_response = await self.client.async_fiber_optics_eeprom()
                fiber_eeprom = _extract_fiber_rows(fiber_eeprom_response, "fiberOptics")
            except NetgearProAvError:
                fiber_eeprom = {}

            try:
                stp_response = await self.client.async_stp_config()
                stp_config = stp_response.get("stp_config", {})
            except NetgearProAvError:
                stp_config = {}

            try:
                stp_port_response = await self.client.async_stp_port_info()
                stp_ports = _extract_stp_port_rows(stp_port_response)
            except NetgearProAvError:
                stp_ports = {}

            try:
                multicast_response = await self.client.async_multicast_groups()
                multicast_groups = _extract_multicast_group_rows(multicast_response)
            except NetgearProAvError:
                multicast_groups = []
            multicast_groups_by_port = _multicast_groups_by_port(multicast_groups)

            try:
                image_response = await self.client.async_image_info()
                image_info = image_response.get("deviceInfo", {})
            except NetgearProAvError:
                image_info = {}

            try:
                stacking_response = await self.client.async_stacking_info()
                stacking_info = stacking_response.get("stacking", {})
            except NetgearProAvError:
                stacking_info = {}

            return NetgearProAvData(
                device_info=info,
                ports=ports,
                port_states=port_states,
                port_configs=port_configs,
                port_statistics_in=port_statistics_in,
                port_statistics_out=port_statistics_out,
                lag_configs=lag_configs,
                poe_info=poe_info,
                neighbors=neighbors,
                neighbors_by_port=neighbors_by_port,
                vlans=vlan_data,
                fiber_optics=fiber_optics,
                fiber_diag=fiber_diag,
                fiber_eeprom=fiber_eeprom,
                stp_config=stp_config,
                stp_ports=stp_ports,
                multicast_groups=multicast_groups,
                multicast_groups_by_port=multicast_groups_by_port,
                image_info=image_info,
                stacking_info=stacking_info,
            )
        except NetgearProAvError as err:
            raise UpdateFailed(str(err)) from err

    async def _async_active_vlan_ids(self) -> list[int]:
        """Return VLAN IDs discovered from profiles, with manual config as fallback."""
        try:
            profile_response = await self.client.async_profile_list()
        except NetgearProAvError:
            return self.vlans
        vlan_ids = _extract_profile_vlan_ids(profile_response)
        return vlan_ids or self.vlans


def _link_state_map(
    ports: dict[int, dict[str, Any]],
    port_states: dict[int, dict[str, Any]],
) -> dict[int, Any]:
    """Return a compact link state map for change detection."""
    ids = set(ports) | set(port_states)
    return {
        port_id: (
            ports.get(port_id, {}).get("linkState"),
            port_states.get(port_id, {}).get("linkState"),
        )
        for port_id in ids
    }


def _lldp_neighbors(neighbors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only LLDP rows from the mixed NETGEAR neighbor endpoint."""
    return [
        neighbor
        for neighbor in neighbors
        if isinstance(neighbor, dict) and str(neighbor.get("source") or "").strip().lower() == "lldp"
    ]


def _format_timestamp(timestamp: float) -> str:
    """Return an ISO-ish UTC timestamp for diagnostics."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


def _extract_profile_vlan_ids(response: dict[str, Any]) -> list[int]:
    """Extract active VLAN IDs from profile/list."""
    vlan_ids: set[int] = set()
    for profile in response.get("profileList", []) or []:
        if not isinstance(profile, dict):
            continue
        for key in ("id", "vlanNum"):
            value = profile.get(key)
            if isinstance(value, int) and 1 <= value <= 4094:
                vlan_ids.add(value)
        for vlan in profile.get("vlans", []) or []:
            if not isinstance(vlan, dict):
                continue
            value = vlan.get("vlanId")
            if isinstance(value, int) and 1 <= value <= 4094:
                vlan_ids.add(value)
    return sorted(vlan_ids)


def _switch_poe_capable(poe_info: dict[str, Any]) -> bool:
    """Return whether switch-level PoE data indicates PoE support."""
    for key in ("consumedPower", "totalPowerAvailable", "thresholdPower"):
        value = _watts(poe_info.get(key))
        if value is not None and value > 0:
            return True
    return False


def _watts(value: Any) -> float | None:
    """Convert milliwatts to watts for PoE capability checks."""
    if value in (None, ""):
        return None
    try:
        return float(value) / 1000
    except (TypeError, ValueError):
        return None


def _has_poe_fields(config: dict[str, Any]) -> bool:
    """Return whether a port config already carries PoE data."""
    return any(
        key in config
        for key in (
            "poeIsValid",
            "poeStatus",
            "currentPower",
            "powerUsage",
            "powerLimit",
            "powerLimitMode",
            "classification",
            "poeClass",
            "enable",
        )
    )


def _has_poe_control_fields(config: dict[str, Any]) -> bool:
    """Return whether a port config carries writable PoE admin fields."""
    return all(key in config for key in ("enable", "powerLimitMode", "classification", "powerLimit", "status"))


def _extract_poe_port_config(response: dict[str, Any], port_id: int | None = None) -> dict[str, Any]:
    """Extract one-port PoE config from possible AVUI response shapes."""
    candidates = (
        response.get("poePortConfig"),
        response.get("switchPoeConfig"),
        response.get("switchConfigPoe"),
        response.get("swcfgPoe"),
    )
    for candidate in candidates:
        if isinstance(candidate, dict):
            return candidate
        if isinstance(candidate, list):
            for row in candidate:
                if isinstance(row, dict) and (port_id is None or row.get("portNum") == port_id):
                    return row
    for value in response.values():
        if isinstance(value, dict) and _has_poe_fields(value):
            return value
    return {}


def _port_auto_protection_reason(
    data: NetgearProAvData,
    port_id: int,
    markers: tuple[str, ...],
) -> str | None:
    """Return an auto-protection reason for infrastructure ports."""
    port = data.ports.get(port_id, {})
    config = data.port_configs.get(port_id, {})
    texts: list[str] = []
    for row in data.neighbors_by_port.get(port_id, []) or []:
        if not isinstance(row, dict):
            continue
        texts.extend(
            str(row.get(key) or "")
            for key in (
                "friendlyName",
                "friendly_name",
                "hostName",
                "hostIpAddress",
                "systemDescription",
                "remoteSystemDescription",
                "remoteChassisId",
            )
        )
    texts.extend(
        str(value or "")
        for value in (
            port.get("description"),
            config.get("description"),
            port.get("profileName"),
            config.get("profileName"),
        )
    )
    haystack = " ".join(texts).lower()
    for marker in markers:
        if re.search(rf"(?<![a-z0-9]){re.escape(marker)}(?![a-z0-9])", haystack):
            return f"matched '{marker}' in LLDP/port metadata"
    return None


def _extract_port_config_rows(response: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Flatten the nested swcfg_port response into a port-number keyed map."""
    rows: dict[int, dict[str, Any]] = {}
    switch_config = response.get("switchPortConfig", {})
    for unit in switch_config.get("unit", []) or []:
        if not isinstance(unit, dict):
            continue
        for slot in unit.get("slot", []) or []:
            if not isinstance(slot, dict):
                continue
            for key in ("port", "fanout"):
                for port in slot.get(key, []) or []:
                    if not isinstance(port, dict) or port.get("portNum") is None:
                        continue
                    rows[int(port["portNum"])] = port
    return rows


def _extract_port_statistics_rows(response: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Return port statistics rows keyed by port number."""
    rows: dict[int, dict[str, Any]] = {}
    for row in response.get("portStatistics", {}).get("rows", []) or []:
        if not isinstance(row, dict):
            continue
        port_id = row.get("port") or row.get("portNum")
        if port_id is None:
            continue
        rows[int(port_id)] = row
    return rows


def _extract_fiber_rows(response: dict[str, Any], key: str) -> dict[int, dict[str, Any]]:
    """Return fiber rows keyed by port number."""
    rows: dict[int, dict[str, Any]] = {}
    for row in response.get(key, []) or []:
        if not isinstance(row, dict):
            continue
        port_id = row.get("port") or row.get("portNum")
        if port_id in (None, ""):
            continue
        rows[int(port_id)] = row
    return rows


def _extract_stp_port_rows(response: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Return STP port rows keyed by port number."""
    rows: dict[int, dict[str, Any]] = {}
    for row in response.get("stpPortInfo", {}).get("portList", []) or []:
        if not isinstance(row, dict):
            continue
        port_id = row.get("portid") or row.get("portNum") or row.get("port")
        if port_id in (None, ""):
            continue
        rows[int(port_id)] = row
    return rows


def _extract_multicast_group_rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Return sorted multicast subscriber rows."""
    rows = [
        row
        for row in response.get("multicastGroups", {}).get("rows", []) or []
        if isinstance(row, dict)
    ]
    return sorted(rows, key=_multicast_sort_key)


def _multicast_groups_by_port(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Return compact multicast summaries keyed by physical port number."""
    grouped_rows: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        port = row.get("port")
        if port in (None, ""):
            continue
        try:
            port_id = int(port)
        except (TypeError, ValueError):
            continue
        grouped_rows.setdefault(port_id, []).append(row)

    return {
        port_id: {
            "group_count": len(port_rows),
            "vlans": _sorted_unique(row.get("vlanId") for row in port_rows),
            "groups": _sorted_unique(
                (row.get("multicastAddress") for row in port_rows),
                key=_ip_sort_key,
            ),
            "subscribers": _sorted_unique(
                (row.get("subscriberAddress") for row in port_rows),
                key=_ip_sort_key,
            ),
            "rows": port_rows,
        }
        for port_id, port_rows in sorted(grouped_rows.items())
    }


def _multicast_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """Return a stable multicast row sort key."""
    return (
        _number_sort_key(row.get("port")),
        _number_sort_key(row.get("vlanId")),
        _ip_sort_key(row.get("multicastAddress")),
        _ip_sort_key(row.get("subscriberAddress")),
        str(row.get("subscriberMacAddress") or "").lower(),
        str(row.get("type") or "").lower(),
    )


def _sorted_unique(values: Any, key: Callable[[Any], Any] | None = None) -> list[Any]:
    """Return unique non-empty values in a stable sorted order."""
    unique = {
        value
        for value in values
        if value not in (None, "")
    }
    return sorted(unique, key=key)


def _number_sort_key(value: Any) -> tuple[int, int | str]:
    """Sort numeric strings before non-numeric text."""
    try:
        return (0, int(value))
    except (TypeError, ValueError):
        return (1, str(value or "").lower())


def _ip_sort_key(value: Any) -> tuple[int, tuple[int, ...] | str]:
    """Sort IPv4 strings numerically before non-IP text."""
    parts = str(value or "").split(".")
    if len(parts) == 4:
        try:
            parsed = tuple(int(part) for part in parts)
        except ValueError:
            parsed = ()
        if len(parsed) == 4 and all(0 <= part <= 255 for part in parsed):
            return (0, parsed)
    return (1, str(value or "").lower())
