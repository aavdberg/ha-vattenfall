"""Read-only client for the undocumented portal contracts endpoint."""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from aiohttp import ClientError, ClientSession

from .const import CONTRACTS_URL
from .models import (
    OverviewData,
    OverviewMetric,
    Tariff,
    parse_installment,
    parse_month,
    parse_supplies,
    parse_tariffs,
    previous_month,
)

_LOGGER = logging.getLogger(__name__)
_API_ROOT = "https://api.vattenfall.nl/api/mvf"
CONSUMPTIONS_URL = f"{_API_ROOT}/consumptions/v1/consumptions"
INSTALLMENT_URL = f"{_API_ROOT}/invoices/v1/TermAmount"


class PortalAuthError(Exception):
    """Session headers are missing, rejected or expired."""


class PortalConnectionError(Exception):
    """The portal is unreachable or returned an unexpected HTTP response."""


@dataclass(frozen=True, repr=False)
class PortalCredentials:
    """Sensitive headers; deliberately excluded from object representations."""

    access_token: str
    subscription_key: str
    vf_context: str

    def headers(self) -> dict[str, str]:
        """Validate input without putting credentials in error messages."""
        token = self.access_token.strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        values = (token, self.subscription_key.strip(), self.vf_context.strip())
        if any(
            not value
            or len(value) > 32768
            or any(ord(char) < 32 or ord(char) > 126 for char in value)
            for value in values
        ):
            raise PortalAuthError("Invalid portal session headers")
        return {
            "Authorization": f"Bearer {values[0]}",
            "Ocp-Apim-Subscription-Key": values[1],
            "Vf-Context": values[2],
            "Accept": "application/json",
        }


class VattenfallClient:
    """Read fixed portal services without redirects or account enumeration."""

    def __init__(self, session: ClientSession, credentials: PortalCredentials) -> None:
        self._session = session
        self._credentials = credentials

    async def async_get_tariffs(self, today: date) -> tuple[Tariff, ...]:
        """Return sanitized tariff models; never log a response or credentials."""
        return parse_tariffs(await self._async_get_json(CONTRACTS_URL), today)

    async def _async_get_json(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        optional: bool = False,
    ) -> object:
        headers = self._credentials.headers()
        try:
            async with asyncio.timeout(30):
                async with self._session.get(
                    url, headers=headers, params=params, allow_redirects=False
                ) as response:
                    if response.status in (401, 403) or 300 <= response.status < 400:
                        raise PortalAuthError("Portal session rejected")
                    if optional and response.status in (204, 404):
                        _LOGGER.warning(
                            "Optional portal overview service unavailable (HTTP %s)",
                            response.status,
                        )
                        return None
                    if response.status != 200:
                        raise PortalConnectionError(
                            f"Portal endpoint returned HTTP {response.status}"
                        )
                    if response.content_type != "application/json":
                        raise PortalConnectionError(
                            "Portal endpoint did not return JSON"
                        )
                    try:
                        payload = await response.json(
                            loads=lambda text: json.loads(text, parse_float=Decimal)
                        )
                    except (ValueError, UnicodeError):
                        raise PortalConnectionError(
                            "Portal endpoint returned invalid JSON"
                        ) from None
                    if payload is None:
                        raise PortalConnectionError(
                            "Portal endpoint returned null JSON"
                        )
                    return payload
        except (ClientError, TimeoutError):
            raise PortalConnectionError("Unable to reach the portal endpoint") from None

    async def async_get_overview(self, today: date) -> OverviewData:
        """Read only summaries, never documents, correspondence or identity data."""
        metrics: list[OverviewMetric] = []
        unavailable: list[str] = []
        for label, url, advice in (
            ("installment", INSTALLMENT_URL, False),
            ("installment_advice", f"{INSTALLMENT_URL}/advice", True),
        ):
            payload = await self._async_get_json(url, optional=True)
            values = (
                parse_installment(payload, advice=advice) if payload is not None else ()
            )
            metrics.extend(values)
            if not values:
                unavailable.append(label)
        payload = await self._async_get_json(CONSUMPTIONS_URL, optional=True)
        if payload is None:
            unavailable.append("consumption")
        else:
            status, supplies = parse_supplies(payload)
            metrics.append(OverviewMetric("contract_status", status))
            start, end = previous_month(today)
            for supply in supplies:
                payload = await self._async_get_json(
                    f"{CONSUMPTIONS_URL}/products/{supply.ean}/Month",
                    params={"startDate": start.isoformat(), "endDate": end.isoformat()},
                    optional=True,
                )
                values = (
                    parse_month(payload, supply, start, end)
                    if payload is not None
                    else ()
                )
                metrics.extend(values)
                if not values:
                    unavailable.append(f"{supply.fuel}_previous_month")
        return OverviewData(tuple(metrics), tuple(unavailable))
