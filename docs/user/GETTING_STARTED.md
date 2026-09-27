# Getting started with casaIT : Smart Home

This guide covers installation, bridge setup, hardware configuration, and the first checks after setup.

## Prerequisites

- Home Assistant 2026.8.0 or newer
- A running casaIT SMBus TCP bridge
- Network access from Home Assistant to the bridge, normally on TCP port `8555`
- At least one supported casaIT I2C module or 1-Wire device

No cloud account, API key, or YAML integration configuration is required.

## Install with HACS

1. Open HACS and select **Integrations**.
2. Open the menu and select **Custom repositories**.
3. Add `https://github.com/Gurkengewuerz/casait-homeassistant` as an **Integration** repository.
4. Install **casaIT : Smart Home**.
5. Restart Home Assistant.

## Manual installation

1. Download the latest repository release.
2. Copy `custom_components/casait_smarthome/` to the Home Assistant configuration directory.
3. Restart Home Assistant.

## Connect the bridge

1. Open **Settings > Devices & services**.
2. Select **Add integration** and search for **casaIT : Smart Home**.
3. Enter the bridge hostname or IP address.
4. Enter the TCP port and response timeout.
5. Submit the form.

The integration sends a protocol ping during setup. A successful TCP connection alone is not accepted as a compatible bridge.

If the bridge is discovered through Zeroconf, confirm the discovered host and connection values instead.

## Configure modules

Open the integration and select **Configure**, then **Configure a device**. The
list shows every module and 1-Wire device found during the latest scan; pick one
to open its form. Each form lists repeated items — ports, output pairs, slots —
as collapsible sections. Changes are collected until you select **Save and
close**.

### IM117

Give the module a meaningful name and open each input to assign one role:

- **Push button** creates an event entity; automations react to it with the **Event received** trigger.
- **Contact or switch** creates a binary sensor, with an optional door, window, motion, smoke, or similar device class.
- **Unused** creates no entity.

### OM117

Open each pair of outputs and choose independent switches, a roller shutter, a
blind with slat tilt, or two pulse buttons. The timing fields of the chosen mode
appear once the mode is selected. Cover timings and pulse duration can also be
adjusted later through number entities.

Shutters and blinds also choose how they correct the drift of their calculated
position: automatically, by running into the nearer end position before a move
that ends close to it, only through the `casait_smarthome.reference_run` action,
or not at all. Reference runs need an overrun time above zero.

**After a power failure** decides what the switch outputs do when the module was
without power: restore their last state, which is the default, or stay off.
Shutters, blinds and pulse outputs always stay off. DM117 modules, DS2413 chips
and LED controllers have the same option.

### DM117

Open each slot and select what is installed: unused, digital input, digital
output, or 0-10 V dimmer. A slot switched to digital input also shows its two
channels. Home Assistant warns in Repairs if the module reports a different
EEPROM slot type.

### SM117 and 1-Wire

Name each SM117 bus and each 1-Wire device. The profile is detected
automatically and only needs to be changed to override it. DS2413 channel A and
B can be configured independently as inputs or outputs. DS28E17 LED controllers
expose LED count and animation speed controls.

### Multisensor

A DS28E17 board with any of the SHT41, SGP40, STCC4 and VEML7700 sensors is
recognised as a Multisensor, and only the fitted sensors appear as entities. If
it carries a CO2 sensor, let it run for a few minutes in fresh air and press
**Calibrate CO2 sensor**; the reference concentration is set with the **CO2
calibration reference** entity, 420 ppm by default.

## Verify the installation

After configuration:

1. Open the **casaIT bridge** device.
2. Confirm that **Connection** is connected.
3. Check that **Fast poll cycle** is below roughly 30 ms for a typical input-only fast cycle.
4. Press a configured IM117 push button and confirm that its event entity records the press.
5. Toggle an output and confirm that its state changes without a full bus refresh.

## Scan changed hardware

Press **Rescan bus** on the bridge device or call `casait_smarthome.scan_devices` after connecting or removing hardware. The integration updates the device registry and reloads its entity platforms automatically.

## Change the bridge address

Use **Reconfigure** from the integration menu. Reconfiguration preserves every module name, role, timing, and 1-Wire profile.

## Troubleshooting

### Bridge unavailable

- Verify the host and port.
- Check the bridge process and its network route.
- Check firewall rules.
- Review **Settings > System > Repairs** after repeated setup failures.

### Module missing

- Check module power and I2C wiring.
- Rescan the bus.
- Open the repair issue and submit it after correcting the hardware connection.

### Debug logging

```yaml
logger:
  default: warning
  logs:
    custom_components.casait_smarthome: debug
```

Restart Home Assistant, reproduce the problem, and inspect **Settings > System > Logs**.

## Support

- [GitHub issues](https://github.com/Gurkengewuerz/casait-homeassistant/issues)
- [GitHub discussions](https://github.com/Gurkengewuerz/casait-homeassistant/discussions)
