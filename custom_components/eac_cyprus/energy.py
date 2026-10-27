"""Migrate selected EAC grid sources after precise history is available."""

from __future__ import annotations

from copy import deepcopy

from eac_dso_portal import MeterConfig
from homeassistant.components.energy.data import async_get_manager
from homeassistant.core import HomeAssistant

from .history import statistic_id
from .statistics import EacStatistics, external_statistic_id


def migrate_sources(sources: list[dict], replacements: dict[str, str]) -> list[dict]:
    """Preserve other devices, retired meters and existing pricing settings.

    Supports both HA's legacy flow lists and the newer flat grid schema.
    Deduplicate by final statistic ID, across grid source entries as well.
    """
    result = deepcopy(sources)
    seen_from: set[str] = set()
    seen_to: set[str] = set()
    for source in result:
        if source.get("type") != "grid":
            continue
        for flow, field, seen, cost in (
            ("flow_from", "stat_energy_from", seen_from, "stat_cost"),
            ("flow_to", "stat_energy_to", seen_to, "stat_compensation"),
        ):
            if flow in source:
                kept = []
                for item in source[flow]:
                    old = item[field]
                    new = replacements.get(old, old)
                    if new in seen and (
                        old in replacements or new in replacements.values()
                    ):
                        continue
                    seen.add(new)
                    item[field] = new
                    kept.append(item)
                source[flow] = kept
            elif old := source.get(field):
                new = replacements.get(old, old)
                if new in seen and (
                    old in replacements or new in replacements.values()
                ):
                    source[field] = None
                    source[cost] = None
                else:
                    source[field] = new
                    seen.add(new)
    return [
        s
        for s in result
        if s.get("type") != "grid"
        or (
            s.get("flow_from")
            or s.get("flow_to")
            or s.get("stat_energy_from")
            or s.get("stat_energy_to")
            or s.get("power")
            or s.get("power_config")
            or s.get("stat_rate")
        )
    ]


async def async_migrate_energy(
    hass: HomeAssistant, sp_id: str, cfg: MeterConfig, store: EacStatistics
) -> None:
    """Only replace this meter's already selected daily/tariff sources.

    Never switch until a complete history request succeeded. A retired meter's
    separate source is preserved; no interpolation is added to sparse readings.
    """
    replacements = {}
    precise = {}
    # v0.3.0 used naive-as-UTC dates and lifetime readings as sums. Use new
    # per-meter register IDs so old misdated rows cannot remain interleaved.
    for ch in cfg.channels:
        sid = statistic_id(sp_id, cfg, ch)
        data = store.series.get(sid, {})
        if not ch.interval and data.get("full_checked") and data.get("points"):
            replacements[external_statistic_id(sp_id, ch.id)] = sid
    for interval_type, old_types in (
        ("KWH-30MIN-LP-IMP", {"S-KWH", "S-KWH-24H", "S-KWH-NORMAL", "S-KWH-OFFPEAK"}),
        ("KWH-30MIN-LP-EXP", {"S-KWH-EXP"}),
    ):
        channel = next((ch for ch in cfg.channels if ch.type == interval_type), None)
        if channel is None:
            continue
        sid = statistic_id(sp_id, cfg, channel)
        data = store.series.get(sid, {})
        if not data.get("full_checked") or not data.get("points"):
            continue
        if cfg.removal_date is None and not data.get("energy_connected"):
            precise["from" if interval_type.endswith("IMP") else "to"] = sid
        for old in cfg.channels:
            if old.type in old_types:
                replacements[external_statistic_id(sp_id, old.id)] = sid
                replacements[statistic_id(sp_id, cfg, old)] = sid
    if not replacements and not precise:
        return
    manager = await async_get_manager(hass)
    if manager.data is None:
        return
    sources = manager.data["energy_sources"]
    migrated = connect_replacement_meter(
        migrate_sources(sources, replacements), sp_id, precise
    )
    if sources != migrated:
        await manager.async_update({"energy_sources": migrated})
    # Mark only a source already present before this update; HA persists its
    # Energy preferences asynchronously. A crash before that save must retry.
    selected = _selected_ids(sources)
    marked = False
    for sid in precise.values():
        if sid in selected:
            store.series[sid]["energy_connected"] = True
            marked = True
    if marked:
        await store.store.async_save(store.series)


def _selected_ids(sources: list[dict]) -> set[str]:
    ids = set()
    for source in sources:
        if source.get("type") != "grid":
            continue
        for flow, field in (
            ("flow_from", "stat_energy_from"),
            ("flow_to", "stat_energy_to"),
        ):
            if flow in source:
                ids.update(item[field] for item in source[flow])
            elif source.get(field):
                ids.add(source[field])
    return ids


def connect_replacement_meter(
    sources: list[dict], sp_id: str, precise: dict[str, str]
) -> list[dict]:
    """Append a replacement to an already configured service point."""
    selected = _selected_ids(sources)
    if not any(sid.startswith(f"eac_cyprus:{sp_id}_") for sid in selected):
        return sources
    missing = {
        direction: sid for direction, sid in precise.items() if sid not in selected
    }
    if not missing:
        return sources
    result = deepcopy(sources)
    grid = next(s for s in result if s.get("type") == "grid")
    if "flow_from" in grid:
        for direction, sid in missing.items():
            cost = "stat_cost" if direction == "from" else "stat_compensation"
            grid[f"flow_{direction}"].append(
                {
                    f"stat_energy_{direction}": sid,
                    cost: None,
                    "entity_energy_price": None,
                    "number_energy_price": None,
                }
            )
    else:
        result.append(
            {
                "type": "grid",
                "stat_energy_from": missing.get("from"),
                "stat_energy_to": missing.get("to"),
                "stat_cost": None,
                "stat_compensation": None,
                "entity_energy_price": None,
                "number_energy_price": None,
                "entity_energy_price_export": None,
                "number_energy_price_export": None,
                "cost_adjustment_day": 0,
            }
        )
    return result
