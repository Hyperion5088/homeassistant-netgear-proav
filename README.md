# NETGEAR Pro AV Switch for Home Assistant

Home Assistant integration for NETGEAR Pro AV switches using the local AVUI REST API.

This repository is the HACS integration. The companion dashboard card is [`netgear-proav-switch-card`](https://github.com/Hyperion5088/netgear-proav-switch-card).

[![Add this repository to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Hyperion5088&repository=ha-netgear-proav&category=integration)

## Status

This V1.0 release is designed to expose switch infrastructure state without creating Home Assistant network-client trackers. Guarded switch controls are available per switch and should be enabled only where they are useful.

Current scope:

- switch inventory, firmware, uptime, CPU, memory, temperature, and PoE budget sensors
- one binary sensor per physical port, with link state as the state
- port attributes for admin state, speed, profile, VLAN/PVID, STP, traffic, LLDP, PoE/fiber details, and description suggestions
- LAG pseudo-ports are exposed only when the switch reports member ports for that LAG
- one PoE power sensor per PoE-capable port, with delivery/status details as attributes
- switch-level LLDP neighbor summary
- fan speed sensors and a fan mode select for Off, Quiet, and Cool
- guarded port admin, admin bounce, PoE enable, and PoE reset controls
- guarded switch reboot and Save Config controls
- port config protection locks with LLDP/metadata-based critical-port detection and timed temporary unlocks
- diagnostic polling controls and sensors: Full Poll, Pause Polling, Last Poll, and disabled-by-default Polls Last Minute
- switch-level Port Descriptions diagnostic sensor with current descriptions and LLDP-derived suggestions
- switch-level manual port description target/input/apply controls
- selected VLAN membership summary sensors
- SSDP and mDNS discovery prompts when the switch advertises a matching M4250/M4300/M4350/M4500 service
- subnet scanning from the config flow for known management ranges where multicast discovery is not visible
- config flow and options flow
- diagnostics with credentials redacted

Not included yet:

- VLAN/profile changes
- firmware update, config restore, or factory reset actions
- verified stack handling on stacked M4300/M4350/M4500 switches

## Installation

### HACS

1. Use the button above, or add this repository to HACS manually:
   - Repository: `https://github.com/Hyperion5088/ha-netgear-proav`
   - Category: `Integration`
2. Install `NETGEAR Pro AV Switch` from HACS.
3. Restart Home Assistant.
4. Add the integration from Settings > Devices & services.

### Manual

Copy `custom_components/netgear_proav` to your Home Assistant `custom_components` directory, restart Home Assistant, then add the integration from Settings > Devices & services.

### Related Repository

The optional Lovelace card is packaged separately as the [`netgear-proav-switch-card`](https://github.com/Hyperion5088/netgear-proav-switch-card) HACS dashboard/plugin repository. Add that repository to HACS as a dashboard/plugin repository, then add a card with `type: custom:netgear-proav-switch-card-v3`.

## Configuration

Add a switch from Home Assistant:

1. Go to Settings > Devices & services.
2. Choose Add integration.
3. Search for `NETGEAR Pro AV Switch`.
4. Enter the switch host, AVUI username, password, REST API port, and VLAN IDs to poll.
   - Do not use the built-in `admin` account for the integration.
   - Create a separate switch user in the main NETGEAR web UI for Home Assistant to use.

The integration uses:

- `POST /api/v1/login`
- the returned `user.session` value in the `session` header
- status and guarded write endpoints under `/api/v1`
- `POST /api/v1/device_fan` when changing fan mode
- port admin, PoE, config-save, reboot, and description endpoints when the related controls are enabled

Use a dedicated switch API user. NETGEAR documents sessions as exclusive per user on some models. If you use NETGEAR Engage to manage switches, avoid using the built-in `admin` account for this integration because Engage also uses `admin` by default. Sharing the same account between Engage, browser sessions, and Home Assistant can interrupt active sessions and cause polling or controls to fail.

## Controls And Safety

Write actions are exposed as guarded controls. Port admin, admin bounce, PoE enable/reset, switch reboot, Save Config, fan mode, and description updates can be enabled from the config flow or options flow.

Port config protection is stored in Home Assistant and is intended to reduce accidental changes on infrastructure ports. Critical ports are detected from LLDP and port metadata, can be temporarily unlocked for maintenance, and automatically re-lock after the configured timeout.

Use Pause Polling only when you need to log in to the vendor web UI with the same switch account used by this integration. The pause resumes automatically after the configured timeout. If you use a separate browser/Engage account, pausing is normally unnecessary.

## Notes

The integration currently assumes the AVUI API is available at `https://<switch>/api/v1` unless a different port is configured. SSL verification is optional because many switches use local or self-signed certificates.
