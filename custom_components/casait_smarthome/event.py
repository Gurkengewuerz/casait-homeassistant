"""Support for casaIT input ports wired to push buttons."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later

from . import CasaITConfigEntry
from .api import CasaITApi
from .const import (
    BUTTON_EVENT_TYPES,
    DOMAIN,
    EVENT_BUTTON,
    EVENT_DATA_EVENT_TYPE,
    EVENT_DATA_SUBTYPE,
    EVENT_DOUBLE_PRESS,
    EVENT_LONG_PRESS,
    EVENT_LONG_RELEASE,
    EVENT_SINGLE_PRESS,
    EVENT_SINGLE_RELEASE,
    INPUT_ROLE_BUTTON,
    PCF8574_MAPPED_PORTS,
)
from .helpers import (
    InputSettings,
    build_bridge_slug,
    build_device_identifier,
    build_i2c_entity_id,
    get_address_range,
    get_im117_port_configuration,
    get_input_settings,
    get_module_name,
)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: CasaITConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up event entities for input ports configured as buttons."""

    api: CasaITApi = config_entry.runtime_data
    await api.async_wait_initialized()

    input_range = get_address_range("IM117")
    if input_range is None:
        return

    port_config = get_im117_port_configuration(config_entry.options)
    settings = get_input_settings(config_entry.options)

    entities = [
        CasaITButtonEvent(api, config_entry, address, port, settings, invert=config.invert)
        for address in api.im117_om117
        if input_range[0] <= address <= input_range[1]
        for port, config in port_config.get(address, {}).items()
        if config.role == INPUT_ROLE_BUTTON
    ]

    if entities:
        async_add_entities(entities)


class CasaITButtonEvent(EventEntity):
    """A push button on an IM117 input port.

    Presses are derived from the edges the poll loop latches, so a tap that was
    visible in only a single sample still produces an event.

    Every edge reports immediately: "single_press" when the button goes down,
    "long_press" the moment the hold threshold passes while it is still down,
    and "single_release" or "long_release" when it comes back up. A press that
    lands inside the double click window adds "double_press". Automations
    therefore react while the user is still holding the button.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = BUTTON_EVENT_TYPES
    _attr_translation_key = "im117_button"

    def __init__(
        self,
        api: CasaITApi,
        config_entry: CasaITConfigEntry,
        address: int,
        port: int,
        settings: InputSettings,
        *,
        invert: bool = False,
    ) -> None:
        """Initialize the button event entity."""

        self._api = api
        self._address = address
        self._port = port
        self._hardware_port = PCF8574_MAPPED_PORTS[port]
        self._settings = settings
        self._invert = invert
        self._held = False
        self._long_reported = False
        self._pending_double: CALLBACK_TYPE | None = None
        self._pending_long: CALLBACK_TYPE | None = None

        bridge_slug = build_bridge_slug(config_entry.entry_id, config_entry.unique_id)
        self._attr_unique_id = f"{config_entry.entry_id}_im117_{address}_{port}_button"
        self.entity_id = build_i2c_entity_id("event", bridge_slug, "im117", address, "button", port + 1)
        self._attr_translation_placeholders = {"port": str(port + 1)}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, build_device_identifier(config_entry.entry_id, "im117", address))},
            name=get_module_name(config_entry.options, "im117", address, f"IM117 0x{address:02X}"),
            manufacturer="casaIT",
            model="PCF8574 Input",
            via_device=(DOMAIN, build_device_identifier(config_entry.entry_id, "bridge", "controller")),
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to the edges published for this module."""

        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self._api.edge_signal(self._address), self._handle_edges)
        )
        self.async_on_remove(self._close_double_window)
        self.async_on_remove(self._cancel_pending_long)

    @property
    def available(self) -> bool:
        """Return if the owning module is responding."""

        return self._address in self._api.pcf_states

    @callback
    def _close_double_window(self) -> None:
        """Stop treating the next press as the second half of a double press."""

        if self._pending_double is not None:
            self._pending_double()
            self._pending_double = None

    @callback
    def _cancel_pending_long(self) -> None:
        """Drop the running hold timer, if any."""

        if self._pending_long is not None:
            self._pending_long()
            self._pending_long = None

    @callback
    def _handle_edges(self, edges: dict[int, list[bool]]) -> None:
        """Translate raw port edges into button events."""

        for level in edges.get(self._hardware_port, ()):
            # Inputs are active low: the level drops while the button is held. A
            # button wired the other way round inverts that.
            if level is not self._invert:
                self._handle_release()
            else:
                self._handle_press()

    @callback
    def _handle_press(self) -> None:
        """Report the press itself and arm the hold timer."""

        self._cancel_pending_long()
        self._held = True
        self._long_reported = False
        self._fire(EVENT_SINGLE_PRESS)

        if self._pending_double is not None:
            # A press that lands inside the window opened by the previous
            # release completes a double press.
            self._close_double_window()
            self._fire(EVENT_DOUBLE_PRESS)

        self._pending_long = async_call_later(
            self.hass,
            self._settings.long_press_ms / 1000,
            self._flush_long_press,
        )

    @callback
    def _flush_long_press(self, _now: Any) -> None:
        """Report the long press as soon as the button has been held long enough.

        Waiting for the release would delay the feedback until the user lets go,
        which makes a hold feel unresponsive.
        """

        self._pending_long = None
        self._long_reported = True
        self._fire(EVENT_LONG_PRESS)

    @callback
    def _handle_release(self) -> None:
        """Report the matching release once the button comes back up."""

        if not self._held:
            return

        self._held = False
        self._cancel_pending_long()

        if self._long_reported:
            self._long_reported = False
            self._fire(EVENT_LONG_RELEASE)
            return

        self._fire(EVENT_SINGLE_RELEASE)

        if self._settings.double_click_ms > 0:
            self._pending_double = async_call_later(
                self.hass,
                self._settings.double_click_ms / 1000,
                self._expire_double_window,
            )

    @callback
    def _expire_double_window(self, _now: Any) -> None:
        """Forget the previous release once the double click window has passed."""

        self._pending_double = None

    @callback
    def _fire(self, event_type: str) -> None:
        """Publish an event and push the new state."""

        self._trigger_event(event_type)
        if self.device_entry is not None:
            self.hass.bus.async_fire(
                EVENT_BUTTON,
                {
                    ATTR_DEVICE_ID: self.device_entry.id,
                    EVENT_DATA_EVENT_TYPE: event_type,
                    EVENT_DATA_SUBTYPE: f"button_{self._port + 1}",
                },
            )
        self.async_write_ha_state()
