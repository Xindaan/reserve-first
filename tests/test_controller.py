from dataclasses import replace
from datetime import timedelta

import pytest
from conftest import stabilized

from solarbank_controller.config import Config
from solarbank_controller.controller import Controller
from solarbank_controller.models import Command, State


@pytest.mark.parametrize(
    "soc,pv,state",
    [
        (30, 0, State.NIGHT_SAFE),
        (44, 300, State.CHARGE_PROTECT),
        (50, 100, State.HOLD),
        (56, 450, State.PV_BYPASS),
        (80, 600, State.PV_BYPASS),
    ],
)
def test_requested_nominal_cases(sample, soc, pv, state):
    controller = Controller(Config(reserve_mode="nominal"))
    d = stabilized(controller, replace(sample, soc=soc, pv_w=pv, battery_w=pv))
    assert d.state == state
    assert d.command.discharge_priority is False
    assert d.command.allow_export is (state == State.PV_BYPASS)
    assert d.command.appliance_load == 100


@pytest.mark.parametrize("hour,soc", [(17, 48), (0, 70), (4, 100)])
def test_night_beats_soc(sample, hour, soc):
    now = sample.observed_at.replace(hour=hour)
    d = Controller(Config()).decide(replace(sample, observed_at=now, device_at=now, soc=soc))
    assert d.state == State.NIGHT_SAFE and not d.command.allow_export


def test_sudden_cloud_and_discharge_preempts_dwell(sample):
    c = Controller(Config(reserve_mode="nominal"))
    assert stabilized(c, sample).command.allow_export
    now = sample.observed_at + timedelta(minutes=25)
    s = replace(sample, observed_at=now, device_at=now, soc=55, pv_w=10, battery_w=10)
    assert not c.decide(s).command.allow_export
    s = replace(s, battery_w=-100, output_dc_w=100, pv_w=0, mode="discharge")
    d = c.decide(s)
    assert d.state == State.FAULT_SAFE and d.alarms and not d.command.allow_export
    assert not stabilized(c, replace(sample, observed_at=now, device_at=now)).command.allow_export


def test_stale_and_frozen_data(sample):
    c = Controller(Config())
    late = replace(sample, observed_at=sample.observed_at + timedelta(minutes=5))
    assert c.decide(late).command is None
    # Gleichbleibender SOC allein ist kein Nachweis fuer einen eingefrorenen Sensor.
    assert stabilized(Controller(Config()), sample).command.allow_export


def test_standby_age_does_not_enable_export(sample):
    s = replace(sample, mode="standby", observed_at=sample.observed_at + timedelta(minutes=30))
    d = Controller(Config()).decide(s)
    assert d.command is not None and not d.command.allow_export
    s = replace(s, observed_at=sample.observed_at + timedelta(hours=2))
    assert Controller(Config()).decide(s).command is None


@pytest.mark.parametrize("inverter", ["unknown", "MI60", "third-party-150W"])
def test_unverified_inverters_blocked(sample, inverter):
    assert Controller(Config()).decide(replace(sample, inverter=inverter)).command is None


@pytest.mark.parametrize("temperature", [-5, 56, None])
def test_temperature_does_not_force_discharge(sample, temperature):
    d = stabilized(Controller(Config()), replace(sample, temperature_c=temperature))
    assert not d.command.allow_export and d.alarms


def test_discharge_priority_known_and_off_required(sample):
    for s in (
        replace(sample, discharge_priority_known=False),
        replace(sample, active=Command(True, 100, 0, True)),
    ):
        d = Controller(Config()).decide(s)
        assert d.state == State.FAULT_SAFE and not d.command.allow_export


def test_estimates_never_authorize_bypass(sample):
    d = stabilized(Controller(Config()), replace(sample, battery_quality="estimated"))
    assert not d.command.allow_export


def test_adaptive_reserve(sample):
    c = Controller(Config(reserve_mode="adaptive"))
    assert c.reserves() == (60, 65)
    c.weak_days = 3
    assert c.reserves() == (65, 70)
    d = stabilized(c, replace(sample, soc=64))
    assert not d.command.allow_export


def test_continuous_only_above_minimum(sample):
    d = stabilized(
        Controller(Config(strategy="continuous")), replace(sample, pv_w=450, battery_w=450)
    )
    assert d.command.appliance_load == 360


def test_hysteresis_keeps_export_above_reserve(sample):
    c = Controller(Config(reserve_mode="nominal"))
    assert stabilized(c, sample).command.allow_export
    now = sample.observed_at + timedelta(minutes=25)
    s = replace(sample, soc=50, active=Command(True, 100, 0), observed_at=now, device_at=now)
    assert c.decide(s).command.allow_export
    s = replace(s, soc=44)
    assert not c.decide(s).command.allow_export


def test_nan_and_future_and_missing_time(sample):
    for s in (
        replace(sample, soc=float("nan")),
        replace(sample, device_at=None),
        replace(sample, device_at=sample.device_at + timedelta(hours=1)),
        replace(sample, timestamp_verified=False),
    ):
        assert Controller(Config()).decide(s).command is None


def test_missing_pv_history_is_not_stable(sample):
    c = Controller(Config())
    c.decide(sample)
    now = sample.observed_at + timedelta(minutes=20)
    assert not c.decide(replace(sample, observed_at=now, device_at=now)).command.allow_export


def test_unconfirmed_dry_run_proposal_does_not_oscillate(sample):
    c = Controller(Config())
    for m in range(0, 45, 5):
        now = sample.observed_at + timedelta(minutes=m)
        d = c.decide(replace(sample, observed_at=now, device_at=now))
        if m >= 15:
            assert d.state == State.PV_BYPASS
