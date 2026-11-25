"""Synthetische Physik-/Firmwaremodelle, ausdruecklich keine Geraetebeweise."""

import asyncio
import csv
import json
import math
import random
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from .api_adapter import MockAdapter
from .config import Config
from .controller import Controller
from .models import Command, Sample
from .safety import Executor
from .schedule import BERLIN, active_command, is_day, mock_schedule
from .storage import Store


def observed_day() -> list[float]:
    """Rekonstruktion, keine Messreihe: 300 Wh, Peak 92,4 W um 10:20."""
    values = [0.0] * 1440
    for i in range(420):
        values[540 + i] = 92.4 * (i / 80 if i <= 80 else (420 - i) / 340)
    peak = 620
    factor = (300 * 60 - 92.4) / (math.fsum(values) - 92.4)
    return [v if i == peak else v * factor for i, v in enumerate(values)]


class Plant:
    """Nominelles Gleichstrommodell mit separatem Eigenverbrauch und Wirkungsgrad.

    Bypass <= Vorgabe/PV; Entladung schaltet PV ab. BMS-Floor 10 % nur Annahme.
    0-W-Erfolg ist ein Modellparameter, kein Ergebnis der Simulation.
    """

    def __init__(self, soc=50.0, idle_w=2.0, switch_seconds=600, zero_works=True):
        self.wh = soc * 16
        self.idle_w, self.switch_seconds, self.zero_works = idle_w, switch_seconds, zero_works
        self.low20 = self.deficit = 0
        self.discharge_hold = 0
        self.last = {
            "pv_w": 0.0,
            "battery_w": 0.0,
            "output_dc_w": 0.0,
            "bypass_w": 0.0,
            "mode": "standby",
            "idle_w": idle_w,
            "spilled_w": 0.0,
        }

    def step(self, available_pv, command: Command, *, seconds=60, temperature=10.0):
        permitted = command.allow_export or not self.zero_works
        low = available_pv < 20
        deficit = available_pv < 100 and command.appliance_load - available_pv >= 100
        self.low20 = self.low20 + seconds if permitted and low else 0
        self.deficit = self.deficit + seconds if permitted and deficit else 0
        trigger = permitted and (
            self.low20 > self.switch_seconds
            or self.deficit > self.switch_seconds
            or command.discharge_priority
        )
        if trigger:
            self.discharge_hold = 1800  # adverse Beispiel: 30 min PV-Ausfall
        if not permitted:
            self.discharge_hold = 0
        discharge = permitted and self.discharge_hold > 0 and self.wh > 160
        pv, output, bypass, charge, batt_out = available_pv, 0.0, 0.0, 0.0, 0.0
        if discharge:
            pv = 0.0
            batt_out = min(
                command.appliance_load, max(0.0, (self.wh - 160) * 0.95 * 3600 / seconds)
            )
            output = batt_out
            self.discharge_hold = max(0, self.discharge_hold - seconds)
        else:
            # Ladeprioritaet ist keine SOC-Obergrenze.
            if permitted and self.wh / 16 >= command.charge_priority_limit:
                output = bypass = min(pv, command.appliance_load)
            if 0 <= temperature <= 55:
                charge = min(max(0, pv - bypass), max(0, 1600 - self.wh) * 3600 / seconds / 0.95)
        stored = charge * 0.95
        removed = batt_out / 0.95
        actual_idle = min(self.idle_w, max(0, self.wh * 3600 / seconds + stored - removed))
        self.wh = max(0, min(1600, self.wh + (stored - removed - actual_idle) * seconds / 3600))
        # Verfuegbare Einstrahlung und tatsaechlich abgenommene PV strikt trennen.
        pv = bypass + charge
        self.last = {
            "pv_w": pv,
            "battery_w": charge - batt_out,
            "output_dc_w": output,
            "bypass_w": bypass,
            "mode": "discharge"
            if discharge
            else "charge"
            if charge
            else "bypass"
            if bypass
            else "standby",
            "idle_w": actual_idle,
            "spilled_w": max(0, available_pv - bypass - charge),
        }
        return self.last


def low_light_probe() -> list[dict]:
    rows = []
    for pv in (0, 10, 20, 40, 60, 80, 100):
        for enabled in (False, True):
            plant = Plant(soc=70, idle_w=0)
            for minute in range(1, 22):
                actual = plant.step(pv, Command(enabled, 100, 0 if enabled else 100))
                if minute in (1, 10, 11, 21):
                    rows.append(
                        {
                            "available_pv_w": pv,
                            "allow_export": enabled,
                            "setpoint_w": 100,
                            "minute": minute,
                            **actual,
                            "soc": plant.wh / 16,
                        }
                    )
    return rows


async def simulate(
    directory: Path,
    cfg: Config,
    *,
    days=7,
    seed=1600,
    initial_soc=50,
    idle_w=2,
    switch_seconds=600,
    zero_works=True,
    cloud_cliff=False,
):
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("Simulationsverzeichnis muss neu/leer sein")
    directory.mkdir(parents=True, exist_ok=True)
    store = Store(directory / "private")
    controller = Controller(cfg)
    plant = Plant(initial_soc, idle_w, switch_seconds, zero_works)
    start = datetime(2026, 11, 7, tzinfo=BERLIN)
    sample = Sample(
        start,
        start,
        initial_soc,
        0,
        0,
        0,
        temperature_c=10,
        owner=True,
        battery_quality="simulated",
        mode="standby",
        discharge_priority_known=True,
        schedule=mock_schedule(),
    )
    adapter = MockAdapter(sample)
    executor = Executor(adapter, store, cfg, execute=True)
    profile = observed_day()
    rng = random.Random(seed)
    totals = {
        "available_pv_wh": 0.0,
        "pv_bypass_dc_wh": 0.0,
        "battery_discharge_dc_wh": 0.0,
        "night_battery_discharge_dc_wh": 0.0,
        "discharge_below_45_wh": 0.0,
        "idle_wh_assumed": 0.0,
        "idle_wh_served": 0.0,
        "spilled_or_curtailed_wh": 0.0,
        "writes": 0,
        "soc_band_minutes": 0,
        "minimum_soc": initial_soc,
        "maximum_soc": initial_soc,
    }
    rows, socs, daily = [], [], []
    decision = None
    day_wh = 0.0
    action = "startup"
    try:
        for t in range(days * 1440):
            now = start + timedelta(minutes=t)
            day, m = divmod(t, 1440)
            if day == 0:
                available = profile[m]
            elif day in (1, 2, 3):
                available = profile[m] * (0, 0.01, 0.02)[day - 1]
            else:
                available = profile[m] * (4 if day == 4 else 0.9) * rng.uniform(0.25, 1.2)
            if cloud_cliff and day == 0 and 621 <= m < 635:
                available = 0.0
            if m == 0 and day:
                daily.append(day_wh)
                controller.weak_days = controller.weak_days + 1 if day_wh < cfg.weak_day_wh else 0
                day_wh = 0.0
            day_wh += available / 60
            if t % (cfg.cycle_s // 60) == 0:
                s = replace(
                    adapter.sample,
                    observed_at=now,
                    device_at=now,
                    soc=plant.wh / 16,
                    pv_w=plant.last["pv_w"],
                    battery_w=plant.last["battery_w"],
                    output_dc_w=plant.last["output_dc_w"],
                    mode=plant.last["mode"],
                )
                adapter.sample = s
                s = await adapter.read()
                executor.confirm_behavior(s)
                decision = controller.decide(s)
                result = await executor.apply(s, decision)
                totals["writes"] += result.writes
                action = result.action
                store.cycle(s, decision, result)
            cmd = active_command(adapter.sample.schedule, now)
            soc_before = plant.wh / 16
            actual = plant.step(available, cmd)
            soc = plant.wh / 16
            energy_out = max(0, -actual["battery_w"]) / 60
            totals["available_pv_wh"] += available / 60
            totals["pv_bypass_dc_wh"] += actual["bypass_w"] / 60
            totals["battery_discharge_dc_wh"] += energy_out
            totals["night_battery_discharge_dc_wh"] += energy_out if not is_day(now, cfg) else 0
            totals["discharge_below_45_wh"] += energy_out if soc_before < 45 else 0
            totals["idle_wh_assumed"] += idle_w / 60
            totals["idle_wh_served"] += actual["idle_w"] / 60
            totals["spilled_or_curtailed_wh"] += actual["spilled_w"] / 60
            totals["soc_band_minutes"] += int(45 <= soc <= 55)
            totals["minimum_soc"] = min(totals["minimum_soc"], soc)
            totals["maximum_soc"] = max(totals["maximum_soc"], soc)
            socs.append(soc)
            rows.append(
                {
                    "time": now.isoformat(),
                    "available_pv_w": available,
                    "soc": soc,
                    "state": str(decision.state),
                    "allow_export": cmd.allow_export,
                    "setpoint_w": cmd.appliance_load,
                    "action": action,
                    "reserve_low": decision.reserve_low,
                    **actual,
                }
            )
        daily.append(day_wh)
        totals.update(
            {
                "mean_soc": math.fsum(socs) / len(socs),
                "final_soc": socs[-1],
                "time_45_55_percent": 100 * totals["soc_band_minutes"] / len(socs),
                "daily_pv_wh": daily,
                "minutes": len(socs),
            }
        )
        with (directory / "timeline.csv").open("w") as out:
            writer = csv.DictWriter(out, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        report = {
            "kind": "SIMULATION, kein Geraetenachweis",
            "config": cfg.export(),
            "assumptions": {
                "capacity_wh": 1600,
                "charge_efficiency": 0.95,
                "discharge_efficiency": 0.95,
                "idle_w": idle_w,
                "zero_works": zero_works,
                "switch_seconds": switch_seconds,
                "cloud_cliff": cloud_cliff,
                "schedule_ack": "ideal; Fehler separat in Tests",
                "initial_soc": initial_soc,
                "seed": seed,
            },
            "metrics": totals,
        }
        (directory / "summary.json").write_text(json.dumps(report, indent=2))
        return report
    finally:
        store.close()


def run_simulation(*args, **kwargs):
    return asyncio.run(simulate(*args, **kwargs))
