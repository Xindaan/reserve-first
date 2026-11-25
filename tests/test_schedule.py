import copy
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from solarbank_controller.api_adapter import device_time
from solarbank_controller.config import Config
from solarbank_controller.models import Command
from solarbank_controller.schedule import (
    BERLIN,
    active_command,
    fingerprint,
    mock_schedule,
    propose,
    validate_schedule,
)


@pytest.mark.parametrize(
    "bad", ["single", "gap", "overlap", "backwards", "short", "invalid_time", "zero_load"]
)
def test_invalid_schedules(bad):
    plan = mock_schedule()
    if bad == "single":
        plan["ranges"] = [dict(plan["ranges"][0], end_time="24:00")]
    elif bad == "gap":
        plan["ranges"][1]["start_time"] = "12:01"
    elif bad == "overlap":
        plan["ranges"][1]["start_time"] = "11:59"
    elif bad == "backwards":
        plan["ranges"].reverse()
    elif bad == "short":
        plan["ranges"][1]["end_time"] = "23:59"
    elif bad == "invalid_time":
        plan["ranges"][1]["end_time"] = "24:01"
    else:
        plan["ranges"][0]["appliance_loads"][0]["power"] = 0
    with pytest.raises(ValueError):
        validate_schedule(plan)


def test_plan_copy_no_unknown_field_loss(sample):
    plan = copy.deepcopy(sample.schedule)
    plan["vendor_extension"] = {"keep": 42}
    plan["ranges"][0]["vendor_extension"] = ["keep"]
    old = fingerprint(plan)
    new = propose(plan, Command(True, 100, 0), sample.observed_at, Config())
    assert fingerprint(plan) == old
    assert new["vendor_extension"] == plan["vendor_extension"]
    assert new["ranges"][0]["vendor_extension"] == ["keep"]
    validate_schedule(new, winter=True)
    assert active_command(new, sample.observed_at).allow_export
    assert not active_command(new, sample.observed_at + timedelta(minutes=10)).allow_export
    assert not active_command(new, sample.observed_at.replace(hour=0)).allow_export
    # Dokumentierter verbleibender Fehlerfall: Gen 1 wiederholt den Plan taeglich.
    assert active_command(new, sample.observed_at + timedelta(days=1)).allow_export


@pytest.mark.parametrize("date", [(2026, 3, 29), (2026, 10, 25)])
def test_dst_daylight_and_repeated_hour(date, sample):
    now = datetime(*date, 12, tzinfo=BERLIN)
    plan = propose(sample.schedule, Command(True, 100, 0), now, Config())
    for fold in (0, 1):
        assert not active_command(plan, now.replace(hour=2, minute=30, fold=fold)).allow_export
    assert active_command(plan, now).allow_export


@pytest.mark.parametrize(
    "value", ["2026-10-25 02:30:00", "2026-03-29 02:30:00", "1970-01-01 00:00:00", "", None]
)
def test_ambiguous_invalid_timestamps_rejected(value):
    assert device_time(value, "Europe/Berlin") is None


def test_no_schedule_at_night(sample):
    with pytest.raises(ValueError):
        propose(
            sample.schedule, Command(True, 100, 0), sample.observed_at.replace(hour=0), Config()
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"pv_stop_w": 20},
        {"soc_low": 44},
        {"max_writes_hour": 100},
        {"stable_s": 0},
        {"lease_minutes": 11},
        {"cycle_s": 5},
        {"pv_start_w": float("nan")},
        {"timestamp_verified": "true"},
    ],
)
def test_config_rejects_unsafe_values(kwargs):
    with pytest.raises((ValueError, TypeError)):
        replace(Config(), **kwargs)
