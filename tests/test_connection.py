"""Synthetic signed identities, account scoping and persistent refresh behavior."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from time import time
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from aiohttp import ClientSession
from cryptography.hazmat.primitives.asymmetric import rsa
from homeassistant import loader
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.setup import async_setup_component

from custom_components.vattenfall.api import PortalAuthError, PortalConnectionError
from custom_components.vattenfall.config_flow import VattenfallConfigFlow
from custom_components.vattenfall.connection import (
    INITIALIZATION_URL,
    Account,
    ApplicationKeys,
    RenewableSession,
    async_accounts,
    async_application_keys,
)
from custom_components.vattenfall.oauth import (
    APP_CLIENT_ID,
    ISSUER,
    JWKS_URL,
    OAuthAttempt,
    OAuthError,
    OAuthTokens,
    VerifiedIdentity,
    async_authorize,
    async_refresh,
    async_verify_identity,
)

from .test_auth import response
from .test_oauth import callback, mock_session, token

BP = "1000000001"
CA = "2000000001"
CONTEXT = f"{BP}:{CA}"
KEYS = ApplicationKeys("s" * 32, "i" * 32)


def credentials():
    return OAuthTokens(
        "synthetic-id-token",
        "synthetic-refresh-token",
        time() + 3600,
        "synthetic-subject",
        (BP,),
        "openid device:synthetic",
        "synthetic-nonce",
    )


def entry_for(hass, tokens=None):
    data = {
        **(tokens or credentials()).data(),
        "subscription_key": KEYS.contracts,
        "vf_context": CONTEXT,
    }
    entry = ConfigEntry(
        version=1,
        minor_version=1,
        domain="vattenfall",
        title="Synthetic",
        data=data,
        source="user",
        unique_id="synthetic-unique-id",
        options={},
        discovery_keys={},
        subentries_data=[],
    )
    hass.config_entries._entries[entry.entry_id] = entry
    return entry


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def signed_identity(signing_key, **updates):
    claims = {
        "iss": ISSUER,
        "aud": APP_CLIENT_ID,
        "sub": "synthetic-subject",
        "iat": int(time()) - 1,
        "exp": int(time()) + 3600,
        "nonce": "synthetic-nonce",
        "customer_id": [BP],
        **updates,
    }
    encoded = jwt.encode(
        claims, signing_key, algorithm="RS256", headers={"kid": "synthetic-key"}
    )
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    jwk["kid"] = "synthetic-key"
    return encoded, {"keys": [jwk]}


async def test_real_signature_verification_pins_identity_and_jwks(signing_key):
    encoded, jwks = signed_identity(signing_key)
    session = MagicMock(spec=ClientSession)
    session.get.return_value = response(payload=jwks)
    result = await async_verify_identity(
        session, encoded, "synthetic-nonce", initial=True
    )
    assert result.subject == "synthetic-subject"
    assert result.customer_ids == (BP,)
    session.get.assert_called_once_with(JWKS_URL, allow_redirects=False)
    assert "synthetic-subject" not in repr(result)


@pytest.mark.parametrize(
    "updates",
    [
        {"iss": "https://attacker.invalid"},
        {"aud": "other-client"},
        {"aud": [APP_CLIENT_ID, "other-client"]},
        {"azp": "other-client"},
        {"sub": "other-user"},
        {"exp": time() - 10},
        {"iat": time() + 3600},
        {"nonce": None},
        {"nonce": "other-attempt"},
        {"customer_id": []},
        {"customer_id": [BP, "../other"]},
        {"customer_id": BP},
        {"at_hash": "incorrect"},
    ],
)
async def test_identity_failures_never_trust_claims(signing_key, updates, caplog):
    encoded, jwks = signed_identity(signing_key, **updates)
    session = MagicMock(spec=ClientSession)
    session.get.return_value = response(payload=jwks)
    with pytest.raises(OAuthError, match="identity_invalid"):
        await async_verify_identity(
            session,
            encoded,
            "synthetic-nonce",
            subject="synthetic-subject",
            access_token="synthetic-access-token",
            initial=True,
        )
    assert encoded not in caplog.text


async def test_bad_signature_and_untrusted_algorithm_stop(signing_key):
    encoded, jwks = signed_identity(signing_key)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    wrong_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(other.public_key()))
    wrong_jwk["kid"] = "synthetic-key"
    session = MagicMock(spec=ClientSession)
    session.get.return_value = response(payload={"keys": [wrong_jwk]})
    with pytest.raises(OAuthError, match="identity_invalid"):
        await async_verify_identity(session, encoded, "synthetic-nonce")
    forged = jwt.encode(
        {"iss": ISSUER}, "s" * 64, algorithm="HS256", headers={"kid": "synthetic-key"}
    )
    session.get.reset_mock()
    with pytest.raises(OAuthError, match="identity_invalid"):
        await async_verify_identity(session, forged, "synthetic-nonce")
    session.get.assert_not_called()
    session.get.return_value = response(payload=jwks)
    with pytest.raises(OAuthError, match="identity_invalid"):
        await async_verify_identity(session, encoded, "wrong-nonce")


async def test_refresh_may_omit_nonce_but_not_change_subject(signing_key):
    encoded, jwks = signed_identity(signing_key)
    claims = jwt.decode(encoded, options={"verify_signature": False})
    claims.pop("nonce")
    encoded = jwt.encode(
        claims, signing_key, algorithm="RS256", headers={"kid": "synthetic-key"}
    )
    session = MagicMock(spec=ClientSession)
    session.get.return_value = response(payload=jwks)
    result = await async_verify_identity(
        session, encoded, "synthetic-nonce", subject="synthetic-subject"
    )
    assert result.customer_ids == (BP,)
    with pytest.raises(OAuthError, match="identity_invalid"):
        await async_verify_identity(session, encoded, "synthetic-nonce", initial=True)


async def test_authorize_and_rotated_refresh_require_valid_identity():
    attempt = OAuthAttempt.create()
    session = mock_session(
        response(payload=token(id_token="synthetic-id")),
        response(
            payload=token(
                id_token="refreshed-id", refresh_token="rotated-refresh-token"
            )
        ),
    )
    identity = VerifiedIdentity("synthetic-subject", (BP,), time() + 3600)
    with (
        patch("custom_components.vattenfall.oauth.ClientSession", return_value=session),
        patch(
            "custom_components.vattenfall.oauth.async_verify_identity",
            return_value=identity,
        ) as verify,
    ):
        tokens = await async_authorize(attempt, callback(attempt))
        rotated = await async_refresh(tokens)
    assert rotated.id_token == "refreshed-id"
    assert rotated.refresh_token == "rotated-refresh-token"
    assert rotated.nonce == attempt.nonce
    assert verify.await_args_list[0].kwargs["initial"]
    assert verify.await_args_list[1].kwargs["subject"] == tokens.subject
    assert not verify.await_args_list[1].kwargs["initial"]
    assert session.post.call_args_list[1].kwargs["data"]["refresh_token"] == (
        "synthetic-refresh-token"
    )
    assert "rotated-refresh-token" not in repr(rotated)


@pytest.mark.parametrize(
    ("payload", "outcome"),
    [
        (token(id_token="synthetic", refresh_token=None), "no_refresh_token"),
        (token(), "identity_invalid"),
        (token(id_token=[]), "identity_invalid"),
    ],
)
async def test_production_authorization_never_saves_partial_tokens(payload, outcome):
    attempt = OAuthAttempt.create()
    session = mock_session(response(payload=payload))
    with (
        patch("custom_components.vattenfall.oauth.ClientSession", return_value=session),
        pytest.raises(OAuthError, match=outcome),
    ):
        await async_authorize(attempt, callback(attempt))


async def test_public_application_keys_are_discovered_without_authentication():
    session = MagicMock(spec=ClientSession)
    session.get.side_effect = [
        response(
            raw=b'<link href="chunk-empty.js"><script src="chunk-config.js"></script>'
        ),
        response(raw=b"const uninteresting = true"),
        response(
            raw=(
                f"const cfg={{cepTouchPointSubscriptionKey:`{KEYS.contracts}`,"
                f"ocpApimSubscriptionInitialisationKey:'{KEYS.initialization}'}}"
            ).encode()
        ),
    ]
    assert await async_application_keys(session) == KEYS
    assert all(call.kwargs["headers"] is None for call in session.get.call_args_list)
    assert all(
        call.kwargs["allow_redirects"] is False for call in session.get.call_args_list
    )


async def test_bootstrap_change_or_redirect_is_not_silently_accepted():
    session = MagicMock(spec=ClientSession)
    session.get.return_value = response(
        raw=b'<script src="https://attacker.invalid/x">'
    )
    with pytest.raises(OAuthError, match="bootstrap_changed"):
        await async_application_keys(session)
    assert session.get.call_count == 1
    session.get.return_value = response(
        302, headers={"Location": "https://attacker.invalid"}
    )
    with pytest.raises(OAuthError, match="cannot_connect"):
        await async_application_keys(session)


def account_payload(customer=BP, account=CA):
    return {
        "data": [
            {
                "contactPerson": {"firstName": "SENSITIVE_UNUSED_PROFILE"},
                "businessPartners": [
                    {
                        "businessPartnerId": customer,
                        "contractAccounts": [{"contractAccountId": account}],
                    }
                ],
            }
        ]
    }


async def test_account_lookup_is_scoped_and_discards_profile(caplog):
    session = MagicMock(spec=ClientSession)
    session.get.return_value = response(payload=account_payload())
    accounts = await async_accounts(session, credentials(), KEYS)
    assert accounts == (Account(BP, CA),)
    call = session.get.call_args
    assert call.args == (INITIALIZATION_URL,)
    assert call.kwargs["params"] == {"businessPartnerIds": BP}
    assert call.kwargs["headers"]["Authorization"] == "Bearer synthetic-id-token"
    assert "SENSITIVE_UNUSED_PROFILE" not in repr(accounts) + caplog.text
    session.get.return_value = response(payload=account_payload(customer="9999999999"))
    with pytest.raises(OAuthError, match="wrong_account"):
        await async_accounts(session, credentials(), KEYS)
    session.get.return_value = response(payload=account_payload(account="../other"))
    with pytest.raises(OAuthError, match="bootstrap_changed"):
        await async_accounts(session, credentials(), KEYS)


async def test_refresh_is_serialized_and_persisted_before_credentials_return(hass):
    original = replace(credentials(), expires_at=time() - 1)
    entry = entry_for(hass, original)
    manager = RenewableSession(hass, entry)
    rotated = replace(
        credentials(),
        id_token="refreshed-id",
        refresh_token="rotated-refresh-token",
    )
    with patch(
        "custom_components.vattenfall.connection.async_refresh",
        return_value=rotated,
    ) as refresh:
        results = await asyncio.gather(
            manager.async_credentials(), manager.async_credentials()
        )
        refresh.assert_awaited_once()
    assert all(result.access_token == "refreshed-id" for result in results)
    assert entry.data["access_token"] == "refreshed-id"
    assert entry.data["refresh_token"] == "rotated-refresh-token"
    assert entry.data["vf_context"] == CONTEXT


async def test_reconfiguration_during_refresh_cannot_overwrite_new_credentials(hass):
    entry = entry_for(hass, replace(credentials(), expires_at=time() - 1))
    replacement = {
        **entry.data,
        "refresh_token": "new-login-refresh-token",
        "expires_at": time() + 3600,
    }

    async def concurrent_reconfiguration(_tokens):
        hass.config_entries.async_update_entry(entry, data=replacement)
        return replace(credentials(), refresh_token="obsolete-rotated-token")

    with (
        patch(
            "custom_components.vattenfall.connection.async_refresh",
            side_effect=concurrent_reconfiguration,
        ),
        pytest.raises(PortalConnectionError, match="changed"),
    ):
        await RenewableSession(hass, entry).async_credentials()
    assert entry.data == replacement


@pytest.mark.parametrize("status", [400, 503])
async def test_provider_outage_is_transient_not_a_rejected_login(status):
    session = mock_session(
        response(status, payload={"error": "temporarily_unavailable"})
    )
    with (
        patch("custom_components.vattenfall.oauth.ClientSession", return_value=session),
        pytest.raises(OAuthError, match="cannot_connect"),
    ):
        await async_refresh(credentials())


@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        ("grant_rejected", PortalAuthError),
        ("identity_invalid", PortalAuthError),
        ("cannot_connect", PortalConnectionError),
        ("rate_limited", PortalConnectionError),
    ],
)
async def test_refresh_failure_never_erases_credentials_or_changes_account(
    hass, outcome, error
):
    entry = entry_for(hass, replace(credentials(), expires_at=time() - 1))
    original = dict(entry.data)
    with (
        patch(
            "custom_components.vattenfall.connection.async_refresh",
            side_effect=OAuthError(outcome),
        ),
        pytest.raises(error),
    ):
        await RenewableSession(hass, entry).async_credentials()
    assert entry.data == original


async def test_restart_revalidates_saved_identity_and_account(hass):
    entry = entry_for(hass)
    with patch(
        "custom_components.vattenfall.connection.async_verify_identity",
        return_value=VerifiedIdentity(
            "synthetic-subject", ("9999999999",), time() + 3600
        ),
    ):
        with pytest.raises(PortalAuthError, match="wrong_account"):
            await RenewableSession(hass, entry).async_credentials()


async def test_expired_rotated_identity_cannot_switch_account(hass):
    entry = entry_for(hass, replace(credentials(), expires_at=time() - 1))
    original = dict(entry.data)
    with (
        patch(
            "custom_components.vattenfall.connection.async_refresh",
            return_value=replace(credentials(), customer_ids=("9999999999",)),
        ),
        pytest.raises(PortalAuthError, match="wrong_account"),
    ):
        await RenewableSession(hass, entry).async_credentials()
    assert entry.data == original


async def prepare_flow(hass):
    flow = VattenfallConfigFlow()
    flow.hass = hass
    flow.context = {"source": "user"}
    await flow.async_step_login()
    return flow


async def authorize_flow(flow):
    with (
        patch(
            "custom_components.vattenfall.config_flow.async_authorize",
            return_value=credentials(),
        ),
        patch(
            "custom_components.vattenfall.config_flow.async_application_keys",
            return_value=KEYS,
        ),
        patch(
            "custom_components.vattenfall.config_flow.async_accounts",
            return_value=(Account(BP, CA), Account(BP, "2000000002")),
        ),
    ):
        return await flow.async_step_login({"callback_url": "synthetic-callback"})


async def test_live_flow_selects_account_then_saves_only_validated_credentials(hass):
    flow = await prepare_flow(hass)
    result = await authorize_flow(flow)
    assert result["step_id"] == "account"
    assert flow._oauth_attempt is None
    with patch(
        "custom_components.vattenfall.config_flow._validate",
        return_value=None,
    ) as validate:
        invalid = await flow.async_step_account({"vf_context": "999:999"})
        assert invalid["errors"] == {"base": "connect_wrong_account"}
        validate.assert_not_awaited()
        done = await flow.async_step_account({"vf_context": CONTEXT})
    assert done["type"] == "create_entry"
    assert done["data"]["auth_method"] == "oauth"
    assert done["data"]["refresh_token"] == "synthetic-refresh-token"
    assert done["data"]["vf_context"] == CONTEXT
    assert "callback_url" not in done["data"]
    assert flow._login_tokens is None
    assert flow._login_key is None
    assert flow._oauth_cancel is None


async def test_failed_tariff_validation_does_not_create_entry(hass):
    flow = await prepare_flow(hass)
    await authorize_flow(flow)
    with patch(
        "custom_components.vattenfall.config_flow._validate",
        return_value="invalid_auth",
    ):
        result = await flow.async_step_account({"vf_context": CONTEXT})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_auth"}
    flow.async_remove()
    assert flow._login_tokens is None


@pytest.mark.parametrize("source", ["reauth", "reconfigure"])
async def test_reauthentication_rejects_another_account_before_saving(hass, source):
    entry = entry_for(hass)
    flow = await prepare_flow(hass)
    flow.context = {"source": source, "entry_id": entry.entry_id}
    with patch.object(
        flow,
        f"_get_{'reauth' if source == 'reauth' else 'reconfigure'}_entry",
        return_value=entry,
    ):
        result = await authorize_flow(flow)
        assert result["step_id"] == "account"
        choices = result["data_schema"].schema
        assert list(choices.values())[0].container == {CONTEXT: f"{BP} / {CA}"}
        entry_data = dict(entry.data)
        flow._clear_login()
        await flow.async_step_login()
        with (
            patch(
                "custom_components.vattenfall.config_flow.async_authorize",
                return_value=replace(credentials(), subject="other-user"),
            ),
            patch(
                "custom_components.vattenfall.config_flow.async_application_keys",
                return_value=KEYS,
            ),
            patch(
                "custom_components.vattenfall.config_flow.async_accounts",
                return_value=(Account(BP, CA),),
            ),
        ):
            result = await flow.async_step_login({"callback_url": "synthetic"})
        assert result["errors"] == {"base": "connect_wrong_account"}
        assert entry.data == entry_data
    flow.async_remove()


async def test_cancellation_during_exchange_cannot_save_result(hass):
    flow = await prepare_flow(hass)

    async def cancel(*args):
        flow.async_remove()
        return credentials()

    with (
        patch(
            "custom_components.vattenfall.config_flow.async_authorize",
            side_effect=cancel,
        ),
        patch(
            "custom_components.vattenfall.config_flow.async_application_keys",
            return_value=KEYS,
        ),
        patch(
            "custom_components.vattenfall.config_flow.async_accounts",
            return_value=(Account(BP, CA),),
        ),
    ):
        result = await flow.async_step_login({"callback_url": "synthetic"})
    assert result["type"] == "abort"
    assert flow._login_tokens is None


async def test_manual_reconfiguration_removes_renewable_credentials(hass):
    entry = entry_for(hass)
    flow = await prepare_flow(hass)
    flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
    manual = {
        "access_token": "new-manual-token",
        "subscription_key": "new-manual-key",
        "vf_context": CONTEXT,
    }
    with (
        patch.object(flow, "_get_reconfigure_entry", return_value=entry),
        patch.object(flow, "async_update_reload_and_abort") as finish,
        patch("custom_components.vattenfall.config_flow._validate", return_value=None),
    ):
        await flow.async_step_manual(manual)
    assert finish.call_args.kwargs["data"] == manual
    assert "refresh_token" not in finish.call_args.kwargs["data"]
    flow.async_remove()


async def test_real_ha_connection_refresh_reload_and_reauth_preserve_entities(
    hass, payload, tmp_path
):
    """Exercise the actual flow manager, headers, entry reload and sensor setup."""
    (tmp_path / "custom_components").symlink_to(
        Path(__file__).parents[1] / "custom_components", target_is_directory=True
    )
    loader.async_setup(hass)
    await hass.config_entries.async_initialize()
    device_registry.async_setup(hass)
    await device_registry.async_load(hass)
    await entity_registry.async_load(hass)
    assert await async_setup_component(hass, "sensor", {})
    reply = response(payload=payload)
    reply.json = AsyncMock(return_value=payload)
    session = MagicMock(spec=ClientSession)
    session.get.return_value = reply
    rotated = replace(
        credentials(),
        id_token="refreshed-id-token",
        refresh_token="rotated-refresh-token",
    )
    with (
        patch(
            "custom_components.vattenfall.async_get_clientsession", return_value=session
        ),
        patch(
            "custom_components.vattenfall.config_flow.async_get_clientsession",
            return_value=session,
        ),
        patch(
            "custom_components.vattenfall.config_flow.async_authorize",
            return_value=credentials(),
        ),
        patch(
            "custom_components.vattenfall.config_flow.async_application_keys",
            return_value=KEYS,
        ),
        patch(
            "custom_components.vattenfall.config_flow.async_accounts",
            return_value=(Account(BP, CA),),
        ),
        patch(
            "custom_components.vattenfall.connection.async_verify_identity",
            return_value=VerifiedIdentity("synthetic-subject", (BP,), time() + 3600),
        ) as verify,
        patch(
            "custom_components.vattenfall.connection.async_refresh",
            return_value=rotated,
        ) as refresh,
    ):
        menu = await hass.config_entries.flow.async_init(
            "vattenfall", context={"source": "user"}
        )
        login = await hass.config_entries.flow.async_configure(
            menu["flow_id"], {"next_step_id": "login"}
        )
        account = await hass.config_entries.flow.async_configure(
            login["flow_id"], {"callback_url": "synthetic-callback"}
        )
        result = await hass.config_entries.flow.async_configure(
            account["flow_id"], {"vf_context": CONTEXT}
        )
        await hass.async_block_till_done()
        entry = result["result"]
        assert entry.state is ConfigEntryState.LOADED
        assert entry.data["auth_method"] == "oauth"
        entity_ids = {state.entity_id for state in hass.states.async_all("sensor")}
        assert len(entity_ids) == 4
        assert all(
            state.state not in ("unknown", "unavailable")
            for state in hass.states.async_all("sensor")
        )
        assert verify.await_count == 1
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "expires_at": time() - 1}
        )
        await entry.runtime_data.async_refresh()
        assert entry.data["refresh_token"] == rotated.refresh_token
        refresh.assert_awaited_once()
        assert session.get.call_args.kwargs["headers"]["Authorization"] == (
            "Bearer refreshed-id-token"
        )
        assert session.get.call_args.kwargs["headers"]["Vf-Context"] == CONTEXT
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert verify.await_count == 2
        assert entity_ids == {
            state.entity_id for state in hass.states.async_all("sensor")
        }
        menu = await hass.config_entries.flow.async_init(
            "vattenfall",
            context={"source": "reauth", "entry_id": entry.entry_id},
        )
        login = await hass.config_entries.flow.async_configure(
            menu["flow_id"], {"next_step_id": "login"}
        )
        account = await hass.config_entries.flow.async_configure(
            login["flow_id"], {"callback_url": "new-synthetic-callback"}
        )
        result = await hass.config_entries.flow.async_configure(
            account["flow_id"], {"vf_context": CONTEXT}
        )
        assert result["reason"] == "reauth_successful"
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert entity_ids == {
            state.entity_id for state in hass.states.async_all("sensor")
        }
        assert await hass.config_entries.async_unload(entry.entry_id)
