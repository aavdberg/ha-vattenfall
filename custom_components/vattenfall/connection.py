"""Discover public application configuration and maintain an account-bound login."""

import asyncio
import json
import re
from dataclasses import dataclass
from time import time

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import PortalAuthError, PortalConnectionError, PortalCredentials
from .const import CONF_ACCESS_TOKEN, CONF_SUBSCRIPTION_KEY, CONF_VF_CONTEXT
from .oauth import OAuthError, OAuthTokens, async_refresh, async_verify_identity

_PORTAL = "https://www.vattenfall.nl/service/mijn-vattenfall/"
INITIALIZATION_URL = (
    "https://api.vattenfall.nl/initialisation-api-prd/api/v2/initialisation"
)


@dataclass(frozen=True, repr=False)
class ApplicationKeys:
    """Public frontend keys retrieved at runtime, never embedded in source."""

    contracts: str
    initialization: str


@dataclass(frozen=True, repr=False)
class Account:
    """Only the identifiers needed for explicit account selection."""

    customer_id: str
    contract_account_id: str

    @property
    def context(self) -> str:
        return f"{self.customer_id}:{self.contract_account_id}"


async def _read(
    session: ClientSession,
    url: str,
    limit: int,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> bytes:
    async with session.get(
        url, headers=headers, params=params, allow_redirects=False
    ) as response:
        if response.status in (401, 403):
            raise OAuthError("api_rejected")
        if response.status != 200:
            raise OAuthError("cannot_connect")
        if headers and response.content_type != "application/json":
            raise OAuthError("bootstrap_changed")
        body = bytearray()
        async for chunk in response.content.iter_chunked(8192):
            body.extend(chunk)
            if len(body) > limit:
                raise OAuthError("bootstrap_changed")
        return bytes(body)


async def async_application_keys(session: ClientSession) -> ApplicationKeys:
    """Parse only named configuration fields, never execute provider JavaScript."""
    try:
        async with asyncio.timeout(90):
            html = (await _read(session, _PORTAL, 1_048_576)).decode("utf-8")
            scripts = dict.fromkeys(
                re.findall(
                    r"""(?:src|href)=["'](?:\./)?(chunk-[\w-]+\.js)["']""",
                    html,
                )
            )
            for name in list(scripts)[:12]:
                source = (await _read(session, f"{_PORTAL}{name}", 4_194_304)).decode(
                    "utf-8"
                )
                values = []
                for field in (
                    "cepTouchPointSubscriptionKey",
                    "ocpApimSubscriptionInitialisationKey",
                ):
                    matches = set(
                        re.findall(
                            rf"""\b{field}\s*:\s*["'`]([a-zA-Z0-9]{{16,128}})["'`]""",
                            source,
                        )
                    )
                    if len(matches) != 1:
                        break
                    values.append(matches.pop())
                if len(values) == 2:
                    return ApplicationKeys(*values)
        raise OAuthError("bootstrap_changed")
    except (ClientError, TimeoutError):
        raise OAuthError("cannot_connect") from None
    except UnicodeError:
        raise OAuthError("bootstrap_changed") from None


async def async_accounts(
    session: ClientSession, tokens: OAuthTokens, keys: ApplicationKeys
) -> tuple[Account, ...]:
    """Request only signed customer IDs; discard profile fields immediately."""
    try:
        async with asyncio.timeout(30):
            body = await _read(
                session,
                INITIALIZATION_URL,
                1_048_576,
                headers={
                    "Authorization": f"Bearer {tokens.id_token}",
                    "Ocp-Apim-Subscription-Key": keys.initialization,
                    "Accept": "application/json",
                },
                params={"businessPartnerIds": ",".join(tokens.customer_ids)},
            )
        payload = json.loads(body)
        records = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            raise OAuthError("bootstrap_changed")
        accounts: dict[str, Account] = {}
        for record in records:
            partners = (
                record.get("businessPartners") if isinstance(record, dict) else None
            )
            if not isinstance(partners, list):
                raise OAuthError("bootstrap_changed")
            for partner in partners:
                if not isinstance(partner, dict):
                    raise OAuthError("bootstrap_changed")
                customer = partner.get("businessPartnerId")
                if not isinstance(customer, str) or customer not in tokens.customer_ids:
                    raise OAuthError("wrong_account")
                contracts = partner.get("contractAccounts")
                if not isinstance(contracts, list):
                    raise OAuthError("bootstrap_changed")
                for contract in contracts:
                    account_id = (
                        contract.get("contractAccountId")
                        if isinstance(contract, dict)
                        else None
                    )
                    if (
                        not isinstance(account_id, str)
                        or not account_id.isascii()
                        or not account_id.isdigit()
                        or len(account_id) > 20
                    ):
                        raise OAuthError("bootstrap_changed")
                    account = Account(customer, account_id)
                    accounts[account.context] = account
                    if len(accounts) > 100:
                        raise OAuthError("bootstrap_changed")
        if not accounts:
            raise OAuthError("no_accounts")
        return tuple(accounts.values())
    except (ClientError, TimeoutError):
        raise OAuthError("cannot_connect") from None
    except (ValueError, UnicodeError):
        raise OAuthError("bootstrap_changed") from None


class RenewableSession:
    """Serialize refresh and save rotation before any subsequent API request."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._lock = asyncio.Lock()
        self._verified = False

    async def async_credentials(self) -> PortalCredentials:
        async with self._lock:
            data = self._entry.data
            try:
                tokens = OAuthTokens(
                    data[CONF_ACCESS_TOKEN],
                    data["refresh_token"],
                    data["expires_at"],
                    data["subject"],
                    (),
                    data["scope"],
                    data["nonce"],
                )
                if time() >= tokens.expires_at - 120:
                    tokens = await async_refresh(tokens)
                    self._check_current(data)
                    self._check_account(tokens.customer_ids)
                    self._hass.config_entries.async_update_entry(
                        self._entry, data={**data, **tokens.data()}
                    )
                    self._verified = True
                elif not self._verified:
                    async with ClientSession(
                        cookie_jar=DummyCookieJar(), timeout=ClientTimeout(total=20)
                    ) as session:
                        claims = await async_verify_identity(
                            session,
                            tokens.id_token,
                            tokens.nonce,
                            subject=tokens.subject,
                        )
                    self._check_current(data)
                    self._check_account(claims.customer_ids)
                    self._verified = True
                return PortalCredentials(
                    tokens.id_token,
                    data[CONF_SUBSCRIPTION_KEY],
                    data[CONF_VF_CONTEXT],
                )
            except OAuthError as err:
                if err.outcome in ("cannot_connect", "rate_limited"):
                    raise PortalConnectionError(
                        f"OAuth refresh unavailable ({err.outcome})"
                    ) from None
                raise PortalAuthError(
                    f"Vattenfall login must be renewed ({err.outcome})"
                ) from None
            except (ClientError, TimeoutError):
                raise PortalConnectionError(
                    "Identity verification unavailable"
                ) from None
            except (KeyError, TypeError, ValueError):
                raise PortalAuthError("Invalid stored Vattenfall login") from None

    def _check_account(self, customers: tuple[str, ...]) -> None:
        context = self._entry.data[CONF_VF_CONTEXT]
        parts = context.split(":")
        if len(parts) != 2 or parts[0] not in customers:
            raise OAuthError("wrong_account")

    def _check_current(self, data: object) -> None:
        if (
            self._entry.data is not data
            or self._hass.config_entries.async_get_entry(self._entry.entry_id)
            is not self._entry
        ):
            raise PortalConnectionError(
                "Vattenfall credentials changed during authentication"
            )
