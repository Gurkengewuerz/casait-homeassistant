"""Support for casaIT input ports wired to push buttons."""

from __future__ import annotations

from collections.abc import Hashable, Mapping
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
    EVENT_REPEAT,
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
    get_dm117_input_configuration,
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

    settings = get_input_settings(config_entry.options)
    entities: list[CasaITInputEvent] = []

    if (input_range := get_address_range("IM117")) is not None:
        port_config = get_im117_port_configuration(config_entry.options)
        entities.extend(
            CasaITButtonEvent(api, config_entry, address, port, settings, invert=config.invert, repeat=config.repeat)
            for address in api.im117_om117
            if input_range[0] <= address <= input_range[1]
            for port, config in port_config.get(address, {}).items()
            if config.role == INPUT_ROLE_BUTTON
        )

    entities.extend(
        CasaITDM117ButtonEvent(
            api, config_entry, address, slot, channel, settings, invert=config.invert, repeat=config.repeat
        )
        for address, channels in get_dm117_input_configuration(config_entry.options).items()
        if address in api.dm117
        for (slot, channel), config in channels.items()
        if config.role == INPUT_ROLE_BUTTON
    )

    if entities:
        async_add_entities(entities)


class CasaITInputEvent(EventEntity):
    """A push button on a digital input, whatever module carries it.

    Presses are derived from the edges the poll loop reports, so a tap that was
    visible in only a single sample still produces an event.

    Every edge reports immediately: "single_press" when the button goes down,
    "long_press" the moment the hold threshold passes while it is still down,
    and "single_release" or "long_release" when it comes back up. A press that
    lands inside the double click window adds "double_press". Automations
    therefore react while the user is still holding the button.

    Subclasses supply the identity of the entity and where its edges come from:
    the key they arrive under, and the state cache that says whether the owning
    module is still responding.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = BUTTON_EVENT_TYPES
    # The chip level that means "held down" on this module type.
    _active_level: bool = False

    def __init__(
        self,
        api: CasaITApi,
        address: int,
        edge_key: Hashable,
        subtype: str,
        settings: InputSettings,
        state_cache: Mapping[int, Any],
        *,
        invert: bool = False,
        repeat: bool = False,
    ) -> None:
        """Initialize the shared press-detection state."""

        self._api = api
        self._address = address
        self._edge_key = edge_key
        self._subtype = subtype
        self._settings = settings
        self._state_cache = state_cache
        self._invert = invert
        self._repeat = repeat
        self._pressed_level = self._active_level is not invert
        self._held = False
        self._long_reported = False
        self._pending_double: CALLBACK_TYPE | None = None
        self._pending_long: CALLBACK_TYPE | None = None
        self._pending_repeat: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        """Subscribe to the edges published for this module."""

        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self._api.edge_signal(self._address), self._handle_edges)
        )
        self.async_on_remove(self._close_double_window)
        self.async_on_remove(self._cancel_pending_long)
        self.async_on_remove(self._cancel_pending_repeat)

    @property
    def available(self) -> bool:
        """Return if the owning module is responding."""

        return self._address in self._state_cache

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
    def _cancel_pending_repeat(self) -> None:
        """Stop repeating, if this input was repeating at all."""

        if self._pending_repeat is not None:
            self._pending_repeat()
            self._pending_repeat = None

    @callback
    def _handle_edges(self, edges: Mapping[Hashable, list[bool]]) -> None:
        """Translate raw input edges into button events."""

        for level in edges.get(self._edge_key, ()):
            if level is self._pressed_level:
                self._handle_press()
            else:
                self._handle_release()

    @callback
    def _handle_press(self) -> None:
        """Report the press itself and arm the hold timer."""

        self._cancel_pending_long()
        self._cancel_pending_repeat()
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
        self._arm_repeat()

    @callback
    def _arm_repeat(self) -> None:
        """Schedule the next repeat while the button is still held.

        Repeats start once the hold is established, so a normal press never
        produces one and an automation can treat them as "keep going".
        """

        if not self._repeat or not self._held:
            return

        self._pending_repeat = async_call_later(
            self.hass,
            self._settings.repeat_interval_ms / 1000,
            self._flush_repeat,
        )

    @callback
    def _flush_repeat(self, _now: Any) -> None:
        """Report one repeat and queue the next."""

        self._pending_repeat = None
        self._fire(EVENT_REPEAT)
        self._arm_repeat()

    @callback
    def _handle_release(self) -> None:
        """Report the matching release once the button comes back up."""

        if not self._held:
            return

        self._held = False
        self._cancel_pending_long()
        self._cancel_pending_repeat()

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
                    EVENT_DATA_SUBTYPE: self._subtype,
                },
            )
        self.async_write_ha_state()


class CasaITButtonEvent(CasaITInputEvent):
    """A push button on an IM117 input port."""

    _attr_translation_key = "im117_button"
    # PCF8574 inputs are active low: the level drops while the button is held.
    _active_level = False

    def __init__(
        self,
        api: CasaITApi,
        config_entry: CasaITConfigEntry,
        address: int,
        port: int,
        settings: InputSettings,
        *,
        invert: bool = False,
        repeat: bool = False,
    ) -> None:
        """Initialize the IM117 button event entity."""

        super().__init__(
            api,
            address,
            PCF8574_MAPPED_PORTS[port],
            f"button_{port + 1}",
            settings,
            api.pcf_states,
            invert=invert,
            repeat=repeat,
        )

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


class CasaITDM117ButtonEvent(CasaITInputEvent):
    """A push button on one channel of a DM117 input slot."""

    _attr_translation_key = "dm117_button"
    # DM117 input responses report a closed contact as a set bit.
    _active_level = True

    def __init__(
        self,
        api: CasaITApi,
        config_entry: CasaITConfigEntry,
        address: int,
        slot: int,
        channel: int,
        settings: InputSettings,
        *,
        invert: bool = False,
        repeat: bool = False,
    ) -> None:
        """Initialize the DM117 button event entity."""

        channel_name = "a" if channel == 0 else "b"
        super().__init__(
            api,
            address,
            (slot, channel),
            f"button_slot_{slot + 1}_{channel_name}",
            settings,
            api.dm117_states,
            invert=invert,
            repeat=repeat,
        )

        bridge_slug = build_bridge_slug(config_entry.entry_id, config_entry.unique_id)
        self._attr_unique_id = f"{config_entry.entry_id}_dm117_{address}_{slot}_{channel}_button"
        self.entity_id = build_i2c_entity_id(
            "event", bridge_slug, "dm117", address, "slot", slot + 1, "button", channel_name
        )
        self._attr_translation_placeholders = {"slot": str(slot + 1), "channel": channel_name.upper()}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, build_device_identifier(config_entry.entry_id, "dm117", address))},
            name=get_module_name(config_entry.options, "dm117", address, f"DM117 0x{address:02X}"),
            manufacturer="casaIT",
            model="DM117",
            via_device=(DOMAIN, build_device_identifier(config_entry.entry_id, "bridge", "controller")),
        )
