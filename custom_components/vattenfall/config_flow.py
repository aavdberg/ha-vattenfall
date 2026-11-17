"""Portal session import and an explicit, non-persistent password test."""

import hashlib
import logging
from collections.abc import Mapping
from datetime import datetime
from secrets import compare_digest, token_urlsafe
from time import monotonic
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .api import (
    PortalAuthError,
    PortalConnectionError,
    PortalCredentials,
    VattenfallClient,
)
from .auth import LoginResult, async_test_password
from .connection import Account, async_accounts, async_application_keys
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_SUBSCRIPTION_KEY,
    CONF_VF_CONTEXT,
    CONTRACT_TIMEZONE,
    DOMAIN,
)
from .models import InvalidTariffData
from .oauth import (
    ISSUER,
    OAUTH_TIMEOUT,
    OAuthAttempt,
    OAuthError,
    OAuthTestResult,
    OAuthTokens,
    async_authorize,
    async_test_oauth,
)

_SECRET_FIELDS = (CONF_ACCESS_TOKEN, CONF_SUBSCRIPTION_KEY, CONF_VF_CONTEXT)
BROWSER_TIMEOUT = 600
_LOGGER = logging.getLogger(__name__)


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

    def __init__(self) -> None:
        super().__init__()
        self._browser_nonce: str | None = None
        self._browser_data: dict[str, str] | None = None
        self._browser_deadline = 0.0
        self._browser_cancel: CALLBACK_TYPE | None = None
        self._password_result: LoginResult | None = None
        self._oauth_attempt: OAuthAttempt | None = None
        self._oauth_cancel: CALLBACK_TYPE | None = None
        self._oauth_result: OAuthTestResult | None = None
        self._login_tokens: OAuthTokens | None = None
        self._login_accounts: tuple[Account, ...] = ()
        self._login_key: str | None = None
        self._login_deadline = 0.0
        self._login_generation = 0

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_menu(
                step_id="user",
                menu_options=["login", "oauth", "password", "browser", "manual"],
            )
        return await self.async_step_manual(user_input)

    @callback
    def _clear_login(self, _now: datetime | None = None) -> None:
        self._clear_oauth()
        self._login_tokens = None
        self._login_accounts = ()
        self._login_key = None
        self._login_deadline = 0.0
        self._login_generation += 1

    @callback
    def _login_form(self, error: str | None = None) -> ConfigFlowResult:
        self._clear_login()
        self._oauth_attempt = OAuthAttempt.create()
        self._login_deadline = self._oauth_attempt.deadline
        self._oauth_cancel = async_call_later(
            self.hass, OAUTH_TIMEOUT, self._clear_login
        )
        return self.async_show_form(
            step_id="login",
            data_schema=vol.Schema(
                {
                    vol.Required("callback_url"): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    )
                }
            ),
            errors={"base": f"connect_{error}"} if error else {},
            description_placeholders={
                "authorize_url": self._oauth_attempt.authorization_url()
            },
        )

    def _target_entry(self) -> config_entries.ConfigEntry | None:
        if self.context["source"] == config_entries.SOURCE_REAUTH:
            return self._get_reauth_entry()
        if self.context["source"] == config_entries.SOURCE_RECONFIGURE:
            return self._get_reconfigure_entry()
        return None

    async def async_step_login(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self._login_form()
        attempt = self._oauth_attempt
        self._oauth_attempt = None
        if attempt is None:
            return self._login_form("expired")
        generation = self._login_generation
        try:
            tokens = await async_authorize(attempt, user_input["callback_url"])
            session = async_get_clientsession(self.hass)
            keys = await async_application_keys(session)
            accounts = await async_accounts(session, tokens, keys)
            if generation != self._login_generation:
                return self.async_abort(reason="login_expired")
            if monotonic() >= self._login_deadline:
                raise OAuthError("expired")
            if entry := self._target_entry():
                if (
                    entry.data.get("auth_method") == "oauth"
                    and entry.data["subject"] != tokens.subject
                ):
                    raise OAuthError("wrong_account")
                accounts = tuple(
                    account
                    for account in accounts
                    if account.context == entry.data[CONF_VF_CONTEXT]
                )
                if not accounts:
                    raise OAuthError("wrong_account")
            self._login_tokens = tokens
            self._login_accounts = accounts
            self._login_key = keys.contracts
            return await self.async_step_account()
        except OAuthError as err:
            if generation != self._login_generation:
                return self.async_abort(reason="login_expired")
            _LOGGER.warning("Vattenfall browser connection: %s", err.outcome)
            return self._login_form(err.outcome)

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        tokens = self._login_tokens
        key = self._login_key
        if tokens is None or key is None or monotonic() >= self._login_deadline:
            return self._login_form("expired")
        choices = {
            account.context: f"{account.customer_id} / {account.contract_account_id}"
            for account in self._login_accounts
        }
        error = None
        if user_input is not None:
            context = user_input.get(CONF_VF_CONTEXT)
            if not isinstance(context, str) or context not in choices:
                error = "connect_wrong_account"
            else:
                data = {
                    CONF_ACCESS_TOKEN: tokens.id_token,
                    CONF_SUBSCRIPTION_KEY: key,
                    CONF_VF_CONTEXT: context,
                }
                error = await _validate(self.hass, data)
                if (
                    monotonic() >= self._login_deadline
                    or self._login_tokens is not tokens
                ):
                    return self._login_form("expired")
                if error is None:
                    unique_id = hashlib.sha256(
                        f"{ISSUER}\0{tokens.subject}\0{context}".encode()
                    ).hexdigest()
                    await self.async_set_unique_id(unique_id)
                    entry = self._target_entry()
                    if any(
                        candidate.unique_id == unique_id
                        and (entry is None or candidate.entry_id != entry.entry_id)
                        for candidate in self._async_current_entries()
                    ):
                        self._clear_login()
                        return self.async_abort(reason="already_configured")
                    credentials = {
                        **tokens.data(),
                        CONF_SUBSCRIPTION_KEY: key,
                        CONF_VF_CONTEXT: context,
                    }
                    self._clear_login()
                    if entry is not None:
                        return self.async_update_reload_and_abort(
                            entry, unique_id=unique_id, data=credentials
                        )
                    return self.async_create_entry(
                        title="Mijn Vattenfall", data=credentials
                    )
        return self.async_show_form(
            step_id="account",
            data_schema=vol.Schema({vol.Required(CONF_VF_CONTEXT): vol.In(choices)}),
            errors={"base": error} if error else {},
        )

    @callback
    def _clear_oauth(self, _now: datetime | None = None) -> None:
        if self._oauth_cancel:
            self._oauth_cancel()
            self._oauth_cancel = None
        self._oauth_attempt = None

    async def async_step_oauth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            attempt = self._oauth_attempt
            self._clear_login()
            if attempt is None:
                self._oauth_result = OAuthTestResult("callback", None, "expired")
            else:
                self._oauth_result = await async_test_oauth(
                    attempt, user_input["callback_url"]
                )
            result = self._oauth_result
            _LOGGER.warning(
                "Experimental OAuth test: stage=%s http=%s outcome=%s",
                result.stage,
                result.status if result.status is not None else "none",
                result.outcome,
            )
            return await self.async_step_oauth_result()
        self._clear_login()
        self._oauth_result = None
        self._oauth_attempt = OAuthAttempt.create()
        self._oauth_cancel = async_call_later(
            self.hass, OAUTH_TIMEOUT, self._clear_oauth
        )
        return self.async_show_form(
            step_id="oauth",
            data_schema=vol.Schema(
                {
                    vol.Required("callback_url"): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    )
                }
            ),
            description_placeholders={
                "authorize_url": self._oauth_attempt.authorization_url()
            },
        )

    async def async_step_oauth_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        result = self._oauth_result
        if user_input is not None or result is None:
            return await self.async_step_oauth()
        return self.async_show_form(
            step_id="oauth_result",
            data_schema=vol.Schema({}),
            errors={"base": f"oauth_{result.outcome}"},
            description_placeholders={
                "stage": result.stage,
                "status": str(result.status) if result.status is not None else "-",
            },
        )

    async def async_step_password(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._password_result = await async_test_password(
                user_input["username"], user_input["password"]
            )
            result = self._password_result
            _LOGGER.warning(
                "Experimental password login: stage=%s http=%s outcome=%s",
                result.stage,
                result.status if result.status is not None else "none",
                result.outcome,
            )
            return await self.async_step_password_result()
        self._password_result = None
        self._clear_login()
        self._oauth_result = None
        return self.async_show_form(
            step_id="password",
            data_schema=vol.Schema(
                {
                    vol.Required("username"): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.EMAIL
                        )
                    ),
                    vol.Required("password"): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    ),
                }
            ),
        )

    async def async_step_password_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        result = self._password_result
        if user_input is not None or result is None:
            return await self.async_step_password()
        return self.async_show_form(
            step_id="password_result",
            data_schema=vol.Schema({}),
            errors={"base": f"password_{result.outcome}"},
            description_placeholders={
                "stage": result.stage,
                "status": str(result.status) if result.status is not None else "-",
            },
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._clear_login()
        errors: dict[str, str] = {}
        if user_input is not None:
            if error := await _validate(self.hass, user_input):
                errors["base"] = error
            else:
                return self._finish(user_input)
        return self.async_show_form(
            step_id="manual", data_schema=_schema(), errors=errors
        )

    @callback
    def _finish(self, data: Mapping[str, str]) -> ConfigFlowResult:
        credentials = {key: data[key] for key in _SECRET_FIELDS}
        if self.context["source"] == config_entries.SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data=credentials
            )
        if self.context["source"] == config_entries.SOURCE_RECONFIGURE:
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(), data=credentials
            )
        return self.async_create_entry(title="Mijn Vattenfall", data=credentials)

    @callback
    def _clear_browser(self, _now: datetime | None = None) -> None:
        if self._browser_cancel:
            self._browser_cancel()
            self._browser_cancel = None
        self._browser_data = None
        self._browser_nonce = None
        self._browser_deadline = 0.0

    @callback
    def async_remove(self) -> None:
        self._clear_browser()
        self._password_result = None
        self._clear_oauth()
        self._clear_login()
        self._oauth_result = None

    @callback
    def _browser_form(self, errors: dict[str, str] | None = None) -> ConfigFlowResult:
        if monotonic() >= self._browser_deadline:
            self._clear_browser()
        if self._browser_nonce is None:
            self._browser_nonce = token_urlsafe(32)
            self._browser_deadline = monotonic() + BROWSER_TIMEOUT
            self._browser_cancel = async_call_later(
                self.hass, BROWSER_TIMEOUT, self._clear_browser
            )
        return self.async_show_form(
            step_id="browser",
            # The extension supplies an authenticated, strictly checked payload;
            # no credential input controls are rendered in the browser-login UI.
            data_schema=vol.Schema({}, extra=vol.ALLOW_EXTRA),
            errors=errors or {},
            description_placeholders={
                "pairing_code": f"VFEDGE.{self.flow_id}.{self._browser_nonce}"
            },
        )

    async def async_step_browser(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._clear_login()
        if not user_input:
            return self._browser_form(
                {"base": "browser_pending"} if user_input is not None else None
            )
        if monotonic() >= self._browser_deadline or self._browser_nonce is None:
            return self._browser_form({"base": "browser_timeout"})
        if (
            set(user_input) != {*_SECRET_FIELDS, "browser_session"}
            or not all(isinstance(value, str) for value in user_input.values())
            or len(user_input["browser_session"]) != 43
            or not user_input["browser_session"].isascii()
            or not compare_digest(user_input["browser_session"], self._browser_nonce)
        ):
            return self._browser_form({"base": "invalid_browser_session"})
        data = {key: user_input[key] for key in _SECRET_FIELDS}
        try:
            PortalCredentials(*(data[key] for key in _SECRET_FIELDS)).headers()
        except PortalAuthError:
            return self._browser_form({"base": "invalid_auth"})
        self._browser_data = data
        self._browser_nonce = None
        return await self.async_step_browser_confirm()

    async def async_step_browser_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._browser_data is None or monotonic() >= self._browser_deadline:
            self._clear_browser()
            return self._browser_form({"base": "browser_timeout"})
        if user_input is not None:
            data = self._browser_data
            self._clear_browser()
            if error := await _validate(self.hass, data):
                return self._browser_form({"base": error})
            return self._finish(data)
        return self.async_show_form(
            step_id="browser_confirm", data_schema=vol.Schema({})
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="reauth",
            menu_options=["login", "oauth", "password", "browser", "reauth_confirm"],
        )

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if error := await _validate(self.hass, user_input):
                errors["base"] = error
            else:
                return self._finish(user_input)
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=_schema(), errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_menu(
                step_id="reconfigure",
                menu_options=["login", "oauth", "password", "browser", "manual"],
            )
        errors: dict[str, str] = {}
        if user_input is not None:
            if error := await _validate(self.hass, user_input):
                errors["base"] = error
            else:
                return self._finish(user_input)
        return self.async_show_form(
            step_id="reconfigure", data_schema=_schema(), errors=errors
        )
