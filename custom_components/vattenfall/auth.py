"""Experimental password-login probe; never persist credentials or sessions."""

import asyncio
import json
from dataclasses import dataclass
from typing import Literal

from aiohttp import ClientError, ClientSession, ClientTimeout, CookieJar
from yarl import URL

CONTRACT_PAGE = (
    "https://www.vattenfall.nl/service/mijn-vattenfall/vf/direct-regelen/contract/"
)
LOGIN_START = "https://www.vattenfall.nl/mvloginprd/service/mijn-vattenfall/api/login"
IDENTITY_ORIGIN = "https://accounts.vattenfall.nl"
IDENTITY_PATH = "/iamng/nlb2c-revamp"
_HOSTS = {"www.vattenfall.nl", "accounts.vattenfall.nl"}
_REDIRECTS = {301, 302, 303, 307, 308}
_MAX_BODY = 65536

type LoginStage = Literal["portal", "authorize", "credentials"]
type LoginOutcome = Literal[
    "invalid_input",
    "captcha",
    "mfa",
    "rejected",
    "rate_limited",
    "accepted",
    "unexpected_response",
    "unsafe_redirect",
    "cannot_connect",
]


@dataclass(frozen=True)
class LoginResult:
    """Only locally defined classifications and an HTTP status may escape."""

    stage: LoginStage
    status: int | None
    outcome: LoginOutcome


def _safe_url(value: str, base: URL) -> URL | None:
    try:
        target = base.join(URL(value))
        if (
            target.scheme == "https"
            and target.host in _HOSTS
            and target.port == 443
            and target.user is None
            and target.password is None
            and not target.fragment
        ):
            return target
    except ValueError:
        pass
    return None


class _Stopped(Exception):
    """Control flow carrying no server text or credentials."""

    def __init__(self, outcome: LoginOutcome) -> None:
        self.outcome = outcome
        super().__init__(outcome)


class _PasswordProbe:
    def __init__(self, session: ClientSession) -> None:
        self.session = session
        self.stage: LoginStage = "portal"
        self.status: int | None = None

    async def _navigate(self, start: str) -> URL:
        current = URL(start)
        for _ in range(8):
            self.status = None
            async with self.session.get(
                str(current),
                headers={"Referer": CONTRACT_PAGE},
                allow_redirects=False,
            ) as response:
                self.status = response.status
                if response.status == 429:
                    raise _Stopped("rate_limited")
                if response.status in _REDIRECTS:
                    target = _safe_url(response.headers.get("Location", ""), current)
                    if not response.headers.get("Location") or target is None:
                        raise _Stopped("unsafe_redirect")
                    current = target
                    continue
                if response.status != 200:
                    raise _Stopped("unexpected_response")
                return current
        raise _Stopped("unexpected_response")

    async def run(self, username: str, password: str) -> LoginResult:
        try:
            async with asyncio.timeout(60):
                await self._navigate(CONTRACT_PAGE)
                self.stage = "authorize"
                login = await self._navigate(LOGIN_START)
                keys = login.query.getall("sessionDataKey", [])
                if (
                    login.origin() != URL(IDENTITY_ORIGIN)
                    or login.path != f"{IDENTITY_PATH}/inloggen"
                    or len(keys) != 1
                    or not keys[0]
                    or len(keys[0]) > 4096
                ):
                    raise _Stopped("unexpected_response")
                self.stage = "credentials"
                self.status = None
                async with self.session.post(
                    f"{IDENTITY_ORIGIN}{IDENTITY_PATH}/login",
                    data={
                        "email": username,
                        "password": password,
                        "sessionDataKey": keys[0],
                        "requestDevice": "Home Assistant:server:Linux",
                        "tenantDomain": "nl.b2c.customers",
                    },
                    headers={"Referer": str(login)},
                    allow_redirects=False,
                ) as response:
                    self.status = response.status
                    if response.status == 429:
                        raise _Stopped("rate_limited")
                    if response.status in _REDIRECTS:
                        target = _safe_url(response.headers.get("Location", ""), login)
                        if target is None or not response.headers.get("Location"):
                            raise _Stopped("unsafe_redirect")
                        if "mfa-verificatie" in target.path.split("/"):
                            raise _Stopped("mfa")
                        raise _Stopped("unexpected_response")
                    if response.content_type != "application/json":
                        if response.status in (401, 403):
                            raise _Stopped("rejected")
                        raise _Stopped("unexpected_response")
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(8192):
                        body.extend(chunk)
                        if len(body) > _MAX_BODY:
                            raise _Stopped("unexpected_response")
                    try:
                        payload = json.loads(body)
                    except (ValueError, UnicodeError):
                        raise _Stopped("unexpected_response") from None
                    if not isinstance(payload, dict):
                        raise _Stopped("unexpected_response")
                    if payload.get("isBlocked") is True:
                        raise _Stopped("rate_limited")
                    message = payload.get("errorMessage")
                    error = response.headers.get("x-vf-iam-error", "").lower()
                    if isinstance(message, str):
                        error += message.lower()
                    if "captcha" in error:
                        raise _Stopped("captcha")
                    redirect = payload.get("redirect")
                    target = (
                        _safe_url(redirect, login)
                        if isinstance(redirect, str) and redirect
                        else None
                    )
                    if redirect and target is None:
                        raise _Stopped("unsafe_redirect")
                    if target and "mfa-verificatie" in target.path.split("/"):
                        raise _Stopped("mfa")
                    if response.status in (401, 403) or payload.get("isValid") is False:
                        raise _Stopped("rejected")
                    if (
                        response.status == 200
                        and payload.get("isValid") is True
                        and target is not None
                        and not message
                    ):
                        raise _Stopped("accepted")
                    raise _Stopped("unexpected_response")
        except _Stopped as stopped:
            return LoginResult(self.stage, self.status, stopped.outcome)
        except (ClientError, TimeoutError):
            return LoginResult(self.stage, self.status, "cannot_connect")


async def async_test_password(username: str, password: str) -> LoginResult:
    """Make one explicit attempt with an isolated, short-lived cookie jar."""
    if (
        not isinstance(username, str)
        or not isinstance(password, str)
        or not username.strip()
        or len(username) > 254
        or not password
        or len(password) > 4096
    ):
        return LoginResult("portal", None, "invalid_input")
    jar = CookieJar()
    try:
        async with ClientSession(
            cookie_jar=jar, timeout=ClientTimeout(total=15)
        ) as session:
            return await _PasswordProbe(session).run(username.strip(), password)
    finally:
        jar.clear()
