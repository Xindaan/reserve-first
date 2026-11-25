"""Nur lesende Auswertung: Abfrageabdeckung ist keine Messwertverifikation."""

import argparse
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from solarbank_controller.config import Config
from solarbank_controller.schedule import fingerprint, is_day

CORE = ("soc", "pv_w", "battery_w", "output_dc_w", "mode")


def summarize(rows, status):
    if not rows:
        raise ValueError("Keine Snapshots")
    cfg = Config()
    rows = sorted(rows, key=lambda r: datetime.fromisoformat(r["observed_at"]))
    times = [datetime.fromisoformat(r["observed_at"]) for r in rows]
    groups = defaultdict(list)
    ages = []
    for r, at in zip(rows, times, strict=True):
        if r.get("device_at"):
            t = datetime.fromisoformat(r["device_at"])
            groups[t].append(r)
            ages.append((at - t).total_seconds())
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:], strict=False)]
    device_times = sorted(groups)
    device_gaps = [
        (b - a).total_seconds() for a, b in zip(device_times, device_times[1:], strict=False)
    ]
    conflicting = sum(len({tuple(r.get(k) for k in CORE) for r in v}) > 1 for v in groups.values())
    chronological = [datetime.fromisoformat(r["device_at"]) for r in rows if r.get("device_at")]
    backwards = sum(b < a for a, b in zip(chronological, chronological[1:], strict=False))
    day = [r for r, at in zip(rows, times, strict=True) if is_day(at, cfg)]
    night = [r for r, at in zip(rows, times, strict=True) if not is_day(at, cfg)]
    span = (times[-1] - times[0]).total_seconds()
    soc = [r["soc"] for r in rows if r.get("soc") is not None]
    discharge = [
        r
        for r in rows
        if r.get("mode") == "discharge" or (r.get("battery_w") is not None and r["battery_w"] < -3)
    ]
    night_discharge = [r for r in night if r in discharge]
    schedules = {fingerprint(r["schedule"]) for r in rows}
    slots = [slot for r in rows for slot in r["schedule"].get("ranges", [])]
    return {
        "final_24h": bool(
            status.get("state") == "finished"
            and span >= 86400
            and day
            and night
            and status.get("samples") == len(rows)
        ),
        "recorder_state": status.get("state"),
        "first_observed_at": times[0].isoformat(),
        "last_observed_at": times[-1].isoformat(),
        "span_hours": span / 3600,
        "samples": len(rows),
        "status_sample_count_matches": status.get("samples") == len(rows),
        "day_samples": len(day),
        "night_samples": len(night),
        "max_poll_gap_s": max(gaps, default=0),
        "poll_gaps_over_360_s": sum(x > 360 for x in gaps),
        "distinct_device_times": len(groups),
        "repeated_device_time_reads": len(chronological) - len(groups),
        "conflicting_duplicate_device_times": conflicting,
        "backward_device_time_steps": backwards,
        "max_device_time_gap_s": max(device_gaps, default=0),
        "device_age_max_s": max(ages, default=None),
        "device_age_over_180_s": sum(x > 180 for x in ages),
        "future_device_times": sum(x < -30 for x in ages),
        "timestamp_verified_samples": sum(bool(r.get("timestamp_verified")) for r in rows),
        "soc_start": rows[0].get("soc"),
        "soc_end": rows[-1].get("soc"),
        "soc_min": min(soc) if soc else None,
        "soc_max": max(soc) if soc else None,
        "observed_soc_outside_band": sum(not 45 <= x <= 55 for x in soc),
        "unknown_soc_samples": len(rows) - len(soc),
        "pv_peak_w": max((r["pv_w"] for r in rows if r.get("pv_w") is not None), default=None),
        "dc_peak_w": max(
            (r["output_dc_w"] for r in rows if r.get("output_dc_w") is not None), default=None
        ),
        "battery_min_w": min(
            (r["battery_w"] for r in rows if r.get("battery_w") is not None), default=None
        ),
        "discharge_samples": len(discharge),
        "night_discharge_samples": len(night_discharge),
        "night_dc_nonzero_samples": sum(
            r.get("output_dc_w") is not None and r["output_dc_w"] > 3 for r in night
        ),
        "unknown_battery_samples": sum(r.get("battery_w") is None for r in rows),
        "known_temperature_samples": sum(r.get("temperature_c") is not None for r in rows),
        "quality_counts": dict(Counter(r.get("battery_quality", "unknown") for r in rows)),
        "mode_counts": dict(Counter(r.get("mode", "unknown") for r in rows)),
        "firmware_values": sorted({r.get("firmware", "unknown") for r in rows}),
        "schedule_hash_count": len(schedules),
        "all_plan_export_off": bool(slots) and all(s.get("turn_on") is False for s in slots),
        "all_discharge_priority_off": bool(slots)
        and all(s.get("priority_discharge_switch") == 0 for s in slots),
        "note": "Stichproben und Poll-Abdeckung; keine Garantie zwischen Messpunkten, kein Energiezaehler. Tag/Nacht nach bestehendem Sonnenfenster mit 30-Minuten-Rand.",
    }


def analyze(directory):
    files = sorted(directory.glob("snapshot-*.json"))
    rows = [json.loads(p.read_text()) for p in files]
    status = json.loads((directory / "status.json").read_text())
    result = summarize(rows, status)
    db = sqlite3.connect(f"file:{(directory / 'telemetry.sqlite').resolve()}?mode=ro", uri=True)
    try:
        db_rows = db.execute(
            "SELECT count(*), min(json_extract(payload,'$.sample.soc')), max(json_extract(payload,'$.sample.soc')) FROM cycles WHERE json_extract(payload,'$.sample') IS NOT NULL"
        ).fetchone()
        result["database_crosscheck"] = {
            "samples": db_rows[0],
            "soc_min": db_rows[1],
            "soc_max": db_rows[2],
            "matches_snapshots": list(db_rows) == [len(rows), result["soc_min"], result["soc_max"]],
        }
        result["database_write_attempts"] = db.execute("SELECT count(*) FROM writes").fetchone()[0]
        result["database_read_errors"] = db.execute(
            "SELECT count(*) FROM cycles WHERE json_extract(payload,'$.communication_error')=1"
        ).fetchone()[0]
        result["logged_decisions"] = dict(
            db.execute(
                "SELECT json_extract(payload,'$.decision.reason'), count(*) FROM cycles WHERE json_extract(payload,'$.decision') IS NOT NULL GROUP BY 1"
            )
        )
        result["database_integrity"] = db.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        db.close()
    result["snapshot_set_sha256"] = hashlib.sha256(
        b"".join(p.name.encode() + b"\0" + p.read_bytes() for p in files)
    ).hexdigest()
    raw_endpoints = Counter(p.stem.split("-", 2)[-1] for p in directory.glob("raw-*.json"))
    result["raw_endpoint_counts"] = dict(raw_endpoints)
    result["only_read_endpoints_archived"] = bool(raw_endpoints) and set(raw_endpoints) <= {
        "site_list",
        "scene_info",
        "bind_devices",
        "get_device_load",
        "solar_info",
    }
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--require-finished", action="store_true")
    args = parser.parse_args()
    result = analyze(args.directory)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.require_finished and not result["final_24h"]:
        raise SystemExit(2)
