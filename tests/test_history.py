"""Full history includes interval imports/exports and retired meters."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from eac_dso_portal import CYPRUS, ChannelReadings, EacApiError, Reading

from custom_components.eac_cyprus.history import (
    async_fetch_energy_history,
    async_import_full_history,
    history_window,
    statistic_id,
)
from custom_components.eac_cyprus.statistics import EacStatistics, build_statistics


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
        before = {
            call.args[1]["statistic_id"]: call.args[2] for call in add.call_args_list
        }
        add.reset_mock()
        await async_import_full_history(hass, mock_client, store)
        after = {
            call.args[1]["statistic_id"]: call.args[2] for call in add.call_args_list
        }
        assert after == before  # recorder upserts the same rows
    assert mock_client.get_readings.call_args_list[0].args[1] == datetime(
        2026, 2, 1, 22, tzinfo=UTC
    )


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


async def test_from_last_keeps_initial_weekly_and_forced_full_imports(
    hass, fake_active_meter
):
    store = EacStatistics(hass, "incremental")
    now = datetime(2026, 10, 27, tzinfo=UTC)
    installed = datetime(2026, 2, 1, 22, tzinfo=UTC)
    assert history_window(
        store, "test", fake_active_meter, now, 14, from_last=True
    ) == (installed, now, True)
    store.series["test"] = {"full_checked": now.isoformat(), "points": {}}
    assert history_window(
        store, "test", fake_active_meter, now, 14, from_last=True
    ) == (installed, now, True)
    # The latest actual reading can lag far behind a successful request.
    last = now - timedelta(days=40)
    store.series["test"].update(
        points={last.isoformat(): "0.1"}, fetched_through=now.isoformat()
    )
    assert history_window(
        store, "test", fake_active_meter, now, 14, from_last=True
    ) == (now - timedelta(days=42), now, False)
    later = now + timedelta(days=7)
    assert history_window(
        store, "test", fake_active_meter, later, 14, from_last=True
    ) == (installed, later, True)
    assert history_window(
        store, "test", fake_active_meter, now, 14, force=True, from_last=True
    ) == (installed, now, True)


async def test_from_last_uses_utc_through_dst_and_stops_at_meter_removal(
    hass, fake_active_meter
):
    store = EacStatistics(hass, "dst_incremental")
    now = datetime(2026, 10, 27, tzinfo=UTC)
    # These correspond to the two occurrences of 03:30 in Cyprus.
    first = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    second = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    store.series["test"] = {
        "full_checked": now.isoformat(),
        "points": {second.isoformat(): "0.1", first.isoformat(): "0.1"},
    }
    retired = replace(fake_active_meter, removal_date=datetime(2026, 10, 26))
    assert history_window(store, "test", retired, now, 14, from_last=True) == (
        datetime(2026, 10, 23, 22, tzinfo=UTC),
        datetime(2026, 10, 25, 22, tzinfo=UTC),
        False,
    )


@pytest.mark.parametrize(
    ("gap", "window_days"),
    [
        (timedelta(minutes=30), 2),
        (timedelta(hours=23), 2),
        (timedelta(days=1), 3),
        (timedelta(days=2), 4),
        (timedelta(days=6), 8),
    ],
)
@pytest.mark.parametrize("lagging", ["portal", "poll"])
async def test_catch_up_window_grows_with_actual_data_or_poll_gap(
    hass, fake_active_meter, gap, window_days, lagging
):
    store = EacStatistics(hass, "window_growth")
    now = datetime(2026, 10, 27, 12, tzinfo=UTC)
    latest = now - gap if lagging == "portal" else now
    through = now - gap if lagging == "poll" else now
    store.series["test"] = {
        "full_checked": now.isoformat(),
        "fetched_through": through.isoformat(),
        "points": {latest.isoformat(): "0.1"},
    }
    assert history_window(
        store, "test", fake_active_meter, now, 14, from_last=True
    ) == (now - timedelta(days=window_days), now, False)


@pytest.mark.parametrize("first_page_empty", [False, True])
async def test_weekly_chunks_resume_after_failure_and_restart(
    hass, mock_client, fake_active_meter, fake_meter_30min_channel, first_page_empty
):
    start = datetime(2026, 10, 20, 12, tzinfo=UTC)
    end = datetime(2026, 11, 5, 12, tzinfo=UTC)
    boundary = datetime(2026, 10, 27, 13, tzinfo=UTC)  # 7 local days, 169 hours
    first = Reading(start + timedelta(minutes=30), 0.1, None)
    shared = Reading(boundary, 0.2, None)
    later = Reading(boundary + timedelta(minutes=30), 0.3, None)
    last = Reading(end, 0.4, None)
    source = [] if first_page_empty else [first, shared]
    store = EacStatistics(hass, "resume")
    sid = statistic_id("111", fake_active_meter, fake_meter_30min_channel)

    def fail_second(sp_id, since, until, mc_id=None):
        if since >= boundary:
            raise EacApiError(503, "temporary", "/readings/list")
        return [ChannelReadings(mc_id, tuple(source))]

    mock_client.get_readings.side_effect = fail_second
    with patch("custom_components.eac_cyprus.statistics.async_add_external_statistics"):
        with pytest.raises(EacApiError):
            await async_fetch_energy_history(
                mock_client,
                store,
                "111",
                fake_active_meter,
                fake_meter_30min_channel,
                start,
                end,
                full=True,
            )
        saved = store.series[sid]
        assert len(saved["points"]) == len(source)
        assert "full_checked" not in saved
        assert (
            datetime.fromisoformat(saved["import_progress"]["next_start"]) == boundary
        )

        restarted = EacStatistics(hass, "resume")
        await restarted.async_load()
        window = history_window(restarted, sid, fake_active_meter, end, 2, force=True)
        assert window == (boundary, end, True)

        # The boundary changes on retry. It must replace, never add to, its old value.
        source = ([first] if not first_page_empty else []) + [
            replace(shared, value=0.25),
            later,
            last,
        ]
        mock_client.get_readings.side_effect = lambda sp, since, until, mc_id=None: [
            ChannelReadings(mc_id, tuple(r for r in source if since <= r.dt <= until))
        ]
        mock_client.get_readings.reset_mock()
        await async_fetch_energy_history(
            mock_client,
            restarted,
            "111",
            fake_active_meter,
            fake_meter_30min_channel,
            window[0],
            window[1],
            full=window[2],
        )
        assert mock_client.get_readings.await_args_list[0].args[1] == boundary
        complete = restarted.series[sid]
        assert "import_progress" not in complete
        assert "full_checked" in complete
        expected = 0.95 if first_page_empty else 1.05
        assert (
            build_statistics(complete["points"], interval=True)[-1]["sum"] == expected
        )
        await async_fetch_energy_history(
            mock_client,
            restarted,
            "111",
            fake_active_meter,
            fake_meter_30min_channel,
            start,
            end,
            full=True,
        )
        assert (
            build_statistics(restarted.series[sid]["points"], interval=True)[-1]["sum"]
            == expected
        )
        for call in mock_client.get_readings.await_args_list:
            _, since, until = call.args
            assert until.astimezone(CYPRUS) - since.astimezone(CYPRUS) <= timedelta(
                days=7
            )
