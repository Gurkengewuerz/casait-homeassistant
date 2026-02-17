# casaIT : Smart Home Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)

## Overview

**casaIT : Smart Home** is a custom [Home Assistant](https://www.home-assistant.io/) integration that provides connectivity to casaIT smart home devices via I2C/SMBus protocol. It enables control and monitoring of modular smart home components including input/output modules, digital controllers, and various 1-Wire sensor devices.

- **Domain:** `casait_smarthome`
- **Quality Scale:** Bronze
- **Connection:** I2C/SMBus (via remote proxy)
- **Main code:** `custom_components/casait_smarthome/`

## Features

### Device Support
- **Input Modules (IM117):** PCF8574-based I2C input expanders
- **Output Modules (OM117):** PCF8574-based controllable outputs (switches & blinds)
- **Digital Modules (DM117):** ATMega8-based digital I/O and PWM dimming
- **Sensor Modules (SM117):** DS2482 1-Wire controllers for temperature, humidity, and digital sensors
- **1-Wire Devices:** DS18B20, DS2438, DS2413, DS28E17 profiles

### Integration Features
- Async, type-checked, fully linted codebase
- SMBus proxy communication for reliable remote I2C access
- Background polling for device state synchronization
- Zeroconf (mDNS) discovery
- Config flow with device configuration options (blind timings, LED settings, etc.)
- Native Home Assistant entities: sensors, binary sensors, switches, covers, lights
- Service for manual device scanning and 1-Wire re-enumeration

## Installation

### HACS (Recommended)
1. Go to HACS → Integrations → Custom Repositories
2. Add this repo: `Gurkengewuerz/casait-homeassistant`
3. Search for `casaIT : Smart Home` and install
4. Restart Home Assistant

### Manual
1. Download/copy the `custom_components/casait_smarthome/` folder into your Home Assistant `custom_components/` directory
2. Restart Home Assistant

## Configuration

Configuration is done via the Home Assistant UI (Integrations page). No YAML setup is required or supported.

### Initial Setup

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for `casaIT : Smart Home`
3. Enter the SMBus proxy details:
   - **Host:** IP address of the SMBus proxy server
   - **Port:** Communication port (default: 1337)
   - **Timeout:** Connection timeout in seconds (default: 5.0)
4. Click Submit

### Configuration Options

After setup, configure device-specific settings:

1. Go to **Settings → Devices & Services**
2. Find **casaIT : Smart Home**
3. Click **Configure** to adjust:
   - **Blind open/close times:** Times for OM117 blind modules
   - **Blind overrun time:** Extra movement time for blind calibration
   - **LED count:** Number of LEDs for DS28E17 LED controllers
   - **1-Wire profiles:** Configure which 1-Wire sensors are connected

### Automatic Discovery

If your SMBus proxy supports Zeroconf (mDNS), the integration can auto-discover it:
- Look for `_casaithome._tcp.local.` service announcements
- Simplifies setup without manual host entry

## Development

- **Validate code:** `./script/check` (type, lint, spell)
- **Run Home Assistant:** `./script/develop` (dev instance on port 8123)
- **Force restart:** `pkill -f "hass --config" || true && pkill -f "debugpy.*5678" || true && ./script/develop`
- **Logs:** See `config/home-assistant.log`

### Getting Started

#### Quick Start (GitHub Codespaces - Recommended)

Develop in your browser with all tools pre-configured:

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/Gurkengewuerz/casait-homeassistant?quickstart=1)

- Zero setup required
- Home Assistant included
- All dependencies pre-installed

#### Local Development

Requirements:
- Docker Desktop
- VS Code with [Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers)

Steps:
1. Clone this repository
2. Open in VS Code
3. Click "Reopen in Container" when prompted

### Development Commands

```bash
# Start Home Assistant (port 8123)
./script/develop

# Run all validations
./script/check

# Format code
./script/lint

# Run tests
./script/test

# With coverage report
./script/test --cov-html
```

### Logs and Debugging

- **Live:** Terminal where `./script/develop` runs
- **File logs:** `config/home-assistant.log` (current), `.log.1` (previous)
- **Debug logging:** Add to `config/configuration.yaml`:
  ```yaml
  logger:
    logs:
      custom_components.casait_smarthome: debug
  ```

### Project Structure

```
custom_components/casait_smarthome/
├── api.py                 # Core API client for I2C device communication
├── config_flow.py         # Configuration flow and options flow
├── const.py               # Constants (device codes, platforms, defaults)
├── manifest.json          # Integration metadata
├── services.yaml          # Service definitions
├── strings.json           # UI text (English, Zeroconf)
├── [platform]/            # Entity platforms
│   ├── __init__.py
│   └── ...
├── services/
│   ├── smbus_proxy.py     # SMBus proxy communication
│   └── i2cClasses/        # I2C device classes (DM117, PCF8574, etc.)
├── translations/
│   └── en.json            # English translations
```

### Key Modules

- **`api.py`** - CasaITApi class: Scans I2C bus, manages polling, coordinates device communication
- **`config_flow.py`** - Setup wizard for host/port/timeout, device-specific options
- **`services/smbus_proxy.py`** - SMBusProxy client for remote I2C access
- **`services/i2cClasses/`** - Device-specific classes (PCF8574, DM117, OneWireBus, LEDConfig)

## Services

### `casait_smarthome.scan_devices`

Manually scan the I2C bus for connected devices and re-enumerate all devices.

Used after connecting new hardware to force immediate discovery without waiting for the next polling cycle.

**Usage:**

```yaml
service: casait_smarthome.scan_devices
```

This service is useful after:
- Connecting new I2C modules
- Adding 1-Wire sensors to the bus
- Recovering from temporary I2C communication issues

## Coding Standards

- **Python:** 4 spaces, 120 char lines, double quotes, full type hints, async/await for all I/O
- **YAML:** 2 spaces, modern Home Assistant syntax
- **JSON:** 2 spaces, no trailing commas
- **Validation:** Always run `./script/check` before committing

See `AGENTS.md` for comprehensive developer guidelines, architecture patterns, and contribution rules.

## Supported Entity Types

Entities are dynamically created based on discovered devices. Common entity types include:

### Sensors
- **Temperature (1-Wire):** DS18B20 and DS2438 temperature readings
- **Humidity (1-Wire):** DS2438 humidity/environmental sensors
- **Input State:** DM117 digital input values
- **Analog Input:** DM117 analog/dimmer input readings

### Binary Sensors
- **Digital Inputs:** IM117/DM117 binary input states
- **Connection Status:** SMBus proxy connectivity status

### Switches
- **Output Control:** OM117 switch outputs, DM117 digital outputs
- **Relay Control:** Control individual relay outputs

### Covers (Blinds/Shutters)
- **Automated Blinds:** OM117 paired outputs for roller blinds
- **Position Tracking:** Open/close/stop commands with position memory

### Lights
- **LED Control:** DS28E17 RGB/addressable LED controllers
- **Dimmable Output:** DM117 PWM dimmer controls

### Buttons
- **Manual Triggers:** One-shot outputs for door bells, garage openers, etc.

## What is SMBus Proxy?

This integration communicates with casaIT devices via SMBus (System Management Bus), a simplified variant of I2C. Since direct I2C access from Home Assistant isn't practical, you need an **SMBus proxy service** running on a separate device (e.g., Raspberry Pi, embedded Linux device with SMBus capabilities).

The proxy forwards Home Assistant's I2C commands to the physical I2C bus and returns device state.

See your casaIT hardware documentation for proxy setup instructions.

## Contributing

Contributions welcome! Please follow:
- Code must pass `./script/check` without errors
- Use async/await for all I/O operations
- Include full type hints on all functions
- Use [Conventional Commits](https://www.conventionalcommits.org/) format
- No breaking changes without explicit approval
- Follow architecture patterns in `AGENTS.md`

## Testing

```bash
# Run all tests
./script/test -v

# Specific test file
./script/test tests/test_config_flow.py

# Update snapshots
./script/test --snapshot-update

# Coverage report
./script/test --cov-html
```

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.

---

**Made with ❤️ by [@Gurkengewuerz][user_profile]**
