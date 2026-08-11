---
applyTo: "custom_components/**/binary_sensor.py, custom_components/**/cover.py, custom_components/**/light.py, custom_components/**/sensor.py, custom_components/**/switch.py"
---

# casaIT Entity Platform Instructions

## Architecture

- Entity platforms remain flat modules; do not create per-platform packages.
- Entities communicate only through `CasaITApi`.
- Dispatcher-driven I2C entities read API caches and use `async_write_ha_state()` on updates.
- Polling 1-Wire entities call async API methods and update availability from the result.
- Do not introduce `DataUpdateCoordinator` or access synchronous drivers from an entity.

## Identity and Devices

- Use the shared helpers for bridge-scoped entity IDs, unique IDs, and device identifiers.
- Every identity must remain unique when two bridges expose the same I2C address or 1-Wire ROM.
- I2C modules are devices; each 1-Wire chip is a child device linked through its entry-scoped SM117 bus.
- Use `_attr_has_entity_name = True`, translation keys, and translation placeholders for entity names.
- Use `EntityDescription` dataclasses for reusable static metadata.

## Digital Inputs

- Every digital input carries the same `DigitalInputConfig`: role (`contact`, `button`, `unused`),
  device class, inversion, and repeat. IM117 ports, DM117 input slots, and DS2413 input channels differ
  only in how they are addressed and read.
- The role decides the platform: `contact` produces a binary sensor, `button` an event entity, `unused`
  nothing at all.
- Keep the module's natural polarity in the entity and apply `invert` on top of it; do not push the
  inversion into the drivers, which report what the chip reports.
- Button events come from the edges the API publishes on `edge_signal`, never from polling entity state.
  A module whose inputs are sampled too slowly for gestures must not offer the `button` role.
- Debouncing is per module (`InputModuleSettings`), because the bridge scanner takes one value for every
  address it samples.

## State and Availability

- Entity properties must be synchronous, side-effect free, and exception free.
- Use `None` for unknown state and `available = False` when the corresponding API state/read is unavailable.
- I2C entities should not perform additional bus reads during property access.
- After a successful write, request an API refresh or update local assumed state according to the existing pattern.
- Raise `HomeAssistantError` with a user-actionable message when a write fails.

## Async Lifecycle

- Register dispatcher callbacks in `async_added_to_hass()` with `async_on_remove()` cleanup.
- Cancel entity-owned tasks in `async_will_remove_from_hass()`.
- Respect each platform's `PARALLEL_UPDATES` and `SCAN_INTERVAL` choices.

## Breaking Changes

Changing entity IDs, unique IDs, device identifiers, translation keys, state values, or device classes is breaking.
Warn the developer first, provide a registry migration where possible, and document the impact in the commit message.
