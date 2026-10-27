"""Shared full, rolling and incremental energy history ingestion."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from eac_dso_portal import (
    CYPRUS,
    EacClient,
    MeasurementChannel,
    MeterConfig,
    Reading,
    as_utc,
)
from homeassistant.core import HomeAssistant

from .const import channel_label
from .statistics import (
    INTERVAL_TYPES,
    EacStatistics,
    interval_statistic_id,
)

_MAX_LOOKBACK = timedelta(days=3650)
RECONCILE_INTERVAL = timedelta(days=7)
IMPORT_CHUNK_DAYS = 7


def meter_identity(cfg: MeterConfig) -> str:
    return f"{cfg.serial_number or 'unknown'}_{cfg.install_date.isoformat() if cfg.install_date else 'unknown'}"


def statistic_id(
    sp_id: str, cfg: MeterConfig, channel: MeasurementChannel, position: str = "end"
) -> str:
    if channel.type in INTERVAL_TYPES:
        return interval_statistic_id(
            sp_id, f"{meter_identity(cfg)}_{position}", channel.id
        )
    return (
        interval_statistic_id(sp_id, meter_identity(cfg), channel.id).removesuffix(
            "_energy"
        )
        + "_register"
    )


def is_energy(channel: MeasurementChannel) -> bool:
    return channel.uom.upper() == "KWH" and (
        not channel.interval or channel.type in INTERVAL_TYPES
    )


def history_window(
    store: EacStatistics,
    sid: str,
    cfg: MeterConfig,
    now: datetime,
    days: int,
    force: bool = False,
    *,
    from_last: bool = False,
) -> tuple[datetime, datetime, bool]:
    data = store.series.get(sid, {})
    # Resume an interrupted reconciliation before starting a new request range.
    # A manual full import supersedes a partially completed rolling poll.
    if (progress := data.get("import_progress")) and (progress["full"] or not force):
        return (
            datetime.fromisoformat(progress["next_start"]),
            datetime.fromisoformat(progress["end"]),
            progress["full"],
        )
    checked = data.get("full_checked")
    full = (
        force
        or not checked
        or now - datetime.fromisoformat(checked) >= RECONCILE_INTERVAL
    )
    installed = as_utc(cfg.install_date) if cfg.install_date else now - _MAX_LOOKBACK
    end = as_utc(cfg.removal_date) if cfg.removal_date else now
    if full:
        return installed, end, True
    if from_last:
        points = data.get("points", {})
        if not points:
            return installed, end, True
        # Keep two days for frequent polling; each complete missed day adds a
        # day. Use the older of requested coverage and actual observations so a
        # successful but delayed/empty portal response cannot skip missing data.
        latest = max(datetime.fromisoformat(key) for key in points)
        through = datetime.fromisoformat(
            data.get("fetched_through", latest.isoformat())
        )
        missed_days = max(0, (end - min(latest, through)).days)
        start = end - timedelta(days=missed_days + 2)
        return max(installed, start), end, False
    start = end - timedelta(days=days)
    # Catch up after an outage longer than the rolling correction window.
    if through := data.get("fetched_through"):
        start = min(start, datetime.fromisoformat(through) - timedelta(days=1))
    # Whole local days are fetched in the library; overlap an additional day so
    # the initial cumulative baseline and midnight-ending interval are retained.
    return max(installed, start), end, False


async def import_channel(
    store: EacStatistics,
    sp_id: str,
    cfg: MeterConfig,
    ch: MeasurementChannel,
    readings,
    *,
    full: bool,
    end: datetime,
    position: str = "end",
    progress: dict | None = None,
) -> str:
    sid = statistic_id(sp_id, cfg, ch, position)
    serial = cfg.serial_number or "unknown"
    interval = ch.type in INTERVAL_TYPES
    label = (
        (
            "Energy imported (30-min)"
            if ch.type.endswith("IMP")
            else "Energy exported (30-min)"
        )
        if interval
        else channel_label(ch.type)
    )
    await store.async_merge(
        sid,
        f"{serial} {label}",
        readings,
        interval=interval,
        position=position,
        full=full,
        fetched_through=end,
        progress=progress,
    )
    return sid


async def async_fetch_energy_history(
    client: EacClient,
    store: EacStatistics,
    sp_id: str,
    cfg: MeterConfig,
    ch: MeasurementChannel,
    start: datetime,
    end: datetime,
    *,
    full: bool,
) -> tuple[Reading, ...]:
    """Save each week before advancing, including a durable restart cursor.

    Adjacent chunks include their common boundary. The ledger replaces that
    observation by UTC timestamp, retaining corrections without double counting.
    Only the final successful chunk marks a full reconciliation complete.
    """
    found: dict[datetime, Reading] = {}
    cursor = start
    while True:
        # Calendar arithmetic in Cyprus keeps weekly boundaries on the same
        # local hour across DST; conversion back to UTC preserves both folds.
        page_end = min(
            end,
            (cursor.astimezone(CYPRUS) + timedelta(days=IMPORT_CHUNK_DAYS)).astimezone(
                UTC
            ),
        )
        crs = await client.get_readings(sp_id, cursor, page_end, mc_id=ch.id)
        readings = tuple(
            reading
            for cr in crs
            if cr.channel_id == ch.id
            for reading in cr.readings
            if cursor <= as_utc(reading.dt) <= page_end
        )
        found.update((as_utc(r.dt), r) for r in readings)
        finished = page_end == end
        await import_channel(
            store,
            sp_id,
            cfg,
            ch,
            readings,
            full=full and finished,
            end=page_end,
            progress=None
            if finished
            else {
                "next_start": page_end.isoformat(),
                "end": end.isoformat(),
                "full": full,
            },
        )
        if finished:
            return tuple(found[dt] for dt in sorted(found))
        cursor = page_end


async def async_import_full_history(
    hass: HomeAssistant, client: EacClient, store: EacStatistics
) -> list[dict[str, Any]]:
    """Import every meter and both interval directions; propagate failures.

    A failed channel must not be presented as a successful complete import.
    Already saved channels can be replayed safely on the next attempt.
    """
    await store.async_load()
    now = datetime.now(UTC)
    results = []
    for sp in await client.list_service_points():
        for cfg in await client.get_meter_configs(sp.id):
            for ch in cfg.channels:
                if not is_energy(ch):
                    continue
                sid = statistic_id(sp.id, cfg, ch)
                start, end, _ = history_window(store, sid, cfg, now, 14, force=True)
                readings = await async_fetch_energy_history(
                    client, store, sp.id, cfg, ch, start, end, full=True
                )
                if not readings:
                    continue
                results.append(
                    {
                        "meter": cfg.serial_number,
                        "channel": ch.type,
                        "statistic_id": sid,
                        "readings": len(readings),
                        "from": start.isoformat(),
                        "to": end.isoformat(),
                    }
                )
    return results
