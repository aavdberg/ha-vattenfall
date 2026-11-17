"""Browser PKCE, bounded token transport and a non-persistent diagnostic."""

import asyncio
import base64
import hashlib
import json
import math
from dataclasses import dataclass
from secrets import compare_digest, token_urlsafe
from time import monotonic, time
from typing import Literal, Self
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import jwt
from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from .auth import IDENTITY_ORIGIN, IDENTITY_PATH

# Public OAuth identifier from the Dutch app, not an API key or client secret.
APP_CLIENT_ID = "O3wh5ArTkjOTNAGaKOq33Idbp9ka"
APP_CALLBACK = "vfea://oauth"
AUTHORIZE_URL = f"{IDENTITY_ORIGIN}{IDENTITY_PATH}/oauth2/authorize"
TOKEN_URL = f"{IDENTITY_ORIGIN}{IDENTITY_PATH}/oauth2/token"
OAUTH_TIMEOUT = 600
_MAX_BODY = 65536
JWKS_URL = "https://accounts.vattenfall.com/iamng/oauth2/jwks"
ISSUER = "iamng.corp.vattenfall.com"

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
    nonce: str = ""

    @classmethod
    def create(cls) -> Self:
        return cls(
            state=token_urlsafe(32),
            verifier=token_urlsafe(64),
            scope=f"openid device:{uuid4()}",
            deadline=monotonic() + OAUTH_TIMEOUT,
            nonce=token_urlsafe(32),
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
            "nonce": self.nonce,
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
            if payload.get("error") in ("server_error", "temporarily_unavailable"):
                raise _Stopped("cannot_connect", response.status)
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


class OAuthError(Exception):
    """A fixed classification, never a provider message or token."""

    def __init__(self, outcome: str) -> None:
        self.outcome = outcome
        super().__init__(outcome)


@dataclass(frozen=True, repr=False)
class OAuthTokens:
    """Validated identity and renewable credentials; HA config-entry data only."""

    id_token: str
    refresh_token: str
    expires_at: float
    subject: str
    customer_ids: tuple[str, ...]
    scope: str
    nonce: str

    def data(self) -> dict[str, object]:
        return {
            "auth_method": "oauth",
            "access_token": self.id_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "subject": self.subject,
            "scope": self.scope,
            "nonce": self.nonce,
        }


@dataclass(frozen=True, repr=False)
class VerifiedIdentity:
    """Only verified claims necessary for account binding."""

    subject: str
    customer_ids: tuple[str, ...]
    expires_at: float


async def async_verify_identity(
    session: ClientSession,
    token: str,
    nonce: str,
    *,
    subject: str | None = None,
    access_token: str | None = None,
    initial: bool = False,
) -> VerifiedIdentity:
    """Pin issuer, audience, algorithm and key origin; never trust JWT URLs."""
    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if (
            header.get("alg") != "RS256"
            or not isinstance(kid, str)
            or not kid
            or len(kid) > 256
        ):
            raise OAuthError("identity_invalid")
        async with session.get(JWKS_URL, allow_redirects=False) as response:
            if response.status != 200:
                raise OAuthError("cannot_connect")
            if response.content_type != "application/json":
                raise OAuthError("identity_invalid")
            body = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                body.extend(chunk)
                if len(body) > _MAX_BODY:
                    raise OAuthError("identity_invalid")
        jwks = json.loads(body)
        keys = jwks.get("keys") if isinstance(jwks, dict) else None
        if not isinstance(keys, list) or len(keys) > 50:
            raise OAuthError("identity_invalid")
        candidates = [
            key
            for key in keys
            if isinstance(key, dict)
            and key.get("kid") == kid
            and key.get("kty") == "RSA"
            and key.get("use", "sig") == "sig"
            and key.get("alg", "RS256") == "RS256"
        ]
        if len(candidates) != 1:
            raise OAuthError("identity_invalid")
        key = jwt.PyJWK.from_dict(candidates[0], algorithm="RS256")
        if not isinstance(key.key, RSAPublicKey) or key.key.key_size < 2048:
            raise OAuthError("identity_invalid")
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=APP_CLIENT_ID,
            issuer=ISSUER,
            options={"require": ["iss", "aud", "exp", "iat", "sub"]},
        )
        sub = claims["sub"]
        aud = claims["aud"]
        if (
            not isinstance(sub, str)
            or not sub
            or len(sub) > 1024
            or (subject is not None and sub != subject)
            or (
                ("azp" in claims or (isinstance(aud, list) and len(aud) > 1))
                and claims.get("azp") != APP_CLIENT_ID
            )
            or ((initial or "nonce" in claims) and claims.get("nonce") != nonce)
        ):
            raise OAuthError("identity_invalid")
        for field in ("exp", "iat"):
            value = claims[field]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise OAuthError("identity_invalid")
        if access_token is not None and "at_hash" in claims:
            expected = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(access_token.encode("ascii")).digest()[:16]
                )
                .rstrip(b"=")
                .decode("ascii")
            )
            if claims["at_hash"] != expected:
                raise OAuthError("identity_invalid")
        customers = claims.get("customer_id")
        if (
            not isinstance(customers, list)
            or not 1 <= len(customers) <= 100
            or any(
                not isinstance(value, str)
                or not value.isascii()
                or not value.isdigit()
                or len(value) > 20
                for value in customers
            )
        ):
            raise OAuthError("identity_invalid")
        return VerifiedIdentity(sub, tuple(customers), float(claims["exp"]))
    except (
        jwt.PyJWTError,
        ValueError,
        UnicodeError,
        TypeError,
        KeyError,
        OverflowError,
    ):
        raise OAuthError("identity_invalid") from None


async def _validated_tokens(
    session: ClientSession,
    payload: dict[str, object],
    attempt: OAuthAttempt,
    previous: OAuthTokens | None = None,
) -> OAuthTokens:
    identity = payload.get("id_token")
    refresh = payload.get(
        "refresh_token", previous.refresh_token if previous is not None else None
    )
    if not isinstance(refresh, str) or not refresh or len(refresh) > 32768:
        raise OAuthError("no_refresh_token")
    if not isinstance(identity, str) or not identity or len(identity) > 32768:
        raise OAuthError("identity_invalid")
    access = payload["access_token"]
    assert isinstance(access, str)  # Validated by the bounded token transport.
    claims = await async_verify_identity(
        session,
        identity,
        attempt.nonce,
        subject=previous.subject if previous else None,
        access_token=access,
        initial=previous is None,
    )
    expires = payload["expires_in"]
    assert isinstance(expires, (int, float))
    try:
        expiry = min(claims.expires_at, time() + expires)
    except OverflowError:
        raise OAuthError("identity_invalid") from None
    if not math.isfinite(expiry) or expiry <= time() + 120:
        raise OAuthError("identity_invalid")
    return OAuthTokens(
        identity,
        refresh,
        expiry,
        claims.subject,
        claims.customer_ids,
        attempt.scope,
        attempt.nonce,
    )


async def async_authorize(attempt: OAuthAttempt, callback_url: str) -> OAuthTokens:
    """Consume one code, require verified identity and a renewable token."""
    try:
        code = attempt._callback_code(callback_url)
        async with (
            asyncio.timeout(45),
            ClientSession(
                cookie_jar=DummyCookieJar(), timeout=ClientTimeout(total=20)
            ) as session,
        ):
            payload, _ = await _post_token(
                session,
                {
                    "grant_type": "authorization_code",
                    "client_id": APP_CLIENT_ID,
                    "redirect_uri": APP_CALLBACK,
                    "code": code,
                    "code_verifier": attempt.verifier,
                },
            )
            return await _validated_tokens(session, payload, attempt)
    except _Stopped as stopped:
        raise OAuthError(
            "cannot_connect"
            if stopped.status is not None and stopped.status >= 500
            else stopped.outcome
        ) from None
    except (ClientError, TimeoutError):
        raise OAuthError("cannot_connect") from None


async def async_refresh(tokens: OAuthTokens) -> OAuthTokens:
    """Refresh once; rotation is returned for atomic config-entry persistence."""
    try:
        async with (
            asyncio.timeout(45),
            ClientSession(
                cookie_jar=DummyCookieJar(), timeout=ClientTimeout(total=20)
            ) as session,
        ):
            payload, _ = await _post_token(
                session,
                {
                    "grant_type": "refresh_token",
                    "client_id": APP_CLIENT_ID,
                    "refresh_token": tokens.refresh_token,
                    "scope": tokens.scope,
                },
            )
            attempt = OAuthAttempt("", "", tokens.scope, 0, tokens.nonce)
            return await _validated_tokens(session, payload, attempt, tokens)
    except _Stopped as stopped:
        raise OAuthError(
            "cannot_connect"
            if stopped.status is not None and stopped.status >= 500
            else stopped.outcome
        ) from None
    except (ClientError, TimeoutError):
        raise OAuthError("cannot_connect") from None


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
