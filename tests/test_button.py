"""The translated refresh control invokes ordinary polling, including retries."""

from datetime import timedelta

import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eac_cyprus.const import CONF_EMAIL, CONF_PASSWORD, DOMAIN


@pytest.mark.parametrize(
    ("language", "name"),
    [("en", "Refresh data from EAC"), ("el", "Ενημέρωση δεδομένων από την ΑΗΚ")],
)
async def test_refresh_button_runs_normal_poll(
    hass, patch_eac_client, mock_client, language, name
):
    hass.config.language = language
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test@example.com",
        data={CONF_EMAIL: "test@example.com", CONF_PASSWORD: "x"},
        title="Test",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, "111111111111_refresh_data"
    )
    assert entity_id
    assert hass.states.get(entity_id).attributes["friendly_name"].endswith(name)
    assert len(hass.states.async_all("button")) == 1
    coordinator = hass.data[DOMAIN][entry.entry_id]
    # A failed scheduled poll must not disable the user's recovery button.
    coordinator.last_update_success = False
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state != "unavailable"
    mock_client.get_readings.reset_mock()
    await hass.services.async_call(
        "button", "press", {"entity_id": entity_id}, blocking=True
    )
    assert mock_client.get_readings.await_count == 2
    for call in mock_client.get_readings.await_args_list:
        _, start, end = call.args
        assert end - start == timedelta(days=2)
    # A short empty window retains the actual known meter values.
    meter = next(iter(coordinator.data.points["111111111111"].meters.values()))
    assert meter.channels["mc_30min_imp"].latest.value == 0.315
    assert coordinator.last_update_success
