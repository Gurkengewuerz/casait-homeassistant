# casaIT : Smart Home for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)

`casaIT : Smart Home` is a custom Home Assistant integration for casaIT I2C and 1-Wire modules connected through the casaIT SMBus TCP bridge.

- Domain: `casait_smarthome`
- Minimum Home Assistant version: 2026.7.4
- Connection: local TCP bridge, no cloud account or YAML configuration
- Repository: `Gurkengewuerz/casait-homeassistant`

## Supported hardware

- IM117 PCF8574 input modules
- OM117 PCF8574 output modules
- DM117 digital input, digital output, and 12-bit dimmer slots
- SM117 DS2482 1-Wire bridges
- DS18B20 temperature sensors
- DS2438 environmental sensors, including optional voltage diagnostics
- DS2413 dual-channel I/O with an independent role per channel
- DS28E17 LED controllers

## Home Assistant features

- Fast input polling with latched push-button edges
- IM117 inputs exposed as events, switches, contacts, or unused channels
- Press, long-press, and double-press device triggers
- OM117 switches, pulse outputs, roller shutters, and blinds with time-based slat tilt
- Runtime controls for cover calibration, pulse duration, LED count, and animation speed
- Five-color LED palettes for chase and alternate animations
- Bridge connection, latency, error-counter, send-spacing, and poll-cycle diagnostics
- Zeroconf discovery, reconfiguration, device rescanning, stale-device cleanup, and repair issues

## Installation

### HACS

1. Open HACS and select **Integrations**.
2. Add `Gurkengewuerz/casait-homeassistant` as a custom integration repository.
3. Install **casaIT : Smart Home**.
4. Restart Home Assistant.

### Manual installation

1. Download the latest release.
2. Copy `custom_components/casait_smarthome/` into the Home Assistant configuration directory.
3. Restart Home Assistant.

## Setup

1. Open **Settings > Devices & services > Add integration**.
2. Search for **casaIT : Smart Home**.
3. Enter the SMBus bridge host, TCP port, and response timeout.

The default port is `8555` and the default timeout is `2.0` seconds. The setup flow verifies the bridge protocol with a ping before creating the config entry.

If the bridge advertises an `_http._tcp.local.` service with a name beginning with `casaithome`, Home Assistant can discover it automatically.

## Configuration options

Open **Settings > Devices & services > casaIT : Smart Home > Configure** to change:

- IM117 module names and the role of every input
- OM117 module names and output-pair modes
- Blind, shutter, tilt, and pulse timings
- DM117 module names and slot types
- SM117 module names
- 1-Wire profiles, DS2413 channel roles, polling intervals, and initial LED count
- Fast and slow polling intervals, button timing, and adaptive send-spacing limit

Use **Reconfigure** from the integration menu to change the bridge host, port, or timeout without losing module options.

## Service actions

### Scan devices

`casait_smarthome.scan_devices` scans the I2C and 1-Wire buses, removes stale registry devices, and reloads the integration so newly connected hardware appears immediately.

```yaml
action: casait_smarthome.scan_devices
```

The bridge device also provides a **Rescan bus** button.

### Set LED palette

`casait_smarthome.set_led_palette` writes up to five RGB colors to a DS28E17 LED controller. Color 1 is required; omitted colors are written as black.

```yaml
action: casait_smarthome.set_led_palette
data:
  device_id: "1900000000000001"
  color_1: [255, 0, 0]
  color_2: [0, 0, 255]
```

## Diagnostics and repairs

The bridge device includes diagnostic entities for:

- TCP connection state
- Last roundtrip latency
- CRC and timeout counters
- Current adaptive send spacing
- Fast and full poll-cycle duration

Home Assistant raises repair issues when a configured module is missing, a DM117 slot reports a different type than configured, or the bridge repeatedly fails to connect.

## Troubleshooting

### Setup cannot connect

- Verify the bridge host and port.
- Confirm that Home Assistant can reach the bridge over the local network.
- Check that no firewall blocks the TCP port.
- Confirm that the endpoint is the casaIT SMBus bridge, not merely another open TCP service.

### A module or entity is unavailable

- Check module power and I2C wiring.
- Press **Rescan bus** on the bridge device.
- Open **Settings > System > Repairs** for a hardware-specific issue.
- Inspect the bridge diagnostic entities and Home Assistant logs.

### Debug logging

```yaml
logger:
  default: warning
  logs:
    custom_components.casait_smarthome: debug
```

Restart Home Assistant after changing logging configuration.

## Development

Always use the repository scripts because they activate the expected environment and perform cleanup:

```bash
script/develop
script/check
script/test
script/hassfest
```

The integration follows the flat architecture documented in `AGENTS.md`: entity platforms call `CasaITApi`, the API serializes hardware access, and synchronous drivers never access Home Assistant directly.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
