"""Acceptance checks for precise, replayable external energy statistics."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from eac_dso_portal import Reading
from homeassistant.components.recorder.models import StatisticMeanType

from custom_components.eac_cyprus.statistics import (
    EacStatistics,
    build_statistics,
    interval_statistic_id,
)


def readings(*pairs):
    return tuple(
        Reading(datetime.fromisoformat(dt), value, None) for dt, value in pairs
    )


def test_first_interval_and_decimal_precision():
    points = {
        "2026-05-01T00:30:00+00:00": "0.101",
        "2026-05-01T01:00:00+00:00": "0.202",
    }
    rows = build_statistics(points, True)
    assert [r["sum"] for r in rows] == [0, 0.303]
    assert rows[1]["start"] == datetime(2026, 5, 1, tzinfo=UTC)


@pytest.mark.parametrize("month,day,hours", [(3, 29, 23), (10, 25, 25), (5, 1, 24)])
def test_day_totals_across_dst(month, day, hours):
    tz = ZoneInfo("Asia/Nicosia")
    start = datetime(2026, month, day, tzinfo=tz).astimezone(UTC)
    end = (datetime(2026, month, day, tzinfo=tz) + timedelta(days=1)).astimezone(UTC)
    points = {}
    dt = start + timedelta(minutes=30)
    while dt <= end:
        points[dt.isoformat()] = "0.123"
        dt += timedelta(minutes=30)
    rows = build_statistics(points, True)
    assert len(rows) == hours + 1
    assert Decimal(str(rows[-1]["sum"])) == Decimal("0.123") * hours * 2
    assert rows[-1]["start"] == end - timedelta(hours=1)


def test_month_totals_and_missing_half_hour():
    start = datetime(2026, 10, 1, tzinfo=ZoneInfo("Asia/Nicosia")).astimezone(UTC)
    end = datetime(2026, 11, 1, tzinfo=ZoneInfo("Asia/Nicosia")).astimezone(UTC)
    dt = start + timedelta(minutes=30)
    points = {}
    while dt <= end:
        points[dt.isoformat()] = "0.001"
        dt += timedelta(minutes=30)
    rows = build_statistics(points, True)
    assert rows[-1]["sum"] == 1.490  # 31 days plus one autumn hour
    del points[(start + timedelta(minutes=30)).isoformat()]
    assert build_statistics(points, True)[-1]["sum"] == 1.489


def test_sparse_register_initial_value_is_only_a_baseline():
    rows = build_statistics(
        {
            "2025-01-01T00:00:00+00:00": "12000",
            "2025-03-01T00:00:00+00:00": "12150",
            "2025-05-01T00:00:00+00:00": "8",
            "2025-07-01T00:00:00+00:00": "23",
        },
        False,
    )
    assert len(rows) == 4
    assert [r["sum"] for r in rows] == [0, 150, 150, 165]


def test_meter_swap_isolated_even_with_reused_channel_id():
    assert interval_statistic_id("111", "old", "1") != interval_statistic_id(
        "111", "new", "1"
    )


async def test_repeat_restart_correction_and_earlier_backfill(hass):
    sid = "eac_cyprus:111_precise"
    initial = readings(
        ("2026-05-02T00:30:00+00:00", 0.1),
        ("2026-05-02T01:00:00+00:00", 0.2),
        ("2026-05-02T01:30:00+00:00", 0.3),
    )
    with patch(
        "custom_components.eac_cyprus.statistics.async_add_external_statistics"
    ) as add:
        ledger = EacStatistics(hass, "test")
        await ledger.async_merge(sid, "Energy", initial, interval=True, full=True)
        assert add.call_args.args[1]["mean_type"] == StatisticMeanType.NONE
        assert add.call_args.args[2][-1]["sum"] == 0.6
        add.reset_mock()
        await ledger.async_merge(sid, "Energy", initial, interval=True)
        add.assert_not_called()
        restarted = EacStatistics(hass, "test")
        await restarted.async_load()
        assert add.call_args.args[2][-1]["sum"] == 0.6
        await restarted.async_merge(
            sid, "Energy", readings(("2026-05-02T00:30:00+00:00", 0.111)), interval=True
        )
        assert add.call_args.args[2][-1]["sum"] == 0.611
        await restarted.async_merge(
            sid, "Energy", readings(("2026-05-01T23:30:00+00:00", 1.001)), interval=True
        )
        assert add.call_args.args[2][-1]["sum"] == 1.612
        # Corrected first hour's change stays precise after an earlier backfill.
        assert add.call_args.args[2][-2]["sum"] == 1.312


async def test_bad_interval_does_not_modify_ledger(hass):
    ledger = EacStatistics(hass, "invalid")
    with pytest.raises(ValueError):
        await ledger.async_merge(
            "eac_cyprus:invalid",
            "Energy",
            readings(("2026-05-01T00:30:00+00:00", -1)),
            interval=True,
        )
    assert ledger.series == {}


async def test_recorder_upserts_and_dashboard_daily_monthly_changes(
    hass, recorder_mock
):
    """Exercise HA's real SQLite recorder and its Energy change queries."""
    from homeassistant.components.recorder.statistics import statistics_during_period
    from homeassistant.util import dt as dt_util

    dt_util.set_default_time_zone(ZoneInfo("Asia/Nicosia"))
    sid = "eac_cyprus:acceptance"
    ledger = EacStatistics(hass, "recorder")
    # One local calendar day spanning the autumn fallback (50 half-hours).
    start = datetime(2026, 10, 25, tzinfo=ZoneInfo("Asia/Nicosia")).astimezone(UTC)
    points = tuple(
        Reading(start + timedelta(minutes=30 * (i + 1)), 0.123, None) for i in range(50)
    )
    await ledger.async_merge(sid, "Energy", points, interval=True, full=True)
    await hass.async_add_executor_job(recorder_mock.block_till_done)

    async def query(period):
        return await recorder_mock.async_add_executor_job(
            statistics_during_period,
            hass,
            start,
            start + timedelta(hours=25),
            {sid},
            period,
            None,
            {"sum", "change"},
        )

    assert (await query("day"))[sid][0]["change"] == pytest.approx(6.150, abs=0.000001)
    assert (await query("month"))[sid][0]["change"] == pytest.approx(
        6.150, abs=0.000001
    )
    before = await query("hour")
    await ledger.async_merge(sid, "Energy", points, interval=True, full=True)
    restarted = EacStatistics(hass, "recorder")
    await restarted.async_load()
    await hass.async_add_executor_job(recorder_mock.block_till_done)
    assert await query("hour") == before
    await restarted.async_merge(
        sid, "Energy", (Reading(points[0].dt, 0.124, None),), interval=True
    )
    await hass.async_add_executor_job(recorder_mock.block_till_done)
    assert (await query("day"))[sid][0]["change"] == pytest.approx(6.151, abs=0.000001)
    assert len((await query("hour"))[sid]) == 25


async def test_recorder_enqueue_failure_retries_unchanged_source(hass):
    ledger = EacStatistics(hass, "retry")
    args = (
        "eac_cyprus:retry",
        "Energy",
        readings(("2026-05-01T00:30:00+00:00", 0.123)),
    )
    with (
        patch(
            "custom_components.eac_cyprus.statistics.async_add_external_statistics",
            side_effect=RuntimeError("queue unavailable"),
        ),
        pytest.raises(RuntimeError),
    ):
        await ledger.async_merge(*args, interval=True)
    with patch(
        "custom_components.eac_cyprus.statistics.async_add_external_statistics"
    ) as add:
        await ledger.async_merge(*args, interval=True)
        assert add.call_args.args[2][-1]["sum"] == 0.123
