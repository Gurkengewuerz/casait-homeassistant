# casaIT : Smart Home for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)

`casaIT : Smart Home` is a custom Home Assistant integration for casaIT I2C and 1-Wire modules connected through the casaIT SMBus TCP bridge.

- Domain: `casait_smarthome`
- Minimum Home Assistant version: 2026.7.4
- Connection: local TCP bridge, no cloud account or YAML configuration
- Repository: `Gurkengewuerz/casait-homeassistant`

## Supported devices

Every module is found by scanning a fixed I2C address range, so the number of
modules of one kind is bounded by how many addresses that range holds.

| Module | Chip    | Address range | Max | What it provides                                      |
| ------ | ------- | ------------- | --- | ----------------------------------------------------- |
| IM117  | PCF8574 | `0x38`–`0x3F` | 8   | 8 digital inputs                                      |
| OM117  | PCF8574 | `0x20`–`0x27` | 8   | 8 digital outputs, pairable into covers               |
| DM117  | ATMega8 | `0x10`–`0x17` | 8   | 8 slots, each an input, an output, or a 12-bit dimmer |
| SM117  | DS2482  | `0x18`–`0x1B` | 4   | one 1-Wire bus each                                   |

1-Wire chips are found by enumerating each SM117 bus. They are recognised by
family code and given a default profile:

| Chip    | Family | Default profile           | Provides                                                |
| ------- | ------ | ------------------------- | ------------------------------------------------------- |
| DS18B20 | `0x28` | `ds18b20_temp`            | temperature                                             |
| DS2438  | `0x26` | `ds2438_hih5030_tept5600` | humidity, temperature, illuminance, voltage diagnostics |
| DS2413  | `0x3A` | `ds2413_in`               | two channels, each independently an input or output     |
| DS28E17 | `0x19` | `ds28e17_led`             | LED controller with a five-color palette                |

The DS2438 profile can be switched to `ds2438_hih4030_tept5600` for the older
humidity sensor. A DS2413 channel that should drive something is switched to the
output role per channel.

## Supported functionality

Which entities a module produces depends on how you configure it.

| Entity          | Created for                                                                                                                                         |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `binary_sensor` | IM117 ports, DM117 input slots, and DS2413 input channels whose role is _contact_; plus a bridge connectivity sensor                                |
| `event`         | the same inputs when their role is _button_, reporting `single_press`, `single_release`, `double_press`, `long_press`, `long_release`, and `repeat` |
| `switch`        | OM117 outputs, DM117 output slots, DS2413 output channels                                                                                           |
| `light`         | DM117 dimmer slots (12-bit, with transition), DS28E17 LED controllers                                                                               |
| `cover`         | OM117 output pairs configured as a shutter or a blind, with time-based slat tilt                                                                    |
| `sensor`        | DS18B20 and DS2438 readings, plus bridge diagnostics (latency, CRC and timeout counters, send spacing, poll-cycle duration)                         |
| `number`        | cover travel and tilt times, pulse duration, LED count and animation speed                                                                          |
| `button`        | **Rescan bus** on the bridge, and one button per OM117 pulse output                                                                                 |

Every digital input — IM117 port, DM117 input slot, DS2413 channel — is
described the same way: a role (_contact_, _button_, or _unused_), an optional
device class, an inversion flag for normally closed contacts, and for buttons an
opt-in repeat while held. The role decides which entity, if any, the input
becomes. Button inputs additionally expose device triggers.

## Home Assistant features

- Fast input polling with latched push-button edges
- IM117 inputs exposed as events, switches, contacts, or unused channels
- Press, long-press, and double-press device triggers
- OM117 switches, pulse outputs, roller shutters, and blinds with time-based slat tilt
- Runtime controls for cover calibration, pulse duration, LED count, and animation speed
- Five-color LED palettes for chase and alternate animations
- Bridge connection, latency, error-counter, send-spacing, and poll-cycle diagnostics
- Zeroconf discovery, reconfiguration, device rescanning, topology watch, and repair issues

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

| Parameter | Default | Meaning                                                             |
| --------- | ------- | ------------------------------------------------------------------- |
| Host      | —       | Hostname or IP address of the casaIT SMBus bridge                   |
| Port      | `8555`  | TCP port the bridge listens on                                      |
| Timeout   | `2.0` s | How long to wait for a bridge response before treating it as failed |

The setup flow verifies the bridge protocol with a ping before creating the
config entry, so a wrong host or an unrelated service on the port is rejected
during setup rather than after it.

If the bridge advertises an `_http._tcp.local.` service with a name beginning
with `casaithome`, Home Assistant discovers it automatically. Use **Reconfigure**
to change host, port, or timeout later without losing module options.

## How data is updated

The integration keeps a single poll loop per bridge rather than a coordinator,
because the two classes of hardware want very different treatment.

**I2C modules are pushed.** One loop reads every module and dispatches only what
actually changed, so entities never poll on their own. Inputs decide how
responsive the system feels and are read every cycle (20 ms by default). Outputs
cannot change by themselves, so they are re-read only on the slow cycle (5 s by
default) to catch drift. One cycle is batched into as few bridge frames as the
protocol allows — one operation per round trip was what made cycle time scale
with module count, not the bus itself. Writes take priority over the loop so a
command is not queued behind a full sweep.

**1-Wire chips are polled** by Home Assistant on each platform's own interval,
because a 1-Wire transaction is long and cannot be interleaved. These reads wait
for a gap between poll cycles so a temperature conversion cannot delay an input
edge:

| Entity                             | Interval |
| ---------------------------------- | -------- |
| `binary_sensor`, `switch` (DS2413) | 1 s      |
| `light` (LED controller), `number` | 10 s     |
| `sensor` (DS18B20, DS2438)         | 15 s     |

Per-chip cache intervals are set from the profile and can be overridden per
device in the options.

## Configuration options

Open **Settings > Devices & services > casaIT : Smart Home > Configure**. Module
options are collected and written in one go, so the integration reloads once when
you save rather than after every module.

**Per module**

- IM117: module name, and for each of the 8 ports a role, device class, inversion, and repeat
- OM117: module name, and per output pair a mode — switch, pulse, shutter, or blind
- DM117: module name, per slot a type (none, input, switch, dimmer), and input settings per channel
- SM117: module name
- 1-Wire: profile, DS2413 channel roles, per-device poll interval, initial LED count
- Input modules: a debounce window (40 ms by default)

**Global settings**

| Setting                | Default   | Meaning                                                                   |
| ---------------------- | --------- | ------------------------------------------------------------------------- |
| Long press threshold   | 500 ms    | How long a button must be held to report `long_press`                     |
| Double click window    | 0 ms      | Wait for a second press. `0` reports single presses immediately           |
| Repeat interval        | 400 ms    | Gap between `repeat` events while a button with repeat enabled is held    |
| Fast polling interval  | 20 ms     | Delay between input polling cycles                                        |
| Slow polling interval  | 5 s       | Consistency re-read of output-only modules                                |
| Maximum send spacing   | 5 ms      | Largest spacing the adaptive transport may use after communication errors |
| Topology scan interval | `0` (off) | How often to rescan the bus for modules that disappeared                  |
| Scans before gone      | 3         | Consecutive scans a module must miss before it is reported                |

Lowering the fast polling interval makes buttons feel more immediate at the cost
of bus and network traffic; raising it does the reverse. The double click window
is off by default because most inputs are plain wall switches, where waiting for
a possible second press only adds latency.

## Use cases

- **Wall switches driving Home Assistant logic.** Give an IM117 port the _button_
  role and it stops being a binary sensor; it becomes an event entity with device
  triggers, so one physical button can run different scenes on single, double, and
  long press.
- **Dimming while holding.** Enable repeat on a button input and it emits `repeat`
  events for as long as it is held, which an automation can turn into stepwise
  brightness changes.
- **Roller shutters and venetian blinds.** Pair two OM117 outputs and give them
  travel times; the integration derives position, and for a blind also slat tilt,
  without any position feedback from the hardware.
- **Door and window contacts.** Give an input the _contact_ role and a device
  class; a normally closed contact is handled by the inversion flag rather than a
  template.
- **Impulse relays and gate drivers.** A pulse output closes for a configured
  duration and releases on its own, exposed as a button entity.
- **Room climate from one chip.** A DS2438 with the right profile reports humidity,
  temperature, and illuminance together.

## Examples

React to a double press on the third input of an IM117:

```yaml
automation:
  - triggers:
      - trigger: device
        domain: casait_smarthome
        device_id: <your IM117 device id>
        type: double_press
        subtype: button_3
    actions:
      - action: scene.turn_on
        target:
          entity_id: scene.evening
```

Dim a light while a button is held, using the repeat event:

```yaml
automation:
  - triggers:
      - trigger: device
        domain: casait_smarthome
        device_id: <your IM117 device id>
        type: repeat
        subtype: button_1
    actions:
      - action: light.turn_on
        target:
          entity_id: light.kitchen
        data:
          brightness_step_pct: 10
```

Listen to the raw event instead, when you want one automation for several buttons:

```yaml
automation:
  - triggers:
      - trigger: event
        event_type: casait_smarthome_button_event
        event_data:
          event_type: long_press
    actions:
      - action: light.turn_off
        target:
          entity_id: all
```

Close every shutter at sunset:

```yaml
automation:
  - triggers:
      - trigger: sun
        event: sunset
    actions:
      - action: cover.close_cover
        target:
          device_id: <your OM117 device id>
```

## Known limitations

- **Cover position is calculated, not measured.** OM117 shutters and blinds have no
  position feedback. Position is derived from the configured travel times, so it
  drifts if those times are wrong or the motor is obstructed. A full open or close
  re-synchronises it.
- **Buttons need fast polling.** Gestures are derived from a polled level, so
  inputs sampled slowly cannot produce reliable presses. Inputs on hardware that
  is only read on the slow path fall back to the contact role instead of silently
  reporting half the presses.
- **Module count is capped by the address ranges** listed under Supported devices —
  8 IM117, 8 OM117, 8 DM117, and 4 SM117 per bridge.
- **One bridge per config entry.** Several bridges need several config entries.
- **The bridge client is synchronous.** Bus I/O runs in the executor rather than on
  asyncio, which is why the poll loop is deliberately kept in one place.
- **A missing module is reported, never removed on its own.** That is intentional,
  but it means a module you removed on purpose leaves a repair issue until you
  answer it.
- **No YAML configuration.** Setup is config-flow only, per Home Assistant policy.

## Service actions

### Scan devices

`casait_smarthome.scan_devices` scans the I2C and 1-Wire buses and reloads the integration so newly connected hardware appears immediately. Devices that no longer answer are reported as a repair issue rather than deleted.

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

### Topology watch

Set a **topology scan interval** in the global settings to rescan the bus periodically. It is off by default, because a full scan walks every address range and enumerates each 1-Wire bus, which takes bus time away from the inputs.

A device only counts as gone once it has been absent from several consecutive scans, so one busy scan cannot drop a module. When it does count as gone, the integration raises a repair issue and otherwise leaves the device alone — its entities, history, and references in automations all stay. The issue offers two answers:

- **Scan for it again** — rescan immediately and close the issue if the device answers.
- **Forget this device** — remove it along with its entities and their recorded history.

Nothing is deleted from the device registry without that confirmation.

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

### A DM117 slot reports the wrong type

The repair issue names the slot and both types — what you configured and what the
module answered. Either the slot holds different hardware than configured, or the
module did not accept the configuration. Check the installed slot hardware, then
submit the repair to read the module again.

### Buttons miss presses or fire twice

- Raise the debounce window of that input module if a single press reports twice.
  Mechanical contacts bounce for a few milliseconds; the default 40 ms covers most.
- Lower the fast polling interval if presses are missed entirely.
- Check the double click window: while it is open, a first press is held back to
  see whether a second one follows. Set it to `0` for plain wall switches.

### A cover ends up at the wrong position

Position is calculated from travel times, not measured. Send a full open or close
to re-synchronise, then correct the travel and tilt times in the runtime number
entities on that device.

### The bus is slow, or the poll cycle overruns

Look at the bridge diagnostic sensors: the poll-cycle duration against the
configured fast interval tells you whether the cycle actually fits. If it does not,
raise the fast polling interval or reduce how many modules are read every cycle.
CRC and timeout counters climbing at the same time point at wiring rather than
timing.

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
