import asyncio
import copy
import inspect
import json
from dataclasses import fields
from unittest.mock import AsyncMock

import pytest

from solarbank_controller.api_adapter import CloudAdapter, cloud_connection, parse_scene
from solarbank_controller.config import Config
from solarbank_controller.schedule import mock_schedule


def raw(sample):
    return {
        "solarbank_info": {
            "updated_time": "2026-11-07 12:00:00",
            "solarbank_list": [
                {
                    "device_sn": sample.device_id,
                    "device_pn": "A17C0",
                    "battery_power": "70",
                    "photovoltaic_power": "60",
                    "output_power": "60",
                    "charging_power": "60",
                    "charging_status": "1",
                    "main_version": "2.0.9",
                }
            ],
        }
    }


def parse(sample, scene):
    return parse_scene(
        scene,
        {"home_load_data": sample.schedule},
        {"solar_model": "A5143"},
        device_id=sample.device_id,
        site_id=sample.site_id,
        owner=True,
        cfg=Config(timestamp_verified=True),
        now=sample.observed_at,
    )


def test_raw_charging_power_is_not_battery_discharge(sample):
    parsed = parse(sample, raw(sample))
    assert parsed.battery_w == 0 and parsed.battery_quality == "estimated"
    assert parsed.output_dc_w == 60 and parsed.active.appliance_load == 100
    assert parsed.household_ac_w is None and parsed.temperature_c is None


def test_reported_discharge_preempts_pv_estimate(sample):
    scene = raw(sample)
    scene["solarbank_info"]["solarbank_list"][0].update(
        bat_charge_power="0", bat_discharge_power="100"
    )
    assert parse(sample, scene).battery_w == -100


def test_bad_sensor_number_not_zero_filled(sample):
    scene = raw(sample)
    scene["solarbank_info"]["solarbank_list"][0]["photovoltaic_power"] = ""
    assert parse(sample, scene).pv_w is None
    scene["solarbank_info"]["updated_time"] = ""
    assert parse(sample, scene).device_at is None


def test_cloud_adapter_only_documented_reads(sample):
    api = AsyncMock()
    api.get_site_list.return_value = {"site_list": [{"site_id": sample.site_id, "ms_type": 1}]}
    api.get_scene_info.return_value = raw(sample)
    api.get_bind_devices.return_value = {"data": []}
    api.get_device_load.return_value = {"home_load_data": sample.schedule}
    api.get_solar_info.return_value = {"solar_model": "A5143"}
    adapter = CloudAdapter(api, Config(), sample.site_id, sample.device_id)
    s = asyncio.run(adapter.read())
    assert s.model == "A17C0" and s.inverter == "MI80" and not s.timestamp_verified
    assert [c[0] for c in api.mock_calls] == [
        "get_site_list",
        "get_scene_info",
        "get_bind_devices",
        "get_device_load",
        "get_solar_info",
    ]


def test_installed_sdk_contract_and_commit_pin():
    api = pytest.importorskip("anker_solix_api.api")
    from importlib.metadata import distribution

    from anker_solix_api.apitypes import SolarbankTimeslot

    dist = distribution("anker-solix-api")
    direct = json.loads(dist.read_text("direct_url.json"))
    assert direct["vcs_info"]["commit_id"] == "646e2b7bc314321527e7ec41070367154e001a3c"
    assert dist.version == "3.9.1"
    for method in (
        "get_bind_devices",
        "get_site_list",
        "get_scene_info",
        "get_device_load",
        "get_solar_info",
        "set_device_parm",
        "set_home_load",
    ):
        assert inspect.iscoroutinefunction(getattr(api.AnkerSolixApi, method))
    assert {"allow_export", "appliance_load", "charge_priority_limit", "discharge_priority"} <= {
        f.name for f in fields(SolarbankTimeslot)
    }
    assert {
        "preset",
        "export",
        "charge_prio",
        "discharge_prio",
        "insert_slot",
        "test_schedule",
    } <= set(inspect.signature(api.AnkerSolixApi.set_home_load).parameters)


def test_upstream_schedule_mapping_runs_offline(tmp_path):
    pytest.importorskip("anker_solix_api.api")
    from aiohttp import ClientSession
    from anker_solix_api.api import AnkerSolixApi

    async def go():
        async with ClientSession() as web:
            api = AnkerSolixApi(
                email="offline@example.invalid", password="not-real", countryId="DE", websession=web
            )
            api.apisession._authFile = str(tmp_path / "auth.json")
            api.apisession.request = AsyncMock(
                side_effect=AssertionError("Netzwerk ist in diesem Test verboten")
            )
            plan = mock_schedule()
            plan["is_show_priority_discharge"] = 1
            result = await api.set_home_load(
                siteId="mock-site",
                deviceSn="mock-device",
                all_day=True,
                preset=100,
                export=False,
                charge_prio=100,
                discharge_prio=0,
                test_schedule=copy.deepcopy(plan),
                test_count=1,
            )
            assert all(s["turn_on"] is False for s in result["ranges"])
            assert all(s["priority_discharge_switch"] == 0 for s in result["ranges"])
            assert all(s["appliance_loads"][0]["power"] == 100 for s in result["ranges"])
            api.apisession.request.assert_not_called()

    asyncio.run(go())


def test_read_only_transport_denies_real_setter(tmp_path, monkeypatch):
    pytest.importorskip("anker_solix_api.api")
    from solarbank_controller.api_adapter import WriteBlocked

    monkeypatch.setenv("ANKER_EMAIL", "offline@example.invalid")
    monkeypatch.setenv("ANKER_PASSWORD", "not-real")

    async def go():
        async with cloud_connection(Config(), tmp_path) as adapter:
            with pytest.raises(WriteBlocked):
                await adapter.api.set_device_parm(
                    siteId="test", paramData=mock_schedule(), deviceSn="test"
                )

    asyncio.run(go())


@pytest.mark.parametrize("watts", [82, 115])
def test_live_zero_battery_fields_cannot_hide_discharge(sample, watts):
    scene = raw(sample)
    scene["solarbank_info"]["solarbank_list"][0].update(
        bat_charge_power="0",
        bat_discharge_power="0",
        photovoltaic_power="0",
        output_power=str(watts),
        charging_power=str(watts),
        charging_status="2",
    )
    result = parse(sample, scene)
    assert result.battery_w == -watts
    assert result.battery_quality == "estimated"
    assert result.mode == "discharge"


@pytest.mark.parametrize(
    "pv,dc,charge,discharge,battery,quality",
    [
        (0, 0, 0, 0, 0, "reported"),
        (42, 0, 37, 0, 37, "reported"),
        (60, 60, 0, 100, -100, "reported"),
        (0, 82, 37, 0, -82, "estimated"),
        (42, 0, 0, 0, 42, "estimated"),
        (60, 62, 0, 0, 0, "reported"),
    ],
)
def test_balance_fix_preserves_known_clean_and_conservative_cases(
    sample, pv, dc, charge, discharge, battery, quality
):
    scene = raw(sample)
    scene["solarbank_info"]["solarbank_list"][0].update(
        photovoltaic_power=str(pv),
        output_power=str(dc),
        bat_charge_power=str(charge),
        bat_discharge_power=str(discharge),
    )
    result = parse(sample, scene)
    assert (result.battery_w, result.battery_quality) == (battery, quality)
