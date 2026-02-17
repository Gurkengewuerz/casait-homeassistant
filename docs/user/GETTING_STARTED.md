# Getting Started with casaIT : Smart Home

This guide will help you install and set up the casaIT : Smart Home custom integration for Home Assistant.

## Prerequisites

- Home Assistant 2025.7.0 or newer
- HACS (Home Assistant Community Store) installed
- Network connectivity to [external service/device]

## Installation

### Via HACS (Recommended)

1. Open HACS in your Home Assistant instance
2. Go to "Integrations"
3. Click the three dots in the top right corner
4. Select "Custom repositories"
5. Add this repository URL: `https://github.com/Gurkengewuerz/casait-homeassistant`
6. Set category to "Integration"
7. Click "Add"
8. Find "casaIT : Smart Home" in the integration list
9. Click "Download"
10. Restart Home Assistant

### Manual Installation

1. Download the latest release from the [releases page](https://github.com/Gurkengewuerz/casait-homeassistant/releases)
2. Extract the `casait_smarthome` folder from the archive
3. Copy it to `custom_components/casait_smarthome/` in your Home Assistant configuration directory
4. Restart Home Assistant

## Initial Setup

After installation, add the integration:

1. Go to **Settings** → **Devices & Services**
2. Click **+ Add Integration**
3. Search for "casaIT : Smart Home"

## Troubleshooting

### Connection Failed

If setup fails with connection errors:

1. Verify the host/IP address is correct and reachable
2. Check that the API key/token is valid
3. Ensure no firewall is blocking the connection
4. Check Home Assistant logs for detailed error messages

### Entities Not Updating

If entities show "Unavailable" or don't update:

1. Check that the device/service is online
2. Verify API credentials haven't expired
3. Review logs: **Settings** → **System** → **Logs**
4. Try reloading the integration

### Debug Logging

Enable debug logging to troubleshoot issues:

```yaml
logger:
  default: warning
  logs:
    custom_components.casait_smarthome: debug
```

Add this to `configuration.yaml`, restart, and reproduce the issue. Check logs for detailed information.

## Next Steps

- See [CONFIGURATION.md](./CONFIGURATION.md) for detailed configuration options
- See [EXAMPLES.md](./EXAMPLES.md) for more automation examples
- Report issues at [GitHub Issues](https://github.com/Gurkengewuerz/casait-homeassistant/issues)

## Support

For help and discussion:

- [GitHub Discussions](https://github.com/Gurkengewuerz/casait-homeassistant/discussions)
- [Home Assistant Community Forum](https://community.home-assistant.io/)
