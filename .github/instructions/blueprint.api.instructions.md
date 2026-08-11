---
applyTo: "custom_components/**/api.py, custom_components/**/services/**/*.py"
---

# casaIT API and Hardware Instructions

The casaIT integration does not use a `DataUpdateCoordinator`. Preserve its API-mediated polling architecture.

## Layering

```text
Entities → CasaITApi → synchronous hardware drivers
```

- `CasaITApi` owns discovery, state caches, the free-running poll loop, dispatcher signals, and async writes.
- Hardware drivers do not import Home Assistant, access `hass`, or instantiate entities.
- Entities never access the SMBus object, drivers, or `_hardware_lock` directly.
- Keep one device transaction at a time under the private hardware lock so I2C and 1-Wire access remain fair.

## Async Boundaries

- Synchronous SMBus and hardware-driver operations run through `hass.async_add_executor_job()`.
- Public hardware-facing API methods are async and return typed values or explicit success/failure results.
- Never hold the hardware lock while calling Home Assistant callbacks or writing entity state.
- Bound waits and initialization with timeouts; cancellation must propagate cleanly.

## Polling and State

- The integration intentionally uses a free-running polling loop for shared I2C state, at
  `fast_poll_interval` (20 ms by default).
- Pack a cycle's reads into as few `I2CBatch` frames as the limits allow; one operation per round
  trip made cycle time scale with the module count.
- Input modules are handed to the bridge scanner once at startup; a bridge on older firmware
  rejects the command and the poll loop reads those addresses itself. Never probe it repeatedly.
- Cache updates dispatch the config-entry-scoped state signal.
- One failed module read must not erase healthy state for other modules.
- A stopped or failed poll task must be observable through initialization/error state.
- 1-Wire polling intervals and profiles come from config-entry options.

## Error Handling

- Convert driver/proxy errors into stable API results or integration-specific exceptions at the API boundary.
- Do not raise from entity properties.
- Log hardware context such as module type and address without exposing credentials or private configuration.
- Setup-time offline conditions are surfaced as `ConfigEntryNotReady` by the integration setup layer.

## Driver Rules

- Keep protocol framing, CRC handling, and synchronous I/O inside `services/`.
- Validate response lengths and value ranges before mutating cached state.
- Do not change bus timing, port mapping, or protocol contracts without explicit hardware evidence.
