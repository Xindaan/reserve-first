"""Reproduzierbarer Vergleich; Zielverzeichnis darf noch nicht existieren."""

import argparse
import json
from dataclasses import replace
from pathlib import Path

from solarbank_controller.config import Config
from solarbank_controller.simulation import low_light_probe, run_simulation

parser = argparse.ArgumentParser()
parser.add_argument("--output", default="results/validation")
args = parser.parse_args()
root = Path(args.output)
root.mkdir(parents=True, exist_ok=False)
cfg = Config()
cases = [
    ("binary_adaptive", cfg, {}),
    ("binary_nominal", replace(cfg, reserve_mode="nominal"), {}),
    ("continuous_adaptive", replace(cfg, strategy="continuous"), {}),
    ("binary_start70", cfg, {"initial_soc": 70}),
    ("idle0", cfg, {"idle_w": 0}),
    ("idle5", cfg, {"idle_w": 5}),
    ("early_switch", cfg, {"initial_soc": 70, "switch_seconds": 0}),
    ("abrupt_early_switch", cfg, {"initial_soc": 70, "switch_seconds": 0, "cloud_cliff": True}),
    ("zero_failure", cfg, {"zero_works": False}),
]
results = {}
for name, config, options in cases:
    report = run_simulation(root / name, config, days=7, **options)
    results[name] = report["metrics"]
    print(name, json.dumps(report["metrics"]))
(root / "comparison.json").write_text(json.dumps(results, indent=2))
(root / "low_light.json").write_text(json.dumps(low_light_probe(), indent=2))
