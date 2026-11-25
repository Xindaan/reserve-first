# reserve-first

**A conservative winter controller for a balcony battery: protect the reserve first, export second.**

reserve-first is a Python controller and simulator for an **Anker SOLIX E1600 (Gen 1, A17C0)
with an MI80 micro-inverter**. In winter, a small balcony system produces little, and a battery
that drains overnight is empty exactly when it is needed. The controller keeps the state of
charge inside a reserve band and only allows export when photovoltaic input is stable and every
safety condition holds.

> **Status: simulation and read-only pilot.** The cloud writer is technically locked. Neither
> `arm` nor `run --execute` changes anything on a real device; `--execute` only runs against
> the mock adapter. Nothing in this repository makes the controller safe for unattended
> operation on real hardware.

## Quickstart

Requires Python 3.12–3.14 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --locked
.venv/bin/pytest -q
.venv/bin/solarbank --adapter mock status
.venv/bin/solarbank --adapter mock run --dry-run --cycles 1
.venv/bin/solarbank simulate --output results/my-winter-run
```

`simulate` runs fully offline and writes a CSV time series and a JSON report. The target
directory must not exist yet, so earlier comparisons are never overwritten.

With the optional cloud SDK ([anker-solix-api](https://github.com/thomluther/anker-solix-api),
pinned to a commit) the API contract tests run as well:

```bash
uv sync --locked --extra cloud
```

Without it, those tests are skipped explicitly.

## Usage

Global options come **before** the subcommand. The default adapter is `cloud`; the default
mode of `run` is read-only.

| Command | Effect |
|---|---|
| `discover` | Read-only lookup of sites and devices; result goes to a private file |
| `status` | Raw readings, data age, active setpoint, model and findings |
| `run --dry-run` | Read real data, log the decisions the controller *would* take |
| `arm` | Back up the original schedule and produce a before/after JSON; activates nothing |
| `run --execute` | Cloud: aborts before connecting. Mock: simulated transactions |
| `stop` | Persistent local stop flag, **not** a physical shutdown |
| `restore BACKUP` | Verify target identity and checksum, show the restore plan |
| `restore BACKUP --execute --confirm SHA256` | Mock only, with read-back and journal |
| `simulate` | Offline simulation, CSV time series, JSON report |
| `analyze DB` | Read-only analysis of a telemetry SQLite file |

For real reads, provide credentials as environment variables (see `.env.example`; the file is
deliberately not loaded automatically): `ANKER_EMAIL`, `ANKER_PASSWORD`, `ANKER_COUNTRY`, then
`ANKER_SITE_ID` and `ANKER_DEVICE_SN` for exactly one target system. All state (SQLite, JSONL,
schedule backups) is written below `--data-dir` (default `private/`) with restrictive file
permissions. A stop stays latched; use a new `--data-dir` for a new read-only run. A second
process on the same data directory is blocked by an OS lock.

`scripts/run_scenarios.py --output <dir>` reproduces the scenario comparison
(binary/continuous strategy, nominal/adaptive reserve, idle draw, early switching, a failing
zero-output command).

## Configuration

`--config config.example.yaml` loads validated defaults:

- 5-minute cycle; PV must be stable for 15 minutes; enable export above 60 W, disable below 40 W.
- At most 6 write attempts per rolling hour and 48 per 24 hours, with one slot each reserved for
  protective actions. Protection may bypass the dwell time, never the write budget.
- `strategy: binary` switches between no export and 100 W. `reserve_mode: nominal` uses a
  **45/55 %** band: below 45 % reserve charging wins; new export is only considered above 55 %
  and only if all other conditions hold. This is neither forced discharge back to 55 % nor a
  guaranteed floor.
- `reserve_mode: adaptive` starts at 60/65 % and, in simulation, moves to 65/70 % after at least
  two weak days.
- `latitude`/`longitude` drive the sunrise/sunset window; the example uses a placeholder.
- `timestamp_verified: false` keeps the read loop in `FAULT_SAFE` until the device clock has
  been checked against local time.
- `timestamp_timezone` must currently be `Europe/Berlin`; the config validator rejects any
  other zone, because device timestamps were only checked for that zone.

These thresholds are **controller assumptions, not measured hardware limits.**

## Limitations

- A hard upper SOC limit of 55 % could not be established with the Gen 1 control surface;
  a 100 W setpoint can still lead to battery discharge.
- How the MI80 behaves on loss of internet/Wi-Fi and on restart has not been measured.
- Cloud SOC and battery power are not independent measurements. Missing battery-channel values
  are reported as estimates; contradictory zero fields are detected via the power balance.
- Missing temperature data never enables export optimisation. This REST pilot does not read
  MQTT temperature values.
- Pure PV pass-through is not proof of discharge; battery stop and full MI80 zero output are
  checked separately.

Not included here: one-off hardware trial scripts, captured device data and the companion
home app. They are tied to one specific installation.

## Troubleshooting

- **Battery fills beyond 50 % despite charge priority:** that setting is a priority, not an
  upper cut-off.
- **Setpoint 100 W, real output 20–80 W:** plausible; neither setpoint nor the daily discharge
  counter proves battery discharge.
- **`FAULT_SAFE`:** data, identity or behaviour is uncertain, so no optimising action is written.
  It says nothing about physical safety.
- **429 / network errors:** persistent backoff of 5, 10, 20, 40, at most 60 minutes.
- **Uncertain write response:** the mock journal stays locked across restarts; there is no
  automatic retry.

## Development

```bash
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/pytest -q
```

Tests never connect to Anker. The SDK contract test runs the real schedule transformation in
the SDK's test mode and forbids network calls. Property-based tests (Hypothesis) check the
safety invariants.

## Architecture

Modules in `src/solarbank_controller/`: `api_adapter` (cloud and mock), `telemetry`,
`controller`, `schedule`, `safety`, `storage`, `simulation`, `analysis`, `observation`,
`history`, `collector`, `cli`. Code comments and log messages are in German.

## License

MIT, see [LICENSE](LICENSE). Not affiliated with or endorsed by Anker.
