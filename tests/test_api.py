"""Network boundary, sensitive headers and explicit portal errors."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientConnectionError, ClientSession

from custom_components.vattenfall.api import (
    PortalAuthError,
    PortalConnectionError,
    PortalCredentials,
    VattenfallClient,
)
from custom_components.vattenfall.const import CONTRACTS_URL
from custom_components.vattenfall.models import InvalidTariffData

from .conftest import TODAY

CREDENTIALS = PortalCredentials(
    "synthetic-test-token", "synthetic-test-key", "synthetic-test-context"
)


def mock_session(payload, status=200, content_type="application/json"):
    response = MagicMock(status=status, content_type=content_type)
    response.json = AsyncMock(return_value=payload)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    session = MagicMock(spec=ClientSession)
    session.get.return_value = context
    return session, response


async def test_fixed_endpoint_only_no_redirects_and_no_response_logging(
    payload, caplog
):
    session, _ = mock_session(payload)
    tariffs = await VattenfallClient(session, CREDENTIALS).async_get_tariffs(TODAY)
    assert len(tariffs) == 3
    session.get.assert_called_once_with(
        CONTRACTS_URL,
        headers={
            "Authorization": "Bearer synthetic-test-token",
            "Ocp-Apim-Subscription-Key": "synthetic-test-key",
            "Vf-Context": "synthetic-test-context",
            "Accept": "application/json",
        },
        params=None,
        allow_redirects=False,
    )
    assert caplog.text == ""


@pytest.mark.parametrize("status", [301, 302, 307, 401, 403])
async def test_auth_failures_never_read_response_body(payload, status):
    session, response = mock_session(payload, status=status)
    with pytest.raises(PortalAuthError):
        await VattenfallClient(session, CREDENTIALS).async_get_tariffs(TODAY)
    response.json.assert_not_called()
    assert session.get.call_count == 1


@pytest.mark.parametrize("status", [400, 404, 429, 500, 503])
async def test_http_errors_explicit(payload, status):
    session, response = mock_session(payload, status=status)
    with pytest.raises(PortalConnectionError, match=str(status)):
        await VattenfallClient(session, CREDENTIALS).async_get_tariffs(TODAY)
    response.json.assert_not_called()


async def test_html_is_not_interpreted_as_tariffs(payload):
    session, response = mock_session(payload, content_type="text/html")
    with pytest.raises(PortalConnectionError, match="JSON"):
        await VattenfallClient(session, CREDENTIALS).async_get_tariffs(TODAY)
    response.json.assert_not_called()


@pytest.mark.parametrize(
    "error", [ValueError("private response"), UnicodeError("private")]
)
async def test_invalid_json_does_not_leak_response(payload, error):
    session, response = mock_session(payload)
    response.json.side_effect = error
    with pytest.raises(PortalConnectionError) as caught:
        await VattenfallClient(session, CREDENTIALS).async_get_tariffs(TODAY)
    assert "private" not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize(
    "error", [TimeoutError("secret-token"), ClientConnectionError("secret-token")]
)
async def test_connection_errors_hide_sensitive_exception_context(payload, error):
    session, _ = mock_session(payload)
    session.get.side_effect = error
    with pytest.raises(PortalConnectionError) as caught:
        await VattenfallClient(session, CREDENTIALS).async_get_tariffs(TODAY)
    assert "secret-token" not in str(caught.value)
    assert caught.value.__suppress_context__


async def test_schema_drift_is_an_explicit_failure():
    session, _ = mock_session({"unexpected": "synthetic"})
    with pytest.raises(InvalidTariffData):
        await VattenfallClient(session, CREDENTIALS).async_get_tariffs(TODAY)


async def test_null_json_is_not_confused_with_an_unsupported_optional_service():
    session, _ = mock_session(None)
    with pytest.raises(PortalConnectionError, match="null JSON"):
        await VattenfallClient(session, CREDENTIALS).async_get_overview(TODAY)


@pytest.mark.parametrize("token", ["", "a\r\nInjected: value", "a\0b"])
async def test_invalid_credentials_do_not_issue_request(payload, token):
    session, _ = mock_session(payload)
    credentials = PortalCredentials(token, "key", "context")
    with pytest.raises(PortalAuthError):
        await VattenfallClient(session, credentials).async_get_tariffs(TODAY)
    session.get.assert_not_called()


def test_bearer_prefix_and_whitespace():
    credentials = PortalCredentials(" Bearer test-token ", " key ", " context ")
    assert credentials.headers()["Authorization"] == "Bearer test-token"
    assert "test-token" not in repr(credentials)
