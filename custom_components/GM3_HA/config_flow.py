import logging
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.const import CONF_IP_ADDRESS, CONF_PASSWORD, CONF_USERNAME, CONF_PORT
from .const import DOMAIN, CONF_ACTIVE_CIRCUITS, CIRCUIT_CHOICES, DEFAULT_PORT

_LOGGER = logging.getLogger(__name__)

class PlumConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_IP_ADDRESS])
            self._abort_if_unique_id_configured()
            title = f"Chaudière ({user_input[CONF_IP_ADDRESS]})"
            return self.async_create_entry(title=title, data=user_input)

        data_schema = vol.Schema({
            vol.Required(CONF_IP_ADDRESS, default="192.168.1.38"): str,
            vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
            vol.Optional(CONF_USERNAME, default="admin"): str,
            vol.Required(CONF_PASSWORD, default="0000"): str,
            
            # Selecteur traduit
            vol.Required(CONF_ACTIVE_CIRCUITS, default=["2"]): SelectSelector(
                SelectSelectorConfig(
                    # Les labels pointent vers options.1, options.2 dans fr.json
                    options=[{"label": f"options.{k}", "value": k} for k in CIRCUIT_CHOICES],
                    mode=SelectSelectorMode.DROPDOWN,
                    multiple=True,
                    translation_key="circuits_selector"
                )
            ),
        })

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
        )