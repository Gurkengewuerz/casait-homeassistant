# casaIT : Smart Home for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)

`casaIT : Smart Home` is a custom Home Assistant integration for casaIT I2C and 1-Wire modules connected through the casaIT SMBus TCP bridge.

- Domain: `casait_smarthome`
- Minimum Home Assistant version: 2026.8.0
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
| DS28E17 | `0x19` | detected, see below       | LED controller, or a Multisensor                        |

The DS2438 profile can be switched to `ds2438_hih4030_tept5600` for the older
humidity sensor. A DS2413 channel that should drive something is switched to the
output role per channel.

### DS28E17: LED controller or Multisensor

The DS28E17 is a 1-Wire to I2C bridge, so its family code does not say what is
behind it. The integration probes the I2C side once per setup:

- if the LED controller firmware answers at `0x42`, the chip becomes an
  **LED controller** (`ds28e17_led`);
- otherwise it looks for the sensors of the **Multisensor** board
  (`ds28e17_multisensor`) and creates entities only for the chips it finds.

| Chip     | I2C address     | Provides                                                                |
| -------- | --------------- | ----------------------------------------------------------------------- |
| SHT41    | `0x44`          | temperature, humidity; also compensates the SGP40 and STCC4             |
| SGP40    | `0x59`          | VOC index (Sensirion gas index algorithm), raw signal as a diagnostic   |
| STCC4    | `0x64` / `0x65` | CO2, plus calibration, self test, conditioning, and a calibration reset |
| VEML7700 | `0x10`          | illuminance with automatic gain and integration-time ranging            |

A board can carry any subset of these chips. The profile can still be overridden
per device in the options if detection ever picks the wrong one.

The Multisensor is sampled every 10 seconds by the integration itself rather than
polled by its entities: the VOC index algorithm has to be fed at a fixed rate,
and the SHT41 reading of the same sample is handed to the SGP40 and STCC4 for
humidity and temperature compensation. The sensors' measurement times are waited
out with the bus released, so inputs are never held up behind them. The learned
VOC baseline survives a Home Assistant restart of up to ten minutes.

## Supported functionality

Which entities a module produces depends on how you configure it.

| Entity          | Created for                                                                                                                                         |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `binary_sensor` | IM117 ports, DM117 input slots, and DS2413 input channels whose role is _contact_; a bridge connectivity sensor; the CO2 self-test result           |
| `event`         | the same inputs when their role is _button_, reporting `single_press`, `single_release`, `double_press`, `long_press`, `long_release`, and `repeat` |
| `switch`        | OM117 outputs, DM117 output slots, DS2413 output channels                                                                                           |
| `light`         | DM117 dimmer slots (12-bit, with transition), DS28E17 LED controllers                                                                               |
| `cover`         | OM117 output pairs configured as a shutter or a blind, with time-based slat tilt                                                                    |
| `sensor`        | DS18B20, DS2438 and Multisensor readings, plus bridge diagnostics (latency, CRC and timeout counters, send spacing, poll-cycle duration)            |
| `number`        | cover travel and tilt times, pulse duration, LED count and animation speed, CO2 calibration reference                                               |
| `button`        | **Rescan bus** on the bridge, one button per OM117 pulse output, and the CO2 sensor maintenance commands                                            |

Every digital input — IM117 port, DM117 input slot, DS2413 channel — is
described the same way: a role (_contact_, _button_, or _unused_), an optional
device class, an inversion flag for normally closed contacts, and for buttons an
opt-in repeat while held. The role decides which entity, if any, the input
becomes. Button inputs are event entities, so automations react to them with
Home Assistant's own **Event received** trigger.

## Home Assistant features

- Inputs sampled on the bridge and pushed as they change, with latched push-button edges
- IM117 inputs exposed as events, switches, contacts, or unused channels
- Press, long-press, double-press, and repeat events for automations
- OM117 switches, pulse outputs, roller shutters, and blinds with time-based slat tilt
- Runtime controls for cover calibration, pulse duration, LED count, and animation speed
- Five-color LED palettes for chase and alternate animations
- Bridge connection, latency, error-counter, send-spacing, sweep-time, I2C-retry, and interlock diagnostics
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

The integration uses no coordinator, because the two classes of hardware want
very different treatment.

**I2C modules are read by the bridge.** At startup the integration hands every
IM117, OM117 and DM117 to the bridge, which reads them on its own and pushes only
what changed. While nothing happens, nothing crosses the network apart from a
heartbeat every 5 s. Inputs decide how responsive the system feels and are
sampled every 20 ms by default; the bridge debounces them against its own clock,
so a press arrives one network hop after it happened and cannot fall between
two polls. Outputs cannot change by themselves, so the bridge reads them back
only every 5 s by default to catch drift, and pushes a relay it released itself,
such as a cover timer running out, right away. A DM117 is read with the inputs
if it has input slots; its input channels and a changed slot layout are pushed
at once, output and dimmer values at most once per slow period, so a dimmer ramp
does not flood Home Assistant. Every pushed reading is acknowledged, and one
lost with a dropped connection is sent again after the reconnect.

The bridge retries an I2C access once when a module missed its address, so the
odd NACK of a busy module never reaches Home Assistant. A module only counts as
unavailable when the bridge reports that it failed several reads in a row.

**1-Wire chips are scheduled** by one scheduler per bridge rather than by their
entities. Each chip is read on its own interval and the result is pushed to its
entities. A reading is one round trip: the bridge runs the reset, the ROM
select, the command and the answer on the 1-Wire bridge chip itself. Conversion
times are waited out with the bus released, so a temperature conversion never
delays a write. All DS18B20s on one bus share a single broadcast
conversion. A chip is only shown as unavailable after three failed reads in a
row.

| Chip                                   | Default interval |
| -------------------------------------- | ---------------- |
| DS2413                                 | 1 s              |
| LED controller                         | 10 s             |
| Multisensor (fixed, see above)         | 10 s             |
| DS2438                                 | 15 s             |
| DS18B20 (per bus, the shortest counts) | 60 s             |

The interval can be changed per device in the options.

## Restarts and power failures

Home Assistant and the bridge can restart in any order; neither has to be up
first.

- **Home Assistant restarts, the bus keeps running.** The modules hold their
  outputs while Home Assistant is away. After the restart the integration reads
  what they actually do, and covers restore their last position. A cover that was
  moving during the restart references itself on its next move.
- **The bridge restarts, or its connection drops.** The integration reconnects on
  its own and hands the modules to the bridge again; outputs are read in full
  before anything is written, so a command never builds on a stale output state.
  The bridge tells the two cases apart by a boot id in its ping. It keeps reading
  the modules for 15 seconds without a client, so presses during a short network
  drop arrive afterwards, and it drops a client whose heartbeat stays away for
  30 seconds.
- **The bridge is not reachable when Home Assistant starts.** Setup is retried
  with a growing delay until the bridge answers; after three failed attempts a
  repair issue says so.
- **The modules lost power.** Output modules start with every output off. The
  integration remembers the state it last commanded, also across restarts of
  Home Assistant, and notices the difference on the next read of the outputs
  (within the slow cycle, 5 s by default). Each output module, DS2413 and LED
  controller has an option **After a power failure**: restore the last state
  (default) or stay off. A DM117 that lost its slot configuration is configured
  again first. Shutters, blinds and pulse outputs are never switched on again;
  a cover that was moving loses its position and references itself on its next
  move.
- **Covers stop on the bridge.** Every cover move hands its stop time to the
  bridge, which releases the relay on its own clock. The stop no longer depends on
  network delay, and a move ends on time even if Home Assistant restarts in the
  middle of it; the bridge also releases every such relay as soon as no client is
  connected.
- **Cover relays are interlocked on the bridge.** The two relays of a shutter or
  blind pair never run together, and one direction starts at the earliest 300 ms
  after the other stopped. The cover itself waits 500 ms before reversing; the
  bridge is the safety net for everything that could go wrong on the way, and
  refuses such a write instead of passing it on. The **Interlock refusals**
  sensor counts how often it had to.
- **The bridge needs current firmware.** The integration relies on the bridge
  reading the modules, the 1-Wire commands, the interlock, output timers and the
  boot id in the ping; the bridge reports what it supports in the ping. A bridge
  with older firmware is refused during setup with a request to update it.

## Configuration options

Open **Settings > Devices & services > casaIT : Smart Home > Configure**. The menu
has four entries:

- **Configure a device** lists every module and 1-Wire device of the last bus
  scan in one list. Picking one opens a single form for it.
- **Button timing** holds the gesture thresholds shared by all push buttons.
- **Advanced: polling and bus** holds the poll cadence, transport limit and
  topology watch.
- **Save and close** writes everything at once, so the integration reloads a
  single time however many devices you changed. Closing the dialog discards the
  changes; devices with unsaved changes are marked with `*` in the list.

Repeated groups — the 8 ports of an IM117, the 4 output pairs of an OM117, the 8
slots of a DM117 — are collapsible sections, so a form stays short. Fields that
only belong to one mode, such as the travel times of a shutter or the channels of
a DM117 input slot, only appear while that mode is selected. When you change a
mode, the form comes back once with the fields that now apply.

**Per device**

- IM117: name, debounce window, and per port a role, device class, inversion, and repeat
- OM117: name, and per output pair a mode — switch, pulse, shutter, or blind — with its timings
- DM117: name, debounce window, per slot the installed hardware, and for input slots both channels
- SM117: name
- 1-Wire: name, profile where there is a choice, poll interval, LED count, DS2413 channels

**Button timing and advanced settings**

| Setting                | Default   | Meaning                                                                   |
| ---------------------- | --------- | ------------------------------------------------------------------------- |
| Long press threshold   | 500 ms    | How long a button must be held to report `long_press`                     |
| Double click window    | 0 ms      | Wait for a second press. `0` reports single presses immediately           |
| Repeat interval        | 400 ms    | Gap between `repeat` events while a button with repeat enabled is held    |
| Fast polling interval  | 20 ms     | How often the bridge samples the inputs                                   |
| Slow polling interval  | 5 s       | How often the bridge re-reads outputs and batches output changes          |
| Maximum send spacing   | 5 ms      | Largest spacing the adaptive transport may use after communication errors |
| Topology scan interval | `0` (off) | How often to rescan the bus for modules that disappeared                  |
| Scans before gone      | 3         | Consecutive scans a module must miss before it is reported                |

Lowering the fast polling interval makes buttons feel more immediate at the cost
of bus and network traffic; raising it does the reverse. The double click window
is off by default because most inputs are plain wall switches, where waiting for
a possible second press only adds latency.

## Use cases

- **Wall switches driving Home Assistant logic.** Give an IM117 port the _button_
  role and it stops being a binary sensor; it becomes an event entity, so one
  physical button can run different scenes on single, double, and long press.
- **Dimming while holding.** Enable repeat on a button input and it emits `repeat`
  events for as long as it is held, which an automation can turn into stepwise
  brightness changes.
- **Roller shutters and venetian blinds.** Pair two OM117 outputs and give them
  travel times; the integration derives position, and for a blind also slat tilt,
  without any position feedback from the hardware. Covers moved by one call or one
  automation switch in the same bus frame, so a whole facade starts and stops
  together.
- **Door and window contacts.** Give an input the _contact_ role and a device
  class; a normally closed contact is handled by the inversion flag rather than a
  template.
- **Impulse relays and gate drivers.** A pulse output closes for a configured
  duration and releases on its own, exposed as a button entity.
- **Room climate from one chip.** A DS2438 with the right profile reports humidity,
  temperature, and illuminance together.
- **Ventilation on demand.** A Multisensor with an STCC4 reports CO2 and, with an
  SGP40, a VOC index; either can drive a fan or a window reminder.

## Examples

Button inputs are event entities, so they work with Home Assistant's **Event
received** trigger. In the automation editor pick the button's event entity and the
event types to react to. React to a double press on the third input of an IM117:

```yaml
automation:
  - triggers:
      - trigger: event.received
        target:
          entity_id: event.bridge_<id>_im117_0x38_button_3
        options:
          event_type:
            - double_press
    actions:
      - action: scene.turn_on
        target:
          entity_id: scene.evening
```

Dim a light while a button is held, using the repeat event:

```yaml
automation:
  - triggers:
      - trigger: event.received
        target:
          entity_id: event.bridge_<id>_im117_0x38_button_1
        options:
          event_type:
            - repeat
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

Re-reference every shutter at night and send each back to where it was:

```yaml
automation:
  - triggers:
      - trigger: time
        at: "03:00:00"
    actions:
      - action: casait_smarthome.reference_run
        target:
          device_id: <your OM117 device id>
```

## Known limitations

- **Cover position is calculated, not measured.** OM117 shutters and blinds have no
  position feedback. Position is derived from the configured travel times, so it
  drifts with every partial move, and more so if those times are wrong or the
  motor is obstructed. A full open or close with overrun re-synchronises it. The
  `position_uncertainty` attribute shows the estimated error; with reference runs
  set to automatic, a move close to an end position first runs into that end once
  the error exceeds 5 %, and after a restart during a move the first move does
  the same. The `casait_smarthome.reference_run` action references on demand.
- **Buttons need fast polling.** Gestures are derived from a polled level, so
  inputs sampled slowly cannot produce reliable presses. Inputs on hardware that
  is only read on the slow path fall back to the contact role instead of silently
  reporting half the presses.
- **Module count is capped by the address ranges** listed under Supported devices —
  8 IM117, 8 OM117, 8 DM117, and 4 SM117 per bridge.
- **One bridge per config entry.** Several bridges need several config entries.
- **A missing module is reported, never removed on its own.** That is intentional,
  but it means a module you removed on purpose leaves a repair issue until you
  answer it.
- **No YAML configuration.** Setup is config-flow only, per Home Assistant policy.

## Bridge firmware updates

The bridge device has a **Firmware** update entity. It shows the firmware the
bridge runs, as it reports in every ping, and the newest stable release from the
[casaIT modules releases](https://git.mc8051.de/casaIT/modules/releases), checked
every six hours. Pre-releases and drafts are ignored.

Any firmware other than the newest release counts as an update, not only an
older one: a bridge running a development build, whose version is a commit hash,
gets back to the official release with one click on **Install**. An older
release is installed with the `update.install` action and its version:

```yaml
action: update.install
target:
  entity_id: update.bridge_<id>_firmware
data:
  version: v0.0.1
```

An installation downloads `cb32.bin` of that release, checks it against the
release's `SHA256SUMS.txt`, and sends it to the bridge's OTA endpoint. Polling
stops while the bridge flashes and restarts, which releases every output timer
and pauses the inputs. The update counts as done once the bridge answers again
with a new boot id and the installed version; the integration then reloads.

## Service actions

### Calibrate CO2 sensor

`casait_smarthome.calibrate_co2` runs a forced recalibration of a Multisensor's
STCC4. Expose the sensor to air of a known concentration for at least three
minutes first — fresh outdoor air is about 420 ppm. The action returns the
correction the sensor applied.

```yaml
action: casait_smarthome.calibrate_co2
data:
  device_id: <your Multisensor device id>
  target_ppm: 420
response_variable: calibration
```

The same calibration is available as the **Calibrate CO2 sensor** button, which
uses the **CO2 calibration reference** number entity of the device as target.
The device also offers a **self test** button with a result sensor, and — hidden
by default — **conditioning** (recommended after long storage) and a reset of the
calibration history. Each of these pauses the CO2 readings for a few seconds;
conditioning for about 25 seconds.

### Reference run

`casait_smarthome.reference_run` runs shutters and blinds into the nearer end
position, which clears the drift of their calculated position, and by default
back to where they were. It needs an overrun time above zero.

```yaml
action: casait_smarthome.reference_run
target:
  entity_id: cover.living_room_shutter
data:
  return_to_position: true
```

### Scan devices

`casait_smarthome.scan_devices` scans the I2C and 1-Wire buses and reloads the integration so newly connected hardware appears immediately. Devices that no longer answer are reported as a repair issue rather than deleted.

```yaml
action: casait_smarthome.scan_devices
```

The bridge device also provides a **Rescan bus** button.

### Set LED palette

`casait_smarthome.set_led_palette` writes up to five RGB colors to a DS28E17 LED controller. Color 1 is required; omitted colors are written as black. The device is picked like any other device; its 1-Wire ROM ID is accepted as well.

```yaml
action: casait_smarthome.set_led_palette
data:
  device_id: <your LED controller device id>
  color_1: [255, 0, 0]
  color_2: [0, 0, 255]
```

## Diagnostics and repairs

The bridge device includes diagnostic entities for:

- TCP connection state
- Last roundtrip latency
- CRC and timeout counters
- Current adaptive send spacing
- How long the bridge's input sweep and output sweep take
- I2C accesses only a second attempt rescued, and writes the interlock refused
- The emergency operation: how many links the bridge holds, how often it switched
  since it started, how often that failed, and when it last acted
- Why the bridge last restarted (power on, watchdog, crash, supply voltage drop, …)
  and how often it freed a stuck I2C bus since

The diagnostics download (**Settings > Devices & services > casaIT : Smart Home >
⋮ > Download diagnostics**) contains a bus overview, `bus_topology`:

- every I2C module with address, name, whether it is read every cycle or only on
  the slow cycle, and whether the bridge samples it on its own;
- every 1-Wire bus with its chips, their profile, poll interval and schedule;
- per module and per chip: reads, errors, error rate, errors in a row, the last
  error and when it happened, and the last and the smoothed round-trip latency.
  For a Multisensor the errors are counted per sensor chip.

It answers which device is slow or unreliable, where the bridge counters only
say that something is.

Home Assistant raises repair issues when a configured module is missing, a DM117 slot reports a different type than configured, or the bridge repeatedly fails to connect.

The bridge counts emergency actions that did not get through - an output module
that did not answer while Home Assistant was away. Once Home Assistant is back and
sees such a count, it raises a repair issue naming the last failed link, from the
input to the output. Submitting the repair clears the count on the bridge. A
restart of the bridge clears it too.

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

### The bus is slow, or the input sweep overruns

Look at the bridge diagnostic sensors: the input sweep against the configured
fast interval tells you whether the sweep actually fits. If it does not, raise
the fast polling interval; a DM117 with input slots costs the most, because it
needs a moment to prepare its answer. A climbing **I2C retries** count means a
module keeps missing its address and only the second attempt reaches it; CRC and
timeout counters climbing at the same time point at wiring rather than timing.

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

The integration follows the flat architecture documented in `AGENTS.md`: entity platforms call `CasaITApi`, the API serializes hardware access, and the asyncio hardware drivers never access Home Assistant directly.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
