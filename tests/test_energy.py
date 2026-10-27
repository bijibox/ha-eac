"""Energy sources must not double count totals and tariff components."""

from custom_components.eac_cyprus.energy import migrate_sources


def test_legacy_migration_keeps_old_meter_and_export():
    sources = [
        {
            "type": "grid",
            "flow_from": [
                {"stat_energy_from": "old", "stat_cost": None},
                {"stat_energy_from": "daily", "stat_cost": "cost"},
                {"stat_energy_from": "peak"},
                {"stat_energy_from": "offpeak"},
            ],
            "flow_to": [{"stat_energy_to": "export", "stat_compensation": None}],
            "cost_adjustment_day": 0,
        }
    ]
    mapping = {
        "daily": "precise",
        "peak": "precise",
        "offpeak": "precise",
        "export": "precise_export",
    }
    result = migrate_sources(sources, mapping)
    assert [x["stat_energy_from"] for x in result[0]["flow_from"]] == ["old", "precise"]
    assert result[0]["flow_from"][1]["stat_cost"] == "cost"
    assert result[0]["flow_to"][0]["stat_energy_to"] == "precise_export"
    assert migrate_sources(result, mapping) == result
    assert len(sources[0]["flow_from"]) == 4


def test_flat_migration_and_unrelated_source():
    sources = [
        {
            "type": "grid",
            "stat_energy_from": "daily",
            "stat_energy_to": "export",
            "number_energy_price": 0.25,
        },
        {"type": "grid", "stat_energy_from": "peak"},
        {"type": "solar", "stat_energy_from": "pv"},
        {"type": "grid", "stat_energy_from": "old"},
    ]
    result = migrate_sources(
        sources, {"daily": "precise", "peak": "precise", "export": "precise_export"}
    )
    assert [x.get("stat_energy_from") for x in result] == ["precise", "pv", "old"]
    assert result[0]["number_energy_price"] == 0.25


async def test_migration_runs_only_after_full_import(
    hass, fake_active_meter, fake_meter_30min_channel
):
    from homeassistant.components.energy.data import async_get_manager

    from custom_components.eac_cyprus.energy import async_migrate_energy
    from custom_components.eac_cyprus.history import statistic_id
    from custom_components.eac_cyprus.statistics import (
        EacStatistics,
        external_statistic_id,
    )

    manager = await async_get_manager(hass)
    old = external_statistic_id("111", "mc_total_24h")
    manager.data = {
        "energy_sources": [
            {
                "type": "grid",
                "flow_from": [
                    {
                        "stat_energy_from": old,
                        "stat_cost": None,
                        "entity_energy_price": None,
                        "number_energy_price": 0.25,
                    }
                ],
                "flow_to": [],
                "cost_adjustment_day": 0,
            }
        ],
        "device_consumption": [],
        "device_consumption_water": [],
    }
    store = EacStatistics(hass, "migration")
    sid = statistic_id("111", fake_active_meter, fake_meter_30min_channel)
    store.series[sid] = {"points": {"2026-05-01T00:30:00+00:00": "0.123"}}
    await async_migrate_energy(hass, "111", fake_active_meter, store)
    assert manager.data["energy_sources"][0]["flow_from"][0]["stat_energy_from"] == old
    store.series[sid]["full_checked"] = "2026-05-01T00:00:00+00:00"
    await async_migrate_energy(hass, "111", fake_active_meter, store)
    assert manager.data["energy_sources"][0]["flow_from"][0]["stat_energy_from"] == sid


def test_meter_replacement_appends_without_changing_retired_source():
    from custom_components.eac_cyprus.energy import connect_replacement_meter

    sources = [
        {
            "type": "grid",
            "flow_from": [{"stat_energy_from": "eac_cyprus:111_old"}],
            "flow_to": [],
        }
    ]
    result = connect_replacement_meter(sources, "111", {"from": "eac_cyprus:111_new"})
    assert [x["stat_energy_from"] for x in result[0]["flow_from"]] == [
        "eac_cyprus:111_old",
        "eac_cyprus:111_new",
    ]
    assert (
        connect_replacement_meter(result, "111", {"from": "eac_cyprus:111_new"})
        == result
    )
    assert (
        connect_replacement_meter(sources, "222", {"from": "eac_cyprus:222_new"})
        == sources
    )
