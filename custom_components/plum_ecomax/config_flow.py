"""Config flow for the Plum EcoMAX integration.

This module handles the configuration flow for setting up the integration
via the Home Assistant UI. It allows the user to define the IP address,
port, password, and active heating circuits.
"""

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_IP_ADDRESS, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CIRCUIT_CHOICES,
    CONF_ACTIVE_CIRCUITS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_PORT,
    DOMAIN,
    MAX_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
    UPDATE_INTERVAL,
)
from .plum_device import DEVICE_MAP_PATH, PlumDevice

_LOGGER = logging.getLogger(__name__)

# A parameter every supported boiler exposes, used purely to prove that a
# real ecoNET conversation (connect + framed request/response + CRC) works,
# not just that something is listening on the TCP port.
_PROBE_SLUG = "hdwstate"
# Factory serial number: the entry's unique_id when readable (an IP can change).
SERIAL_SLUG = "uid"


def _build_data_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    """Builds the connection form schema, pre-filled from `defaults`."""
    return vol.Schema(
        {
            vol.Required(
                CONF_IP_ADDRESS, default=defaults.get(CONF_IP_ADDRESS, vol.UNDEFINED)
            ): str,
            vol.Optional(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): int,
            vol.Optional(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "admin")): str,
            vol.Required(CONF_PASSWORD, default=defaults.get(CONF_PASSWORD, "0000")): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
            vol.Required(
                CONF_ACTIVE_CIRCUITS, default=defaults.get(CONF_ACTIVE_CIRCUITS, ["2"])
            ): SelectSelector(
                SelectSelectorConfig(
                    options=CIRCUIT_CHOICES,
                    mode=SelectSelectorMode.DROPDOWN,
                    multiple=True,
                    translation_key="circuits_selector",
                )
            ),
            vol.Optional(
                CONF_UPDATE_INTERVAL, default=defaults.get(CONF_UPDATE_INTERVAL, UPDATE_INTERVAL)
            ): vol.All(
                vol.Coerce(int), vol.Range(min=MIN_UPDATE_INTERVAL, max=MAX_UPDATE_INTERVAL)
            ),
        }
    )


def _normalise_serial(raw) -> str | None:
    """Boiler serial number as a stable string, or None if unreadable."""
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).split(b"\x00")[0].decode("ascii", errors="ignore")
    if raw is None:
        return None
    serial = str(raw).strip()
    return serial or None


async def _probe_boiler(hass, user_input: dict) -> tuple[str | None, str | None]:
    """Tries an actual protocol-level read against the boiler.

    This proves the IP/port really reach an ecoNET module -- not just that a
    TCP port happens to be open -- by loading the bundled parameter map and
    reading one well-known parameter. The credentials are NOT proven here:
    reads never send them, only writes do (the boiler answers 0x7D to a bad
    password on a write), so a bad password can't be detected without
    writing something to the boiler.

    Returns:
        tuple: (error code to show on the form or None on success,
        the boiler's serial number if it could be read, else None).
    """
    json_path = str(DEVICE_MAP_PATH)
    device = PlumDevice(
        user_input[CONF_IP_ADDRESS],
        port=user_input.get(CONF_PORT, DEFAULT_PORT),
        password=user_input[CONF_PASSWORD],
        user=user_input.get(CONF_USERNAME, "admin"),
        map_file=json_path,
    )

    try:
        await asyncio.to_thread(device.load_map)
    except (OSError, ValueError) as err:
        _LOGGER.error("Could not load parameter map %s: %s", json_path, err)
        return "cannot_load_map", None

    serial = None
    try:
        value = await device.get_value(_PROBE_SLUG, retries=2)
        if value is not None:
            # Best effort: a boiler that doesn't answer for "uid" is still
            # usable, the entry then falls back to an IP-based unique_id.
            with contextlib.suppress(Exception):
                serial = _normalise_serial(await device.get_value(SERIAL_SLUG, retries=2))
    except Exception as err:
        _LOGGER.debug("Connection test failed for %s: %s", user_input[CONF_IP_ADDRESS], err)
        return "cannot_connect", None
    finally:
        # This PlumDevice is a one-shot probe, never stored anywhere. Its
        # connection is now persistent (kept open on success) rather than
        # closed after every transaction, so it has to be closed
        # explicitly here or it leaks an open socket until garbage
        # collection gets around to the object.
        await device.async_close()

    if value is None:
        return "cannot_connect", None
    return None, serial


async def _validate_connection(hass, user_input: dict) -> str | None:
    """Error code from _probe_boiler(), or None on success."""
    error, _serial = await _probe_boiler(hass, user_input)
    return error


class PlumConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Plum EcoMAX.

    This class manages the sequence of steps to configure the integration.
    """

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step.

        This method displays the configuration form to the user and validates
        the input by attempting a real read from the boiler before creating
        the configuration entry.

        Args:
            user_input: A dictionary containing the configuration data entered
                by the user. Defaults to None.

        Returns:
            FlowResult: The result of the flow step (either a form to show
            or an entry creation).
        """
        errors = {}
        if user_input is not None:
            error, serial = await _probe_boiler(self.hass, user_input)
            if error:
                errors["base"] = error
            else:
                # Entries created before the serial-number unique_id existed
                # are keyed by IP -- treat the same IP as already configured.
                if any(
                    e.data.get(CONF_IP_ADDRESS) == user_input[CONF_IP_ADDRESS]
                    for e in self._async_current_entries()
                ):
                    return self.async_abort(reason="already_configured")
                await self.async_set_unique_id(serial or user_input[CONF_IP_ADDRESS])
                self._abort_if_unique_id_configured()
                title = f"Boiler ({user_input[CONF_IP_ADDRESS]})"
                return self.async_create_entry(title=title, data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=_build_data_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input=None):
        """Change IP/port/credentials of an existing entry in place.

        The boiler behind the new address must be the same one: its serial
        number has to match the entry's unique_id (when that is serial-based).
        """
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input is not None:
            error, serial = await _probe_boiler(self.hass, user_input)
            if error:
                errors["base"] = error
            else:
                if (
                    serial
                    and entry.unique_id
                    and entry.unique_id
                    not in (
                        serial,
                        entry.data.get(CONF_IP_ADDRESS),
                    )
                ):
                    return self.async_abort(reason="unique_id_mismatch")
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=serial or entry.unique_id,
                    title=f"Boiler ({user_input[CONF_IP_ADDRESS]})",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_build_data_schema(user_input or entry.data),
            errors=errors,
        )
