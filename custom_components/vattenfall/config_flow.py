"""Manual session import and replacement, without handling a portal password."""

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .api import (
    PortalAuthError,
    PortalConnectionError,
    PortalCredentials,
    VattenfallClient,
)
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_SUBSCRIPTION_KEY,
    CONF_VF_CONTEXT,
    CONTRACT_TIMEZONE,
    DOMAIN,
)
from .models import InvalidTariffData

_SECRET_FIELDS = (CONF_ACCESS_TOKEN, CONF_SUBSCRIPTION_KEY, CONF_VF_CONTEXT)


def _schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(key): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            )
            for key in _SECRET_FIELDS
        }
    )


async def _validate(hass: HomeAssistant, data: Mapping[str, str]) -> str | None:
    client = VattenfallClient(
        async_get_clientsession(hass),
        PortalCredentials(*(data[key] for key in _SECRET_FIELDS)),
    )
    try:
        await client.async_get_tariffs(
            dt_util.utcnow().astimezone(CONTRACT_TIMEZONE).date()
        )
    except PortalAuthError:
        return "invalid_auth"
    except PortalConnectionError:
        return "cannot_connect"
    except InvalidTariffData:
        return "invalid_tariffs"
    return None


class VattenfallConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure a private portal session; never prefill credential forms."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if error := await _validate(self.hass, user_input):
                errors["base"] = error
            else:
                return self.async_create_entry(
                    title="Mijn Vattenfall",
                    data={key: user_input[key] for key in _SECRET_FIELDS},
                )
        return self.async_show_form(
            step_id="user", data_schema=_schema(), errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if error := await _validate(self.hass, user_input):
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data_updates={key: user_input[key] for key in _SECRET_FIELDS},
                )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=_schema(), errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if error := await _validate(self.hass, user_input):
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    self._get_reconfigure_entry(),
                    data_updates={key: user_input[key] for key in _SECRET_FIELDS},
                )
        return self.async_show_form(
            step_id="reconfigure", data_schema=_schema(), errors=errors
        )
