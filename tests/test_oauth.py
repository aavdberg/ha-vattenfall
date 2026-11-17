"""Synthetic browser authorization, PKCE exchange and one-shot refresh tests."""

import base64
import hashlib
import json
from time import monotonic
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from aiohttp import ClientConnectionError, ClientSession

from custom_components.vattenfall.oauth import (
    APP_CALLBACK,
    APP_CLIENT_ID,
    TOKEN_URL,
    OAuthAttempt,
    OAuthTestResult,
    async_test_oauth,
)

from .test_auth import response


def callback(attempt, **params):
    return f"{APP_CALLBACK}?{
        urlencode(
            {
                'code': 'synthetic-authorization-code',
                'state': attempt.state,
                **params,
            }
        )
    }"


def token(**updates):
    return {
        "access_token": "synthetic-access-token",
        "refresh_token": "synthetic-refresh-token",
        "token_type": "Bearer",
        "expires_in": 3600,
        **updates,
    }


def mock_session(*responses):
    session = MagicMock(spec=ClientSession)
    session.post.side_effect = responses
    session.__aenter__ = AsyncMock(return_value=session)
    return session


def test_generated_authorization_is_bound_to_s256_state_and_registered_callback():
    attempt = OAuthAttempt.create()
    query = parse_qs(urlsplit(attempt.authorization_url()).query)
    assert query["client_id"] == [APP_CLIENT_ID]
    assert query["redirect_uri"] == [APP_CALLBACK]
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [
        base64.urlsafe_b64encode(
            hashlib.sha256(attempt.verifier.encode("ascii")).digest()
        )
        .decode()
        .rstrip("=")
    ]
    assert query["state"] == [attempt.state]
    assert query["scope"] == [attempt.scope]
    assert attempt.verifier not in attempt.authorization_url()
    assert not {"ismobile", "isCapacitor", "client_secret"} & query.keys()
    other = OAuthAttempt.create()
    assert other.state != attempt.state
    assert other.verifier != attempt.verifier
    assert other.scope != attempt.scope
    assert attempt.state not in repr(attempt)


async def test_exchange_refresh_and_cleanup_return_health_not_tokens(caplog):
    attempt = OAuthAttempt.create()
    session = mock_session(response(payload=token()), response(payload=token()))
    with patch(
        "custom_components.vattenfall.oauth.ClientSession", return_value=session
    ):
        result = await async_test_oauth(attempt, callback(attempt))
    assert result == OAuthTestResult("refresh", 200, "refresh_succeeded")
    assert session.post.call_count == 2
    first, second = session.post.call_args_list
    assert first.args == second.args == (TOKEN_URL,)
    assert first.kwargs == {
        "data": {
            "grant_type": "authorization_code",
            "client_id": APP_CLIENT_ID,
            "redirect_uri": APP_CALLBACK,
            "code": "synthetic-authorization-code",
            "code_verifier": attempt.verifier,
        },
        "allow_redirects": False,
    }
    assert second.kwargs == {
        "data": {
            "grant_type": "refresh_token",
            "client_id": APP_CLIENT_ID,
            "refresh_token": "synthetic-refresh-token",
            "scope": attempt.scope,
        },
        "allow_redirects": False,
    }
    session.__aexit__.assert_awaited_once()
    for secret in [*token().values(), attempt.verifier, attempt.state]:
        if isinstance(secret, str):
            assert secret not in repr(result)
            assert secret not in caplog.text


@pytest.mark.parametrize(
    "modify",
    [
        lambda value: value.replace("vfea:", "https:"),
        lambda value: value.replace("oauth?", "other?"),
        lambda value: value.replace("oauth?", "user@oauth?"),
        lambda value: value.replace("oauth?", "oauth:443?"),
        lambda value: value.replace("oauth?", "oauth/path?"),
        lambda value: value + "#fragment",
        lambda value: value + "&state=duplicate",
        lambda value: value + "&code=duplicate",
        lambda value: value.split("&state=")[0],
        lambda value: value.replace("synthetic-authorization-code", ""),
        lambda value: "x" * 32769,
    ],
)
async def test_invalid_callback_never_sends_a_token_request(modify):
    attempt = OAuthAttempt.create()
    with patch("custom_components.vattenfall.oauth.ClientSession") as create:
        result = await async_test_oauth(attempt, modify(callback(attempt)))
    assert result == OAuthTestResult("callback", None, "invalid_callback")
    create.assert_not_called()


async def test_other_attempt_state_expiry_and_provider_error_stop_before_exchange():
    attempt = OAuthAttempt.create()
    with patch("custom_components.vattenfall.oauth.ClientSession") as create:
        wrong = await async_test_oauth(attempt, callback(OAuthAttempt.create()))
        assert wrong.outcome == "invalid_callback"
        expired = OAuthAttempt(
            state=attempt.state,
            verifier=attempt.verifier,
            scope=attempt.scope,
            deadline=monotonic() - 1,
        )
        assert (await async_test_oauth(expired, callback(attempt))).outcome == "expired"
        denied = await async_test_oauth(
            attempt,
            f"{APP_CALLBACK}?{
                urlencode(
                    {
                        'state': attempt.state,
                        'error': 'access_denied',
                        'error_description': 'SENSITIVE_PROVIDER_TEXT',
                    }
                )
            }",
        )
        assert denied.outcome == "provider_denied"
        assert "SENSITIVE_PROVIDER_TEXT" not in repr(denied)
    create.assert_not_called()


@pytest.mark.parametrize(
    ("status", "payload", "outcome"),
    [
        (401, {}, "client_rejected"),
        (400, {"error": "invalid_client"}, "client_rejected"),
        (400, {"error": "invalid_grant"}, "grant_rejected"),
        (429, {}, "rate_limited"),
        (503, {"error_description": "SENSITIVE_PROVIDER_TEXT"}, "unexpected_response"),
        (302, {}, "unexpected_response"),
        (200, {"unexpected": True}, "unexpected_response"),
        (200, token(expires_in=float("nan")), "unexpected_response"),
        (200, token(expires_in=True), "unexpected_response"),
        (200, token(expires_in=float("inf")), "unexpected_response"),
        (200, token(refresh_token=False), "unexpected_response"),
    ],
)
async def test_exchange_failures_are_explicit_and_never_retried(
    status, payload, outcome
):
    attempt = OAuthAttempt.create()
    session = mock_session(response(status, payload))
    with patch(
        "custom_components.vattenfall.oauth.ClientSession", return_value=session
    ):
        result = await async_test_oauth(attempt, callback(attempt))
    assert result == OAuthTestResult("exchange", status, outcome)
    assert session.post.call_count == 1
    assert "SENSITIVE_PROVIDER_TEXT" not in repr(result)
    session.__aexit__.assert_awaited_once()


async def test_missing_refresh_and_refresh_rejection_are_not_success():
    attempt = OAuthAttempt.create()
    without_refresh = token()
    del without_refresh["refresh_token"]
    for responses, expected, count in [
        (
            [response(payload=without_refresh)],
            OAuthTestResult("exchange", 200, "no_refresh_token"),
            1,
        ),
        (
            [response(payload=token()), response(400, {"error": "invalid_grant"})],
            OAuthTestResult("refresh", 400, "grant_rejected"),
            2,
        ),
    ]:
        session = mock_session(*responses)
        with patch(
            "custom_components.vattenfall.oauth.ClientSession", return_value=session
        ):
            result = await async_test_oauth(attempt, callback(attempt))
        assert result == expected
        assert session.post.call_count == count


@pytest.mark.parametrize("body", [b"{", json.dumps(token()).encode() + b" " * 65536])
async def test_malformed_and_oversized_token_responses_are_rejected(body):
    attempt = OAuthAttempt.create()
    session = mock_session(response(raw=body))
    with patch(
        "custom_components.vattenfall.oauth.ClientSession", return_value=session
    ):
        result = await async_test_oauth(attempt, callback(attempt))
    assert result.outcome == "unexpected_response"
    assert session.post.call_count == 1


async def test_sensitive_connection_exception_is_not_returned_and_session_closes():
    attempt = OAuthAttempt.create()
    session = mock_session(ClientConnectionError("SENSITIVE_PROVIDER_TEXT"))
    with patch(
        "custom_components.vattenfall.oauth.ClientSession", return_value=session
    ):
        result = await async_test_oauth(attempt, callback(attempt))
    assert result == OAuthTestResult("exchange", None, "cannot_connect")
    assert "SENSITIVE_PROVIDER_TEXT" not in repr(result)
    session.__aexit__.assert_awaited_once()
