"""Zeitgewichtete Kennzahlen mit expliziter Messabdeckung."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import Config
from .schedule import is_day


def analyze(path: Path):
    db = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    records = [json.loads(r[0]) for r in db.execute("SELECT payload FROM cycles ORDER BY rowid")]
    db.close()
    valid_seconds = total_seconds = band_seconds = weighted_soc = 0.0
    night_discharge = below45 = bypass = 0.0
    measured_seconds = 0.0
    ages, socs = [], []
    for i, record in enumerate(records):
        s, d = record["sample"], record["decision"]
        next_record = records[i + 1] if i + 1 < len(records) else None
        at = record.get("recorded_at") or (s or {}).get("observed_at")
        next_at = (
            (next_record.get("recorded_at") or (next_record["sample"] or {}).get("observed_at"))
            if next_record
            else None
        )
        dt = (
            (datetime.fromisoformat(next_at) - datetime.fromisoformat(at)).total_seconds()
            if at and next_at
            else 0
        )
        total_seconds += max(0, dt)
        if not s:
            continue
        if s["soc"] is not None:
            socs.append(s["soc"])
        age = d.get("data_age_s") if d else None
        if age is not None:
            ages.append(age)
        if i + 1 >= len(records) or not records[i + 1]["sample"]:
            continue
        nxt = records[i + 1]["sample"]
        if dt <= 0:
            continue
        # Keine Interpolation ueber lange Luecken oder doppelte Geraetezeitstempel.
        valid = (
            dt <= 600
            and s["valid"]
            and s["timestamp_verified"]
            and age is not None
            and 0 <= age <= 180
            and s["device_at"] != nxt["device_at"]
            and s["soc"] is not None
            and d
            and all(s.get(k) is not None for k in ("pv_w", "battery_w", "output_dc_w"))
        )
        if not valid:
            continue
        valid_seconds += dt
        band_seconds += dt * (45 <= s["soc"] <= 55)
        weighted_soc += dt * s["soc"]
        if s["battery_quality"] not in {"reported", "simulated"} or s["battery_w"] is None:
            continue
        measured_seconds += dt
        discharge = max(0, -s["battery_w"]) * dt / 3600
        below45 += discharge if s["soc"] < 45 else 0
        night_discharge += (
            discharge if not is_day(datetime.fromisoformat(s["observed_at"]), Config()) else 0
        )
        if s["battery_w"] >= 0 and s["output_dc_w"] is not None and s["pv_w"] is not None:
            bypass += min(s["pv_w"], s["output_dc_w"]) * dt / 3600
    return {
        "cycles": len(records),
        "writes": sum(r["result"]["writes"] for r in records),
        "communication_errors": sum(r["communication_error"] for r in records),
        "availability_percent": 100 * valid_seconds / total_seconds if total_seconds else None,
        "time_45_55_percent_of_valid": 100 * band_seconds / valid_seconds
        if valid_seconds
        else None,
        "minimum_soc": min(socs) if socs else None,
        "maximum_soc": max(socs) if socs else None,
        "mean_soc_time_weighted": weighted_soc / valid_seconds if valid_seconds else None,
        "data_age_mean_s": sum(ages) / len(ages) if ages else None,
        "data_age_max_s": max(ages) if ages else None,
        "energy_coverage_seconds": measured_seconds,
        "estimated_discharge_below45_wh": below45 if measured_seconds else None,
        "estimated_night_discharge_wh": night_discharge if measured_seconds else None,
        "estimated_pv_bypass_dc_wh": bypass if measured_seconds else None,
        "unplausible_decisions_percent": 100
        * sum(not r["decision"] or r["decision"]["state"] == "FAULT_SAFE" for r in records)
        / len(records)
        if records
        else None,
        "note": "Energie aus Stichproben, keine Zaehlerpraezision; Luecken werden nicht aufgefuellt. "
        "DC-Ausgang ist weder AC-Haushaltsverbrauch noch gemessene Netzeinspeisung.",
    }
