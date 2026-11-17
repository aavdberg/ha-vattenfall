"""Synthetic password-probe protocol, challenge handling and privacy checks."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientConnectionError, ClientSession

from custom_components.vattenfall.auth import (
    CONTRACT_PAGE,
    IDENTITY_ORIGIN,
    IDENTITY_PATH,
    LOGIN_START,
    LoginResult,
    _PasswordProbe,
    async_test_password,
)

USERNAME = "synthetic@example.invalid"
PASSWORD = " synthetic-password "
LOGIN_PAGE = f"{IDENTITY_ORIGIN}{IDENTITY_PATH}/inloggen?sessionDataKey=synthetic-key"


def response(status=200, payload=None, headers=None, raw=None, content_type=None):
    result = MagicMock()
    result.status = status
    result.headers = headers or {}
    result.content_type = content_type or "application/json"
    body = raw if raw is not None else json.dumps(payload).encode()

    async def chunks(size):
        for offset in range(0, len(body), size):
            yield body[offset : offset + size]

    result.content.iter_chunked.side_effect = chunks
    result.__aenter__ = AsyncMock(return_value=result)
    return result


def session_for(reply):
    session = MagicMock(spec=ClientSession)
    session.get.side_effect = [
        response(),
        response(302, headers={"Location": LOGIN_PAGE}),
        response(),
    ]
    session.post.return_value = reply
    return session


@pytest.mark.parametrize(
    ("status", "payload", "headers", "expected"),
    [
        (400, {"errorMessage": "CAPTCHA rejected"}, {}, "captcha"),
        (400, {}, {"x-vf-iam-error": "CAPTCHA_INVALID"}, "captcha"),
        (200, {"redirect": "/iamng/nlb2c-revamp/mfa-verificatie"}, {}, "mfa"),
        (401, {"isValid": False}, {}, "rejected"),
        (429, {}, {}, "rate_limited"),
        (400, {"isBlocked": True}, {}, "rate_limited"),
        (200, {"isValid": True, "redirect": "/commonauth"}, {}, "accepted"),
        (200, {"redirect": "/commonauth"}, {}, "unexpected_response"),
        (200, None, {}, "unexpected_response"),
        (503, {"errorMessage": PASSWORD}, {}, "unexpected_response"),
    ],
)
async def test_real_form_request_and_safe_classifications(
    status, payload, headers, expected, caplog
):
    session = session_for(response(status, payload, headers))
    result = await _PasswordProbe(session).run(USERNAME, PASSWORD)
    assert result == LoginResult("credentials", status, expected)
    session.post.assert_called_once_with(
        f"{IDENTITY_ORIGIN}{IDENTITY_PATH}/login",
        data={
            "email": USERNAME,
            "password": PASSWORD,
            "sessionDataKey": "synthetic-key",
            "requestDevice": "Home Assistant:server:Linux",
            "tenantDomain": "nl.b2c.customers",
        },
        headers={"Referer": LOGIN_PAGE},
        allow_redirects=False,
    )
    assert session.get.call_args_list[0].args == (CONTRACT_PAGE,)
    assert session.get.call_args_list[1].args == (LOGIN_START,)
    assert all(
        call.kwargs["allow_redirects"] is False for call in session.get.call_args_list
    )
    for secret in (USERNAME, PASSWORD, "synthetic-key"):
        assert secret not in repr(result)
        assert secret not in caplog.text


@pytest.mark.parametrize(
    "location",
    [
        "https://example.invalid/collect",
        "http://accounts.vattenfall.nl/login",
        "https://accounts.vattenfall.nl:444/login",
        "https://user@accounts.vattenfall.nl/login",
        "javascript:alert(1)",
        "",
    ],
)
async def test_redirect_is_validated_before_request_or_credentials(location):
    session = session_for(response())
    session.get.side_effect = [
        response(302, headers={"Location": location}),
    ]
    result = await _PasswordProbe(session).run(USERNAME, PASSWORD)
    assert result.outcome == "unsafe_redirect"
    assert session.get.call_count == 1
    session.post.assert_not_called()


@pytest.mark.parametrize(
    "login",
    [
        f"{IDENTITY_ORIGIN}{IDENTITY_PATH}/inloggen",
        f"{IDENTITY_ORIGIN}{IDENTITY_PATH}/inloggen?sessionDataKey=",
        f"{LOGIN_PAGE}&sessionDataKey=duplicate",
        "https://www.vattenfall.nl/service/mijn-vattenfall/loginprobleem/",
    ],
)
async def test_missing_ambiguous_or_wrong_session_never_posts_password(login):
    session = session_for(response())
    session.get.side_effect = [
        response(),
        response(302, headers={"Location": login}),
        response(),
    ]
    result = await _PasswordProbe(session).run(USERNAME, PASSWORD)
    assert result == LoginResult("authorize", 200, "unexpected_response")
    session.post.assert_not_called()


@pytest.mark.parametrize(
    ("location", "outcome"),
    [
        ("/iamng/nlb2c-revamp/mfa-verificatie", "mfa"),
        ("https://example.invalid/collect", "unsafe_redirect"),
        ("/commonauth", "unexpected_response"),
    ],
)
async def test_post_redirect_never_forwards_password(location, outcome):
    session = session_for(response(307, headers={"Location": location}))
    result = await _PasswordProbe(session).run(USERNAME, PASSWORD)
    assert result.outcome == outcome
    assert session.post.call_count == 1
    assert session.get.call_count == 3


@pytest.mark.parametrize("body", [b"{", b'{"value":"' + b"x" * 65536 + b'"}'])
async def test_malformed_and_oversized_response_is_explicit(body):
    session = session_for(response(raw=body))
    result = await _PasswordProbe(session).run(USERNAME, PASSWORD)
    assert result.outcome == "unexpected_response"


async def test_redirect_loop_is_bounded():
    session = session_for(response())
    session.get.side_effect = None
    session.get.return_value = response(302, headers={"Location": CONTRACT_PAGE})
    result = await _PasswordProbe(session).run(USERNAME, PASSWORD)
    assert result.outcome == "unexpected_response"
    assert session.get.call_count == 8
    session.post.assert_not_called()


@pytest.mark.parametrize(
    "error", [TimeoutError(PASSWORD), ClientConnectionError(PASSWORD)]
)
async def test_network_failure_drops_sensitive_exception_and_clears_cookies(error):
    session = session_for(response())
    session.post.side_effect = error
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=session)
    with (
        patch("custom_components.vattenfall.auth.ClientSession", return_value=manager),
        patch("custom_components.vattenfall.auth.CookieJar") as jar,
    ):
        result = await async_test_password(USERNAME, PASSWORD)
    assert result == LoginResult("credentials", None, "cannot_connect")
    jar.return_value.clear.assert_called_once()
    manager.__aexit__.assert_awaited_once()
    assert PASSWORD not in repr(result)


@pytest.mark.parametrize(
    ("username", "password"), [("", PASSWORD), (USERNAME, ""), (USERNAME, "x" * 4097)]
)
async def test_invalid_input_never_opens_session(username, password):
    with patch("custom_components.vattenfall.auth.ClientSession") as create:
        result = await async_test_password(username, password)
    assert result.outcome == "invalid_input"
    create.assert_not_called()
