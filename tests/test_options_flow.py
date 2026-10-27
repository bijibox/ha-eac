"""Tests for the EAC Cyprus options flow."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from eac_dso_portal import ChannelReadings, Reading
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eac_cyprus.const import (
    CONF_EMAIL,
    CONF_FETCH_FROM_LAST,
    CONF_HISTORY_DAYS,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL_MINUTES,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
)


def _entry(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test@example.com",
        data={CONF_EMAIL: "test@example.com", CONF_PASSWORD: "x"},
        options=options or {},
        title="Test User",
    )
    entry.add_to_hass(hass)
    return entry


async def test_options_flow_defaults_on_first_open(
    hass: HomeAssistant, patch_eac_client
) -> None:
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"
    defaults = {key.schema: key.default() for key in result["data_schema"].schema}
    assert defaults[CONF_FETCH_FROM_LAST] is False


async def test_options_flow_saves_and_applies(
    hass: HomeAssistant, patch_eac_client
) -> None:
    """Submitting the form updates entry.options *and* the coordinator's tick."""
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.update_interval == timedelta(
        minutes=DEFAULT_SCAN_INTERVAL_MINUTES
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCAN_INTERVAL_MINUTES: "60",  # select selector returns strings
            CONF_HISTORY_DAYS: 7,
            CONF_FETCH_FROM_LAST: True,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SCAN_INTERVAL_MINUTES] == 60  # coerced to int
    assert entry.options[CONF_HISTORY_DAYS] == 7
    assert entry.options[CONF_FETCH_FROM_LAST] is True
    # The update listener picked up the new interval without reloading.
    assert coordinator.update_interval == timedelta(minutes=60)


async def test_options_flow_form_prefills_current_values(
    hass: HomeAssistant, patch_eac_client
) -> None:
    """The form should show the currently-saved options as defaults, not factory."""
    entry = _entry(
        hass,
        options={
            CONF_SCAN_INTERVAL_MINUTES: 180,
            CONF_HISTORY_DAYS: 30,
            CONF_FETCH_FROM_LAST: True,
        },
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    # voluptuous Schema's defaults are exposed via the result's data_schema
    schema = result["data_schema"]
    defaults = {key.schema: key.default() for key in schema.schema}
    assert defaults[CONF_SCAN_INTERVAL_MINUTES] == "180"
    assert defaults[CONF_HISTORY_DAYS] == 30
    assert defaults[CONF_FETCH_FROM_LAST] is True


async def test_coordinator_reads_history_days_from_options(
    hass: HomeAssistant, mock_client
) -> None:
    """history_days is read fresh on every refresh — no reload needed."""
    from custom_components.eac_cyprus.coordinator import EacCoordinator

    entry = _entry(hass, options={CONF_HISTORY_DAYS: 3})
    coord = EacCoordinator(hass, mock_client, entry)
    await coord._async_update_data()

    # The initial poll establishes complete history; subsequent polls use options.
    mock_client.get_readings.reset_mock()
    await coord._async_update_data()
    args = mock_client.get_readings.await_args_list
    assert args, "expected at least one get_readings call"
    _sp_id, start, end, *_ = args[0].args + tuple(args[0].kwargs.values())[:0]
    assert (end - start) == timedelta(days=3)


async def test_coordinator_falls_back_to_defaults_without_options(
    hass: HomeAssistant, mock_client
) -> None:
    from custom_components.eac_cyprus.coordinator import EacCoordinator

    entry = _entry(hass)
    coord = EacCoordinator(hass, mock_client, entry)
    assert coord.update_interval == timedelta(minutes=DEFAULT_SCAN_INTERVAL_MINUTES)
    await coord._async_update_data()
    mock_client.get_readings.reset_mock()
    await coord._async_update_data()
    args = mock_client.get_readings.await_args_list[0]
    _sp_id, start, end = args.args[:3]
    assert (end - start) == timedelta(days=DEFAULT_HISTORY_DAYS)


async def test_incremental_poll_corrects_last_reading_and_resumes_after_restart(
    hass, mock_client, fake_active_meter, fake_meter_30min_channel, freezer
):
    """Use saved observations even when the last request covered a later date."""
    from custom_components.eac_cyprus.coordinator import EacCoordinator
    from custom_components.eac_cyprus.history import statistic_id
    from custom_components.eac_cyprus.statistics import build_statistics

    freezer.move_to("2026-05-08T12:00:00+00:00")

    entry = _entry(
        hass,
        options={
            CONF_FETCH_FROM_LAST: True,
            CONF_SCAN_INTERVAL_MINUTES: 30,
            CONF_HISTORY_DAYS: 3,
        },
    )
    meter = replace(fake_active_meter, channels=(fake_meter_30min_channel,))
    mock_client.get_meter_configs.return_value = [meter]
    coord = EacCoordinator(hass, mock_client, entry)
    assert coord.update_interval == timedelta(minutes=30)
    await coord._async_update_data()

    last = datetime(2026, 5, 8, 11, tzinfo=UTC)  # fixture's 14:00 Cyprus
    following = last + timedelta(minutes=30)
    mock_client.get_readings.side_effect = None
    mock_client.get_readings.return_value = [
        ChannelReadings(
            channel_id=fake_meter_30min_channel.id,
            readings=(
                Reading(dt=last, value=0.321, reading=None),
                Reading(dt=following, value=0.456, reading=None),
            ),
        )
    ]
    mock_client.get_readings.reset_mock()
    await coord._async_update_data()
    assert mock_client.get_readings.await_args.args[1] == datetime(
        2026, 5, 6, 12, tzinfo=UTC
    )
    sid = statistic_id("111111111111", meter, fake_meter_30min_channel)
    points = coord.statistics.series[sid]["points"]
    assert len(points) == 2
    assert build_statistics(points, interval=True)[-1]["sum"] == 0.777

    # Empty responses and restart must not move the cursor to request end.
    restarted = EacCoordinator(hass, mock_client, entry)
    mock_client.get_readings.return_value = []
    for _ in range(2):
        await restarted._async_update_data()
        assert mock_client.get_readings.await_args.args[1] == datetime(
            2026, 5, 6, 12, tzinfo=UTC
        )
    assert restarted.statistics.series[sid]["points"] == points

    # Disabling the checkbox takes effect without changing the cadence.
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_FETCH_FROM_LAST: False}
    )
    await restarted._async_update_data()
    _, start, end = mock_client.get_readings.await_args.args
    assert end - start == timedelta(days=3)
    assert restarted.update_interval == timedelta(minutes=30)
