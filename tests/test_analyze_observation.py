"""Auswertung trennt vollstaendige Zeitspanne von tatsaechlicher Datenqualitaet."""

import importlib
import sys
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
try:
    summarize = importlib.import_module("scripts.analyze_observation").summarize
finally:
    sys.path.pop(0)


def row(sample, hours=0, **changes):
    at = sample.observed_at + timedelta(hours=hours)
    d = asdict(replace(sample, observed_at=at, device_at=at, **changes))
    for key in ("observed_at", "device_at"):
        d[key] = d[key].isoformat()
    return d


def test_running_is_not_complete_even_after_24_hours(sample):
    rows = [row(sample, h) for h in (0, 12, 24)]
    assert not summarize(rows, {"state": "running", "samples": 3})["final_24h"]
    result = summarize(rows, {"state": "finished", "samples": 3})
    assert result["final_24h"]
    assert result["max_poll_gap_s"] == 43200  # keine lueckenlose Messung vortaeuschen
    assert result["poll_gaps_over_360_s"] == 2
    assert not summarize(rows, {"state": "finished", "samples": 4})["final_24h"]


def test_repeated_stale_device_time_not_counted_as_new_measurement(sample):
    first = row(sample)
    second = dict(first, observed_at=(sample.observed_at + timedelta(minutes=20)).isoformat())
    result = summarize([first, second], {"state": "running", "samples": 2})
    assert result["distinct_device_times"] == 1
    assert result["repeated_device_time_reads"] == 1
    assert result["device_age_max_s"] == 1200
    assert result["device_age_over_180_s"] == 1
    assert result["conflicting_duplicate_device_times"] == 0
    second["soc"] = 51
    assert summarize([first, second], {})["conflicting_duplicate_device_times"] == 1


def test_negative_battery_and_discharge_mode_both_count(sample):
    rows = [
        row(sample, 12, soc=51, battery_w=-20, mode="bypass"),
        row(sample, 12.1, soc=51, battery_w=0, mode="discharge"),
    ]
    result = summarize(rows, {})
    assert result["discharge_samples"] == result["night_discharge_samples"] == 2


def test_pv_bypass_is_not_battery_discharge(sample):
    result = summarize(
        [row(sample, soc=54, pv_w=60, output_dc_w=60, battery_w=0, mode="bypass")], {}
    )
    assert result["discharge_samples"] == 0 and result["dc_peak_w"] == 60
    assert result["observed_soc_outside_band"] == 0


def test_unknowns_and_outside_band_are_explicit(sample):
    rows = [row(sample, soc=None, battery_w=None, temperature_c=None), row(sample, 1, soc=56)]
    result = summarize(rows, {})
    assert result["unknown_soc_samples"] == result["unknown_battery_samples"] == 1
    assert result["observed_soc_outside_band"] == 1
    assert result["known_temperature_samples"] == 1


def test_device_time_regression_and_schedule_change(sample):
    rows = [row(sample), row(sample, 1)]
    rows[1]["device_at"] = (sample.device_at - timedelta(minutes=1)).isoformat()
    rows[1]["schedule"] = {"ranges": [{"turn_on": True, "priority_discharge_switch": 1}]}
    result = summarize(rows, {})
    assert result["backward_device_time_steps"] == 1
    assert result["schedule_hash_count"] == 2
    assert not result["all_plan_export_off"]
    assert not result["all_discharge_priority_off"]


def test_empty_observation_rejected():
    with pytest.raises(ValueError):
        summarize([], {})
