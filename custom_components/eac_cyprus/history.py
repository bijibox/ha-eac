"""Shared full/rolling history ingestion for polling and the import service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from eac_dso_portal import EacClient, MeasurementChannel, MeterConfig, as_utc
from homeassistant.core import HomeAssistant

from .const import channel_label
from .statistics import (
    INTERVAL_TYPES,
    EacStatistics,
    interval_statistic_id,
)

_MAX_LOOKBACK = timedelta(days=3650)
RECONCILE_INTERVAL = timedelta(days=7)


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
) -> tuple[datetime, datetime, bool]:
    data = store.series.get(sid, {})
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
    )
    return sid


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
                crs = await client.get_readings(sp.id, start, end, mc_id=ch.id)
                matching = next((cr for cr in crs if cr.channel_id == ch.id), None)
                if matching is None or not matching.readings:
                    continue
                await import_channel(
                    store, sp.id, cfg, ch, matching.readings, full=True, end=end
                )
                results.append(
                    {
                        "meter": cfg.serial_number,
                        "channel": ch.type,
                        "statistic_id": sid,
                        "readings": len(matching.readings),
                        "from": start.isoformat(),
                        "to": end.isoformat(),
                    }
                )
    return results
