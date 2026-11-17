"""Read-only client for the undocumented portal contracts endpoint."""

import asyncio
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from aiohttp import ClientError, ClientSession

from .const import CONTRACTS_URL
from .models import Tariff, parse_tariffs


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
    """Fetch only contract tariffs, without redirects or account enumeration."""

    def __init__(self, session: ClientSession, credentials: PortalCredentials) -> None:
        self._session = session
        self._credentials = credentials

    async def async_get_tariffs(self, today: date) -> tuple[Tariff, ...]:
        """Return sanitized tariff models; never log a response or credentials."""
        headers = self._credentials.headers()
        try:
            async with asyncio.timeout(30):
                async with self._session.get(
                    CONTRACTS_URL, headers=headers, allow_redirects=False
                ) as response:
                    if response.status in (401, 403) or 300 <= response.status < 400:
                        raise PortalAuthError("Portal session rejected")
                    if response.status != 200:
                        raise PortalConnectionError(
                            f"Contract endpoint returned HTTP {response.status}"
                        )
                    if response.content_type != "application/json":
                        raise PortalConnectionError(
                            "Contract endpoint did not return JSON"
                        )
                    try:
                        payload = await response.json(
                            loads=lambda text: json.loads(text, parse_float=Decimal)
                        )
                    except (ValueError, UnicodeError):
                        raise PortalConnectionError(
                            "Contract endpoint returned invalid JSON"
                        ) from None
        except (ClientError, TimeoutError):
            raise PortalConnectionError(
                "Unable to reach the contract endpoint"
            ) from None
        return parse_tariffs(payload, today)
