"""Exercise configuration, coordinator and entity behavior against real HA."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import loader
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.helpers.json import json_bytes
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.setup import async_setup_component

from custom_components.vattenfall import async_setup_entry, async_unload_entry
from custom_components.vattenfall.api import PortalAuthError, PortalConnectionError
from custom_components.vattenfall.config_flow import VattenfallConfigFlow
from custom_components.vattenfall.const import (
    CONF_ACCESS_TOKEN,
    CONF_SUBSCRIPTION_KEY,
    CONF_VF_CONTEXT,
)
from custom_components.vattenfall.coordinator import VattenfallCoordinator
from custom_components.vattenfall.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.vattenfall.models import (
    InvalidTariffData,
    OverviewData,
    OverviewMetric,
    parse_tariffs,
)
from custom_components.vattenfall.sensor import (
    DESCRIPTIONS,
    VattenfallTariffSensor,
)
from custom_components.vattenfall.sensor import (
    async_setup_entry as async_setup_sensors,
)

from .conftest import NOW, TODAY

SESSION_DATA = {
    CONF_ACCESS_TOKEN: "synthetic-token",
    CONF_SUBSCRIPTION_KEY: "synthetic-key",
    CONF_VF_CONTEXT: "synthetic-context",
}


def flow(hass):
    result = VattenfallConfigFlow()
    result.hass = hass
    result.context = {"source": "user"}
    return result


async def _complete_session_flow(hass, flow_id, method, data):
    form = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": method}
    )
    if method == "browser":
        from homeassistant.data_entry_flow import InvalidData

        payload = {
            **data,
            "browser_session": form["description_placeholders"]["pairing_code"].split(
                "."
            )[-1],
        }
        receipt = await hass.config_entries.flow.async_configure(flow_id, payload)
        assert receipt["step_id"] == "browser_confirm"
        assert not any(value in str(receipt) for value in data.values())
        with pytest.raises(InvalidData):
            await hass.config_entries.flow.async_configure(flow_id, payload)
        return await hass.config_entries.flow.async_configure(flow_id, {})
    return await hass.config_entries.flow.async_configure(flow_id, data)


@pytest.mark.parametrize("invalid", ["wrong", "unicode", "missing", "extra", "type"])
async def test_browser_handoff_rejects_malformed_or_wrong_nonce(hass, invalid):
    config_flow = flow(hass)
    form = await config_flow.async_step_browser()
    nonce = form["description_placeholders"]["pairing_code"].split(".")[-1]
    data = {**SESSION_DATA, "browser_session": nonce}
    if invalid == "wrong":
        data["browser_session"] = "x" * 43
    elif invalid == "unicode":
        data["browser_session"] = "\N{LATIN SMALL LETTER E WITH ACUTE}" * 43
    elif invalid == "missing":
        del data[CONF_ACCESS_TOKEN]
    elif invalid == "extra":
        data["unrelated"] = "discard"
    else:
        data[CONF_VF_CONTEXT] = 123
    result = await config_flow.async_step_browser(data)
    assert result["errors"] == {"base": "invalid_browser_session"}
    assert config_flow._browser_data is None
    assert not any(value in str(result) for value in SESSION_DATA.values())
    config_flow.async_remove()


async def test_browser_expiry_and_cancel_discard_staged_credentials(hass):
    config_flow = flow(hass)
    with patch("custom_components.vattenfall.config_flow.async_call_later") as timer:
        form = await config_flow.async_step_browser()
        nonce = form["description_placeholders"]["pairing_code"].split(".")[-1]
        await config_flow.async_step_browser({**SESSION_DATA, "browser_session": nonce})
        assert config_flow._browser_data == SESSION_DATA
        timer.call_args.args[2](None)
        assert config_flow._browser_data is None
        result = await config_flow.async_step_browser_confirm({})
        assert result["errors"] == {"base": "browser_timeout"}
        new_nonce = result["description_placeholders"]["pairing_code"].split(".")[-1]
        assert new_nonce != nonce
        await config_flow.async_step_browser(
            {**SESSION_DATA, "browser_session": new_nonce}
        )
        config_flow.async_remove()
        assert config_flow._browser_data is None
        assert config_flow._browser_nonce is None
        assert config_flow._browser_cancel is None


async def test_browser_validates_before_saving_and_discards_rejected_session(hass):
    config_flow = flow(hass)
    form = await config_flow.async_step_browser()
    nonce = form["description_placeholders"]["pairing_code"].split(".")[-1]
    await config_flow.async_step_browser({**SESSION_DATA, "browser_session": nonce})
    with patch(
        "custom_components.vattenfall.config_flow.VattenfallClient.async_get_tariffs",
        side_effect=PortalAuthError,
    ):
        result = await config_flow.async_step_browser_confirm({})
    assert result["errors"] == {"base": "invalid_auth"}
    assert config_flow._browser_data is None
    assert nonce not in str(result)
    config_flow.async_remove()


async def test_browser_deadline_is_enforced_even_before_timer_runs(hass):
    config_flow = flow(hass)
    form = await config_flow.async_step_browser()
    nonce = form["description_placeholders"]["pairing_code"].split(".")[-1]
    with patch(
        "custom_components.vattenfall.config_flow.monotonic",
        return_value=config_flow._browser_deadline,
    ):
        result = await config_flow.async_step_browser(
            {**SESSION_DATA, "browser_session": nonce}
        )
    assert result["errors"] == {"base": "browser_timeout"}
    assert config_flow._browser_data is None
    config_flow.async_remove()


async def test_browser_reconfigure_updates_existing_entry(hass):
    config_flow = flow(hass)
    config_flow.context = {"source": "reconfigure"}
    entry = MagicMock(spec=ConfigEntry)
    form = await config_flow.async_step_browser()
    nonce = form["description_placeholders"]["pairing_code"].split(".")[-1]
    await config_flow.async_step_browser({**SESSION_DATA, "browser_session": nonce})
    with (
        patch(
            "custom_components.vattenfall.config_flow.VattenfallClient.async_get_tariffs",
            new_callable=AsyncMock,
        ),
        patch.object(config_flow, "_get_reconfigure_entry", return_value=entry),
        patch.object(
            config_flow, "async_update_reload_and_abort", return_value={"type": "abort"}
        ) as update,
    ):
        await config_flow.async_step_browser_confirm({})
    update.assert_called_once_with(entry, data_updates=SESSION_DATA)
    assert config_flow._browser_data is None


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (PortalAuthError("rejected"), "invalid_auth"),
        (PortalConnectionError("offline"), "cannot_connect"),
        (InvalidTariffData("schema"), "invalid_tariffs"),
    ],
)
async def test_config_error_is_explicit_and_never_prefills_secrets(
    hass, error, expected
):
    with patch(
        "custom_components.vattenfall.config_flow.VattenfallClient.async_get_tariffs",
        side_effect=error,
    ):
        result = await flow(hass).async_step_user(SESSION_DATA)
    assert result["type"] == "form"
    assert result["errors"] == {"base": expected}
    assert "synthetic-token" not in str(result)


async def test_config_creates_entry_only_after_validation(hass):
    with patch(
        "custom_components.vattenfall.config_flow.VattenfallClient.async_get_tariffs",
        new_callable=AsyncMock,
    ) as fetch:
        result = await flow(hass).async_step_user(
            {**SESSION_DATA, "unrelated": "discard"}
        )
    fetch.assert_awaited_once()
    assert result["type"] == "create_entry"
    assert result["data"] == SESSION_DATA


@pytest.mark.parametrize(
    ("step", "getter"),
    [
        ("async_step_reauth_confirm", "_get_reauth_entry"),
        ("async_step_reconfigure", "_get_reconfigure_entry"),
    ],
)
async def test_reauth_and_reconfigure_replace_headers(hass, step, getter):
    config_flow = flow(hass)
    entry = MagicMock(spec=ConfigEntry)
    with (
        patch(
            "custom_components.vattenfall.config_flow.VattenfallClient.async_get_tariffs",
            new_callable=AsyncMock,
        ),
        patch.object(config_flow, getter, return_value=entry),
        patch.object(
            config_flow, "async_update_reload_and_abort", return_value={"type": "abort"}
        ) as update,
    ):
        await getattr(config_flow, step)(SESSION_DATA)
    update.assert_called_once_with(entry, data_updates=SESSION_DATA)


async def test_reauth_rejection_preserves_existing_entry(hass):
    config_flow = flow(hass)
    with (
        patch(
            "custom_components.vattenfall.config_flow.VattenfallClient.async_get_tariffs",
            side_effect=PortalAuthError,
        ),
        patch.object(config_flow, "async_update_reload_and_abort") as update,
    ):
        result = await config_flow.async_step_reauth_confirm(SESSION_DATA)
    assert result["errors"] == {"base": "invalid_auth"}
    update.assert_not_called()


async def test_failure_retains_snapshot_but_sensor_is_unavailable(hass, payload):
    client = MagicMock()
    client.async_get_tariffs = AsyncMock(return_value=parse_tariffs(payload, TODAY))
    coordinator = VattenfallCoordinator(hass, client)
    with patch("homeassistant.util.dt.utcnow", return_value=NOW):
        await coordinator.async_refresh()
        sensor = VattenfallTariffSensor(coordinator, "entry", DESCRIPTIONS["gas"])
        before = sensor.native_value
        fetched_at = coordinator.data.fetched_at
        assert sensor.available
        assert sensor.extra_state_attributes["stale"] is False
        client.async_get_tariffs.side_effect = PortalConnectionError("offline")
        await coordinator.async_refresh()
        assert not coordinator.last_update_success
        assert not sensor.available
        assert sensor.native_value is None
        attrs = sensor.extra_state_attributes
        assert attrs["stale"] is True
        assert attrs["last_known_rate"] == float(before)
        assert attrs["last_successful_update"] == fetched_at.isoformat()
        client.async_get_tariffs.side_effect = None
        await coordinator.async_refresh()
        assert sensor.available
        assert sensor.extra_state_attributes["stale"] is False


@pytest.mark.parametrize("with_snapshot", [False, True])
async def test_diagnostics_are_json_safe_health_metadata_only(
    hass, payload, with_snapshot
):
    client = MagicMock()
    client.async_get_tariffs = AsyncMock(return_value=parse_tariffs(payload, TODAY))
    coordinator = VattenfallCoordinator(hass, client)
    entry = MagicMock(spec=ConfigEntry)
    entry.data = SESSION_DATA
    entry.runtime_data = coordinator
    with patch("homeassistant.util.dt.utcnow", return_value=NOW):
        if with_snapshot:
            await coordinator.async_refresh()
        client.async_get_tariffs.side_effect = PortalConnectionError("offline")
        await coordinator.async_refresh()
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result == {
        "last_update_success": False,
        "last_successful_update": NOW.isoformat() if with_snapshot else None,
        "tariff_kinds": (
            ["electricity_normal", "electricity_offpeak", "gas"]
            if with_snapshot
            else []
        ),
    }
    encoded = json_bytes(result).decode()
    assert all(value not in encoded for value in SESSION_DATA.values())


async def test_expired_tariff_not_presented_as_current_without_new_data(hass, payload):
    client = MagicMock()
    client.async_get_tariffs = AsyncMock(return_value=parse_tariffs(payload, TODAY))
    coordinator = VattenfallCoordinator(hass, client)
    with patch("homeassistant.util.dt.utcnow", return_value=NOW):
        await coordinator.async_refresh()
    sensor = VattenfallTariffSensor(coordinator, "entry", DESCRIPTIONS["gas"])
    with patch("homeassistant.util.dt.utcnow", return_value=NOW.replace(year=2027)):
        assert not sensor.available
        assert sensor.extra_state_attributes["stale"] is True
        assert sensor.extra_state_attributes["last_known_rate"] is not None


async def test_coordinator_uses_dutch_date_not_utc_date(hass, payload):
    client = MagicMock()
    client.async_get_tariffs = AsyncMock(return_value=parse_tariffs(payload, TODAY))
    coordinator = VattenfallCoordinator(hass, client)
    with patch(
        "homeassistant.util.dt.utcnow",
        return_value=NOW.replace(day=2, hour=22, minute=30),
    ):
        await coordinator._async_update_data()
    client.async_get_tariffs.assert_awaited_once_with(TODAY + timedelta(days=2))


async def test_auth_error_requires_reauthentication(hass):
    client = MagicMock()
    client.async_get_tariffs = AsyncMock(side_effect=PortalAuthError)
    coordinator = VattenfallCoordinator(hass, client)
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_schema_error_is_update_failure(hass):
    client = MagicMock()
    client.async_get_tariffs = AsyncMock(side_effect=InvalidTariffData("unsupported"))
    coordinator = VattenfallCoordinator(hass, client)
    with pytest.raises(UpdateFailed, match="unsupported"):
        await coordinator._async_update_data()


async def test_new_active_blocks_are_discovered_without_duplicate_entities(
    hass, payload
):
    client = MagicMock()
    client.async_get_tariffs = AsyncMock(return_value=parse_tariffs(payload, TODAY))
    coordinator = VattenfallCoordinator(hass, client)
    entry = MagicMock(spec=ConfigEntry)
    entry.runtime_data = coordinator
    entry.entry_id = "synthetic-entry"
    added = []

    with patch("homeassistant.util.dt.utcnow", return_value=NOW):
        await coordinator.async_refresh()
        await async_setup_sensors(hass, entry, lambda entities: added.extend(entities))
        assert len(added) == 4
        await coordinator.async_refresh()
        assert len(added) == 4
        prices = payload["tariffData"]["includingVat"]["specs"]["electricity"][0][
            "prices"
        ]
        prices[:] = [{**prices[0], "blockDescription": "Enkeltarief"}]
        client.async_get_tariffs.return_value = parse_tariffs(payload, TODAY)
        await coordinator.async_refresh()
        assert len(added) == 5
        assert not next(
            s for s in added if s.entity_description.key == "electricity_normal"
        ).available
        assert next(
            s for s in added if s.entity_description.key == "electricity_single"
        ).available
    entry.async_on_unload.call_args.args[0]()


async def test_entry_setup_and_unload(hass):
    entry = MagicMock(spec=ConfigEntry)
    entry.data = SESSION_DATA
    with (
        patch(
            "custom_components.vattenfall.VattenfallCoordinator.async_config_entry_first_refresh",
            new_callable=AsyncMock,
        ) as refresh,
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock
        ) as forward,
        patch.object(
            hass.config_entries,
            "async_unload_platforms",
            new_callable=AsyncMock,
            return_value=True,
        ) as unload,
    ):
        assert await async_setup_entry(hass, entry)
        refresh.assert_awaited_once()
        assert isinstance(entry.runtime_data, VattenfallCoordinator)
        forward.assert_awaited_once()
        assert await async_unload_entry(hass, entry)
        unload.assert_awaited_once()


async def test_initial_network_failure_retries_without_forwarding_entities(hass):
    entry = MagicMock(spec=ConfigEntry)
    entry.data = SESSION_DATA
    with (
        patch(
            "custom_components.vattenfall.VattenfallCoordinator.async_config_entry_first_refresh",
            side_effect=ConfigEntryNotReady,
        ),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock
        ) as forward,
    ):
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, entry)
        forward.assert_not_awaited()


@pytest.mark.parametrize("method", ["manual", "browser"])
async def test_real_flow_loads_integration_and_registers_sensors(
    hass, payload, tmp_path, method
):
    """Run HA's flow manager, loader, entry setup and real sensor platform together."""
    (tmp_path / "custom_components").symlink_to(
        Path(__file__).parents[1] / "custom_components", target_is_directory=True
    )
    loader.async_setup(hass)
    await hass.config_entries.async_initialize()
    device_registry.async_setup(hass)
    await device_registry.async_load(hass)
    await entity_registry.async_load(hass)
    assert await async_setup_component(hass, "sensor", {})
    with (
        patch(
            "custom_components.vattenfall.api.VattenfallClient.async_get_tariffs",
            new_callable=AsyncMock,
            return_value=parse_tariffs(payload, TODAY),
        ) as fetch,
        patch("homeassistant.util.dt.utcnow", return_value=NOW),
    ):
        form = await hass.config_entries.flow.async_init(
            "vattenfall", context={"source": "user"}
        )
        assert form["type"] == "menu"
        result = await _complete_session_flow(
            hass, form["flow_id"], method, SESSION_DATA
        )
        await hass.async_block_till_done()
        assert result["type"] == "create_entry"
        entry = result["result"]
        assert entry.state is ConfigEntryState.LOADED
        all_states = hass.states.async_all("sensor")
        assert len(all_states) == 4
        json_bytes([state.as_dict() for state in all_states])
        states = [
            state
            for state in all_states
            if state.attributes.get("device_class") != "timestamp"
        ]
        assert len(states) == 3
        assert sorted(state.state for state in states) == ["0.24", "0.28", "1.37"]
        assert all(state.attributes["includes_vat"] for state in states)
        assert all(not state.attributes["stale"] for state in states)
        entity_ids = {state.entity_id for state in states}
        fetch.side_effect = PortalConnectionError("synthetic outage")
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        all_states = hass.states.async_all("sensor")
        json_bytes([state.as_dict() for state in all_states])
        snapshot_state = next(
            state
            for state in all_states
            if state.attributes.get("device_class") == "timestamp"
        )
        states = [state for state in all_states if state is not snapshot_state]
        assert all(state.state == "unavailable" for state in states)
        assert snapshot_state.attributes["stale"] is True
        assert snapshot_state.attributes["last_known_tariffs"]["gas"]["rate"] == 1.37

        fetch.side_effect = PortalAuthError("synthetic expired session")
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        reauth = next(
            progress
            for progress in hass.config_entries.flow.async_progress()
            if progress["context"]["source"] == "reauth"
        )
        assert reauth["step_id"] == "reauth"
        fetch.side_effect = None
        replacement = {key: f"rotated-{value}" for key, value in SESSION_DATA.items()}
        reauthenticated = await _complete_session_flow(
            hass,
            reauth["flow_id"],
            "reauth_confirm" if method == "manual" else "browser",
            replacement,
        )
        await hass.async_block_till_done()
        assert reauthenticated["reason"] == "reauth_successful"
        assert entry.data == replacement
        assert entry.state is ConfigEntryState.LOADED
        states = [
            state
            for state in hass.states.async_all("sensor")
            if state.attributes.get("device_class") != "timestamp"
        ]
        assert {state.entity_id for state in states} == entity_ids
        assert sorted(state.state for state in states) == ["0.24", "0.28", "1.37"]
        overview = entry.runtime_data.overview
        values = OverviewData(
            (
                OverviewMetric("installment", Decimal("135")),
                OverviewMetric("contract_status", "Active"),
            )
        )
        with patch(
            "custom_components.vattenfall.api.VattenfallClient.async_get_overview",
            new_callable=AsyncMock,
            return_value=values,
        ) as overview_fetch:
            await overview.async_refresh()
            await hass.async_block_till_done()
            all_states = hass.states.async_all("sensor")
            json_bytes([state.as_dict() for state in all_states])
            added = [
                state
                for state in all_states
                if state.entity_id not in entity_ids
                and state.attributes.get("device_class") != "timestamp"
            ]
            assert sorted(state.state for state in added) == ["135", "Active"]
            overview_ids = {state.entity_id for state in added}
            overview_fetch.side_effect = PortalConnectionError(
                "synthetic overview outage"
            )
            await overview.async_refresh()
            await hass.async_block_till_done()
            assert all(
                hass.states.get(entity_id).state == "unavailable"
                for entity_id in overview_ids
            )
            assert all(
                hass.states.get(entity_id).state != "unavailable"
                for entity_id in entity_ids
            )
            diagnostic = hass.states.get(snapshot_state.entity_id)
            assert diagnostic.attributes["overview_stale"] is True
            json_bytes([state.as_dict() for state in hass.states.async_all("sensor")])
            overview_fetch.side_effect = None
            await overview.async_refresh()
            await hass.async_block_till_done()
            assert all(
                hass.states.get(entity_id).state != "unavailable"
                for entity_id in overview_ids
            )
        assert await hass.config_entries.async_unload(entry.entry_id)
