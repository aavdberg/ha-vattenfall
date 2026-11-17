"""One-use, browser-authorized PKCE and refresh test; no stored tokens."""

import asyncio
import base64
import hashlib
import json
import math
from dataclasses import dataclass
from secrets import compare_digest, token_urlsafe
from time import monotonic
from typing import Literal, Self
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar

from .auth import IDENTITY_ORIGIN, IDENTITY_PATH

# Public OAuth identifier from the Dutch app, not an API key or client secret.
APP_CLIENT_ID = "O3wh5ArTkjOTNAGaKOq33Idbp9ka"
APP_CALLBACK = "vfea://oauth"
AUTHORIZE_URL = f"{IDENTITY_ORIGIN}{IDENTITY_PATH}/oauth2/authorize"
TOKEN_URL = f"{IDENTITY_ORIGIN}{IDENTITY_PATH}/oauth2/token"
OAUTH_TIMEOUT = 600
_MAX_BODY = 65536

type OAuthStage = Literal["callback", "exchange", "refresh"]
type OAuthOutcome = Literal[
    "invalid_callback",
    "expired",
    "provider_denied",
    "client_rejected",
    "grant_rejected",
    "rate_limited",
    "unexpected_response",
    "cannot_connect",
    "no_refresh_token",
    "refresh_succeeded",
]


@dataclass(frozen=True)
class OAuthTestResult:
    """Only these health fields may be returned to HA or logged."""

    stage: OAuthStage
    status: int | None
    outcome: OAuthOutcome


class _Stopped(Exception):
    def __init__(self, outcome: OAuthOutcome, status: int | None = None) -> None:
        self.outcome = outcome
        self.status = status
        super().__init__(outcome)


@dataclass(frozen=True, repr=False)
class OAuthAttempt:
    """Keep only short-lived PKCE state in the flow, never a response or token."""

    state: str
    verifier: str
    scope: str
    deadline: float

    @classmethod
    def create(cls) -> Self:
        return cls(
            state=token_urlsafe(32),
            verifier=token_urlsafe(64),
            scope=f"openid device:{uuid4()}",
            deadline=monotonic() + OAUTH_TIMEOUT,
        )

    def authorization_url(self) -> str:
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(self.verifier.encode("ascii")).digest()
        ).rstrip(b"=")
        params = {
            "response_type": "code",
            "client_id": APP_CLIENT_ID,
            "redirect_uri": APP_CALLBACK,
            "scope": self.scope,
            "state": self.state,
            "code_challenge": challenge.decode("ascii"),
            "code_challenge_method": "S256",
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    def _callback_code(self, value: str) -> str:
        if monotonic() >= self.deadline:
            raise _Stopped("expired")
        if not isinstance(value, str) or not value or len(value) > 32768:
            raise _Stopped("invalid_callback")
        try:
            callback = urlsplit(value.strip())
            params = parse_qs(
                callback.query,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=10,
            )
        except ValueError:
            raise _Stopped("invalid_callback") from None
        if (
            callback.scheme != "vfea"
            or callback.netloc != "oauth"
            or callback.path
            or callback.fragment
            or any(len(values) != 1 for values in params.values())
        ):
            raise _Stopped("invalid_callback")
        state = params.get("state", [""])[0]
        if (
            not state.isascii()
            or len(state) != len(self.state)
            or not compare_digest(state, self.state)
        ):
            raise _Stopped("invalid_callback")
        if "error" in params:
            raise _Stopped("provider_denied")
        code = params.get("code", [""])[0]
        if not code or len(code) > 8192 or not code.isascii():
            raise _Stopped("invalid_callback")
        return code


async def _post_token(
    session: ClientSession, data: dict[str, str]
) -> tuple[dict[str, object], int]:
    async with session.post(TOKEN_URL, data=data, allow_redirects=False) as response:
        if response.status == 429:
            raise _Stopped("rate_limited", response.status)
        if response.status == 401:
            raise _Stopped("client_rejected", response.status)
        if response.content_type != "application/json":
            raise _Stopped("unexpected_response", response.status)
        body = bytearray()
        async for chunk in response.content.iter_chunked(8192):
            body.extend(chunk)
            if len(body) > _MAX_BODY:
                raise _Stopped("unexpected_response", response.status)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            raise _Stopped("unexpected_response", response.status) from None
        if not isinstance(payload, dict):
            raise _Stopped("unexpected_response", response.status)
        if response.status != 200:
            if payload.get("error") in ("invalid_client", "unauthorized_client"):
                raise _Stopped("client_rejected", response.status)
            if payload.get("error") in ("invalid_grant", "access_denied"):
                raise _Stopped("grant_rejected", response.status)
            raise _Stopped("unexpected_response", response.status)
        token = payload.get("access_token")
        token_type = payload.get("token_type")
        expires = payload.get("expires_in")
        if (
            "error" in payload
            or not isinstance(token, str)
            or not token
            or len(token) > 32768
            or not isinstance(token_type, str)
            or token_type.lower() != "bearer"
            or isinstance(expires, bool)
            or not isinstance(expires, (int, float))
            or (isinstance(expires, float) and not math.isfinite(expires))
            or expires <= 0
        ):
            raise _Stopped("unexpected_response", response.status)
        return payload, response.status


async def async_test_oauth(attempt: OAuthAttempt, callback_url: str) -> OAuthTestResult:
    """Exchange once, refresh once, then discard all tokens; never fetch identity."""
    stage: OAuthStage = "callback"
    status: int | None = None
    try:
        code = attempt._callback_code(callback_url)
        stage = "exchange"
        async with (
            asyncio.timeout(45),
            ClientSession(
                cookie_jar=DummyCookieJar(), timeout=ClientTimeout(total=20)
            ) as session,
        ):
            payload, status = await _post_token(
                session,
                {
                    "grant_type": "authorization_code",
                    "client_id": APP_CLIENT_ID,
                    "redirect_uri": APP_CALLBACK,
                    "code": code,
                    "code_verifier": attempt.verifier,
                },
            )
            refresh = payload.get("refresh_token")
            if refresh is None:
                return OAuthTestResult(stage, status, "no_refresh_token")
            if not isinstance(refresh, str) or not refresh or len(refresh) > 32768:
                raise _Stopped("unexpected_response", status)
            stage = "refresh"
            status = None
            _, status = await _post_token(
                session,
                {
                    "grant_type": "refresh_token",
                    "client_id": APP_CLIENT_ID,
                    "refresh_token": refresh,
                    "scope": attempt.scope,
                },
            )
            return OAuthTestResult(stage, status, "refresh_succeeded")
    except _Stopped as stopped:
        return OAuthTestResult(stage, stopped.status, stopped.outcome)
    except (ClientError, TimeoutError):
        return OAuthTestResult(stage, status, "cannot_connect")
