import math
from dataclasses import replace

import pytest

from solarbank_controller.config import Config
from solarbank_controller.models import Command
from solarbank_controller.simulation import Plant, low_light_probe, observed_day, run_simulation


def test_observed_constraints_exact():
    pv = observed_day()
    assert math.fsum(pv) / 60 == pytest.approx(300, abs=1e-9)
    assert max(pv) == 92.4 and pv.index(max(pv)) == 10 * 60 + 20
    assert not any(pv[:540]) and not any(pv[960:])


@pytest.mark.parametrize("pv", [0, 10, 20, 40, 60, 80, 100])
def test_seven_light_levels(pv):
    plant = Plant(soc=70, idle_w=0)
    cmd = Command(True, 100, 0)
    for _ in range(10):
        row = plant.step(pv, cmd)
        assert row["battery_w"] >= 0
        assert row["output_dc_w"] == pv
    row = plant.step(pv, cmd)
    if pv < 20:
        assert row["battery_w"] == -100 and row["pv_w"] == 0
    else:
        assert row["battery_w"] == 0 and row["output_dc_w"] == pv


def test_second_anker_rule_and_early_firmware_failure():
    plant = Plant(soc=70, idle_w=0)
    for _ in range(11):
        row = plant.step(60, Command(True, 200, 0))
    assert row["battery_w"] == -200 and row["pv_w"] == 0
    early = Plant(soc=70, idle_w=0, switch_seconds=0)
    assert early.step(10, Command(True, 100, 0))["battery_w"] == -100


def test_zero_success_is_assumption_not_proof():
    working, broken = Plant(), Plant(zero_works=False)
    for _ in range(11):
        good = working.step(0, Command())
        bad = broken.step(0, Command())
    assert good["battery_w"] == 0
    assert bad["battery_w"] == -100


def test_shade_cannot_be_charged():
    plant = Plant(soc=50, idle_w=2)
    for _ in range(3 * 1440):
        plant.step(0, Command())
    assert plant.wh / 16 == pytest.approx(41, abs=1e-8)


def test_low_light_probe_covers_matrix():
    rows = low_light_probe()
    assert len(rows) == 7 * 2 * 4
    assert all(r["battery_w"] >= 0 for r in rows if not r["allow_export"])


def test_full_and_empty_plant_obey_energy_balance():
    for soc, pv, cmd in [
        (100, 800, Command()),
        (0, 0, Command()),
        (10.01, 0, Command(True, 800, 0)),
    ]:
        plant = Plant(soc=soc, idle_w=5, switch_seconds=0)
        initial = plant.wh
        r = plant.step(pv, cmd)
        battery = r["battery_w"]
        expected = initial + (max(0, battery) * 0.95 - max(0, -battery) / 0.95 - r["idle_w"]) / 60
        assert plant.wh == pytest.approx(expected)
        assert r["pv_w"] + r["spilled_w"] == pytest.approx(pv)
        assert r["pv_w"] == pytest.approx(r["bypass_w"] + max(0, battery))


def test_simulation_exercises_controller_transaction_and_energy(tmp_path):
    out = tmp_path / "run"
    r = run_simulation(out, replace(Config(), reserve_mode="nominal"), days=1, initial_soc=70)
    m = r["metrics"]
    assert m["available_pv_wh"] == pytest.approx(300)
    assert m["writes"] > 0 and m["pv_bypass_dc_wh"] > 0
    assert m["battery_discharge_dc_wh"] == 0
    assert m["night_battery_discharge_dc_wh"] == 0
    assert (out / "timeline.csv").is_file()
    assert r["assumptions"]["zero_works"] is True
