"""Full history includes interval imports/exports and retired meters."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from custom_components.eac_cyprus.history import (
    async_import_full_history,
    history_window,
    statistic_id,
)
from custom_components.eac_cyprus.statistics import EacStatistics


async def test_full_history_and_replay(hass, mock_client):
    store = EacStatistics(hass, "full")
    # Both service points are queried, even if the contract is inactive.
    mock_client.list_service_points.return_value = (
        mock_client.list_service_points.return_value[:1]
    )
    with patch(
        "custom_components.eac_cyprus.statistics.async_add_external_statistics"
    ) as add:
        results = await async_import_full_history(hass, mock_client, store)
        assert {r["channel"] for r in results} == {"S-KWH-24H", "KWH-30MIN-LP-IMP"}
        assert add.call_count == 2
        add.reset_mock()
        await async_import_full_history(hass, mock_client, store)
        assert add.call_count == 2  # recorder upserts the same rows
    for call in mock_client.get_readings.call_args_list:
        assert call.args[1] == datetime(2026, 2, 1, 22, tzinfo=UTC)


async def test_full_history_retired_export(
    hass, mock_client, fake_active_meter, fake_meter_30min_channel
):
    export = replace(fake_meter_30min_channel, type="KWH-30MIN-LP-EXP")
    old = replace(
        fake_active_meter,
        serial_number="OLD",
        removal_date=datetime(2026, 5, 10),
        channels=(export,),
    )
    mock_client.get_meter_configs.return_value = [old]
    store = EacStatistics(hass, "retired")
    with patch("custom_components.eac_cyprus.statistics.async_add_external_statistics"):
        result = await async_import_full_history(hass, mock_client, store)
    assert result[0]["channel"] == "KWH-30MIN-LP-EXP"
    assert mock_client.get_readings.call_args.args[2] == datetime(
        2026, 5, 9, 21, tzinfo=UTC
    )


async def test_poll_recovers_long_outage_and_reconciles_old_corrections(
    hass, fake_active_meter, fake_meter_30min_channel
):
    store = EacStatistics(hass, "window")
    now = datetime(2026, 10, 27, tzinfo=UTC)
    sid = statistic_id("111", fake_active_meter, fake_meter_30min_channel)
    store.series[sid] = {
        "full_checked": now.isoformat(),
        "fetched_through": (now - timedelta(days=40)).isoformat(),
    }
    start, _end, full = history_window(store, sid, fake_active_meter, now, 14)
    assert start == now - timedelta(days=41)
    assert not full
    assert history_window(store, sid, fake_active_meter, now + timedelta(days=8), 14)[2]
