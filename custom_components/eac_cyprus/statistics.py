"""Deterministic external statistics, rebuilt from persisted source readings."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256

from eac_dso_portal import Reading, as_utc
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

INTERVAL_TYPES = {"KWH-30MIN-LP-IMP", "KWH-30MIN-LP-EXP"}
PRECISION = Decimal("0.001")


def external_statistic_id(sp_id: str, channel_id: str) -> str:
    """Keep existing cumulative IDs compatible with 0.3.0."""
    return f"{DOMAIN}:{sp_id}_{channel_id}"


def interval_statistic_id(sp_id: str, meter_identity: str, channel_id: str) -> str:
    """Physical meters never share an accumulator, even if a channel ID is reused."""
    identity = sha256(f"{meter_identity}:{channel_id}".encode()).hexdigest()[:24]
    return f"{DOMAIN}:{sp_id}_{identity}_energy"


def _metadata(statistic_id: str, name: str) -> StatisticMetaData:
    return {
        "mean_type": StatisticMeanType.NONE,
        "has_sum": True,
        "name": name,
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_class": "energy",
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }


def build_statistics(
    points: dict[str, str], interval: bool, position: str = "end"
) -> list[StatisticData]:
    """Use Decimal until recorder serialization; sums are kWh to 1 Wh.

    A baseline row precedes the first interval so HA includes that interval.
    Sparse registers instead begin at sum=0 at their first actual observation.
    They are never interpolated or treated as consumption since meter zero.
    """
    if not points:
        return []
    ordered = sorted(
        (datetime.fromisoformat(dt), Decimal(value)) for dt, value in points.items()
    )
    if interval:
        hours: dict[datetime, Decimal] = defaultdict(Decimal)
        for dt, value in ordered:
            if position == "end":
                dt -= timedelta(minutes=30)
            hours[dt.replace(minute=0, second=0, microsecond=0)] += value
        first = min(hours)
        rows: list[StatisticData] = [{"start": first - timedelta(hours=1), "sum": 0.0}]
        total = Decimal(0)
        # Explicit zero-change hours avoid assigning the first post-gap interval
        # to a previous day/month when the dashboard differences samples.
        hour = first
        last = max(hours)
        while hour <= last:
            total += hours.get(hour, Decimal(0))
            rows.append({"start": hour, "sum": float(total.quantize(PRECISION))})
            hour += timedelta(hours=1)
        return rows

    total = Decimal(0)
    previous = ordered[0][1]
    sparse: dict[datetime, StatisticData] = {}
    for dt, value in ordered:
        # A decreasing register establishes a new baseline; its initial value
        # cannot prove any consumption. Subsequent deltas remain usable.
        total += max(value - previous, Decimal(0))
        previous = value
        start = dt.replace(minute=0, second=0, microsecond=0)
        sparse[start] = {
            "start": start,
            "sum": float(total.quantize(PRECISION)),
            "state": float(value),
        }
    return list(sparse.values())


class EacStatistics:
    """Durable raw ledger. Replaying it after a crash is an idempotent upsert.

    Persist before enqueueing recorder jobs. On startup replay all rows, even
    when the portal is unavailable. Corrections rebuild all subsequent sums.
    The coordinator's lock serializes polling and the full-history service.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self.store = Store(hass, 1, f"{DOMAIN}.{entry_id}.history")
        self.series: dict[str, dict] = {}
        self.loaded = False
        self._pending: set[str] = set()

    def latest_reading(self, sid: str) -> Reading | None:
        """Keep the latest known sensor value when a short poll returns no rows."""
        data = self.series.get(sid, {})
        points = data.get("points", {})
        if not points:
            return None
        key = max(points, key=datetime.fromisoformat)
        value = float(points[key])
        return Reading(
            dt=datetime.fromisoformat(key),
            value=value,
            reading=None if data["interval"] else value,
        )

    async def async_load(self) -> None:
        if self.loaded:
            return
        self.series = await self.store.async_load() or {}
        for sid, data in self.series.items():
            await self._publish(sid, data)
        self.loaded = True

    async def _publish(self, sid: str, data: dict) -> None:
        rows = await self.hass.async_add_executor_job(
            build_statistics,
            data["points"],
            data["interval"],
            data.get("position", "end"),
        )
        if rows:
            async_add_external_statistics(self.hass, _metadata(sid, data["name"]), rows)

    async def async_merge(
        self,
        sid: str,
        name: str,
        readings: tuple[Reading, ...],
        *,
        interval: bool,
        position: str = "end",
        full: bool = False,
        fetched_through: datetime | None = None,
        progress: dict | None = None,
    ) -> None:
        await self.async_load()
        old = self.series.get(sid, {})
        if old and old.get("position", "end") != position:
            raise ValueError(
                "Interval timestamp position cannot change after import; use a new statistic ID"
            )
        points = dict(old.get("points", {}))
        changed: list[str] = []
        for reading in readings:
            value = reading.value if interval else reading.reading
            if value is None:
                continue
            energy = Decimal(str(value))
            if not energy.is_finite() or energy < 0:
                raise ValueError("Energy must be finite and nonnegative")
            dt = as_utc(reading.dt)
            if interval and (dt.minute not in (0, 30) or dt.second or dt.microsecond):
                raise ValueError(
                    "Load profile timestamp is not on a half-hour boundary"
                )
            key = dt.isoformat()
            if points.get(key) != str(energy):
                points[key] = str(energy)
                changed.append(key)
        data = {
            **old,
            "name": name,
            "points": points,
            "interval": interval,
            "position": position,
        }
        if full:
            data["full_checked"] = datetime.now(UTC).isoformat()
        if fetched_through:
            data["fetched_through"] = fetched_through.isoformat()
        if progress is not None:
            data["import_progress"] = progress
        else:
            data.pop("import_progress", None)
        if changed or full:
            self._pending.add(sid)
        self.series[sid] = data
        await self.store.async_save(self.series)
        if sid in self._pending:
            # Cumulative backfill changes the baseline, so rewrite every sum.
            await self._publish(sid, data)
            self._pending.discard(sid)
