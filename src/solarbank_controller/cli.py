"""Bewusst sichere CLI: arm/stop schreiben niemals auf Geraete."""

import argparse
import asyncio
import fcntl
import json
import os
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from .analysis import analyze
from .api_adapter import MockAdapter, cloud_connection
from .config import Config
from .controller import Controller
from .models import Result, Sample
from .safety import Backoff, Executor
from .schedule import fingerprint, mock_schedule, safe_baseline, validate_schedule
from .simulation import low_light_probe, simulate
from .storage import Store, private_json
from .telemetry import issues


def emit(value):
    print(json.dumps(value, default=str, indent=2, allow_nan=False))


def mock_sample():
    now = datetime.now(UTC)
    return Sample(
        now,
        now,
        50,
        0,
        0,
        0,
        temperature_c=10,
        battery_quality="simulated",
        mode="standby",
        firmware="SIMULATED",
        owner=True,
        discharge_priority_known=True,
        schedule=mock_schedule(),
    )


@asynccontextmanager
async def connect(args, cfg, directory):
    if args.adapter == "mock":
        yield MockAdapter(mock_sample())
    else:
        async with cloud_connection(cfg, directory) as adapter:
            yield adapter


async def restore(args, adapter, store, cfg):
    backup = json.loads(Path(args.backup).read_text())
    sample = await adapter.read()
    plan = backup["schedule"]
    validate_schedule(plan)
    if (
        backup["sha256"] != fingerprint(plan)
        or backup["device_id"] != sample.device_id
        or backup["site_id"] != sample.site_id
        or backup["model"] != sample.model
    ):
        raise ValueError("Backup-Pruefsumme oder Zielidentitaet passt nicht")
    if not args.execute:
        emit(
            {
                "action": "restore-preview",
                "backup_hash": backup["sha256"],
                "current_hash": fingerprint(sample.schedule),
                "live_write": False,
            }
        )
        return 0
    if adapter.live or args.confirm != backup["sha256"]:
        emit({"blocked": "Live-Restore gesperrt; Mock-Restore braucht --confirm BACKUP_SHA256"})
        return 2
    if issues(sample, cfg) or not sample.owner:
        raise ValueError("Restore braucht gueltige Daten und Rechte")
    validate_schedule(plan, winter=True)
    store.backup(sample)
    store.put("inflight", {"restore": backup["sha256"]})
    store.write_attempt(sample.observed_at, "restore")
    await adapter.write(plan, fingerprint(sample.schedule))
    after = await adapter.read()
    if fingerprint(after.schedule) != backup["sha256"]:
        raise ValueError("Restore nicht bestaetigt; Journal bleibt gesperrt")
    store.put("inflight", None)
    store.put("expected", backup["sha256"])
    emit({"restored": "Mock-Zeitplan bestaetigt", "live_write": False})
    return 0


async def operate(args):
    cfg = Config.load(args.config)
    if args.command == "simulate":
        cfg = replace(
            cfg,
            strategy=args.strategy or cfg.strategy,
            reserve_mode=args.reserve or cfg.reserve_mode,
        )
        report = await simulate(
            Path(args.output),
            cfg,
            days=args.days,
            seed=args.seed,
            initial_soc=args.soc,
            idle_w=args.idle_w,
            switch_seconds=args.switch_seconds,
            zero_works=not args.zero_failure,
        )
        (Path(args.output) / "low_light.json").write_text(json.dumps(low_light_probe(), indent=2))
        emit(report)
        return 0
    if args.command == "analyze":
        emit(analyze(Path(args.database)))
        return 0
    directory = Path(args.data_dir) / args.adapter
    store = Store(directory)
    try:
        if args.command == "stop":
            store.put("stopped", True)
            emit(
                {
                    "stopped": "Lokaler Reglerstop angefordert",
                    "device_write": False,
                    "warning": "Bestehender Geraetezeitplan bleibt wirksam. Keine physische Abschaltung.",
                }
            )
            return 0
        if args.command in {"run", "restore"} and args.execute and args.adapter == "cloud":
            emit(
                {
                    "blocked": "Live-Schreibpfad gesperrt: beaufsichtigter MI80-Test steht aus",
                    "cloud_contact": False,
                }
            )
            return 2
        lock_path = directory / "runner.lock"
        if lock_path.is_symlink():
            raise ValueError("Lock-Symlink abgewiesen")
        with lock_path.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            async with connect(args, cfg, directory) as adapter:
                if args.command == "discover":
                    devices = (
                        await adapter.discover()
                        if adapter.live
                        else [
                            {"device_id": "mock-device", "site_id": "mock-site", "model": "A17C0"}
                        ]
                    )
                    file = (
                        directory
                        / f"discovery-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}.json"
                    )
                    private_json(file, {"devices": devices})
                    emit(
                        {
                            "devices": [
                                {k: v for k, v in d.items() if k not in {"device_id", "site_id"}}
                                for d in devices
                            ],
                            "private_details": str(file),
                        }
                    )
                    return 0
                if args.command == "restore":
                    return await restore(args, adapter, store, cfg)
                if args.command in {"status", "arm"}:
                    s = await adapter.read()
                    if args.command == "status":
                        status_file = (
                            directory
                            / f"status-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}.json"
                        )
                        private_json(status_file, asdict(s))
                        emit(
                            {
                                "soc": s.soc,
                                "pv_w": s.pv_w,
                                "battery_w": s.battery_w,
                                "battery_quality": s.battery_quality,
                                "output_dc_w": s.output_dc_w,
                                "household_ac_w": s.household_ac_w,
                                "active": asdict(s.active) if s.active else None,
                                "model": s.model,
                                "inverter": s.inverter,
                                "firmware": s.firmware,
                                "device_at": s.device_at,
                                "issues": issues(s, cfg),
                                "live_writes_enabled": False,
                                "private_snapshot": str(status_file),
                            }
                        )
                        return 0
                    backup = store.backup(s)
                    blockers = issues(s, cfg) + [
                        "MI80-Ausfall-/Neustartnachweis fehlt",
                        "Keine reale Schreibfreigabe in dieser Version",
                    ]
                    plan = None
                    try:
                        plan = safe_baseline(s.schedule)
                    except (ValueError, KeyError, TypeError):
                        blockers.append("Bestandszeitplan nicht unterstuetzt")
                    path = (
                        directory / f"proposal-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}.json"
                    )
                    private_json(
                        path,
                        {
                            "before": s.schedule,
                            "after": plan,
                            "backup": backup,
                            "device_id": s.device_id,
                            "site_id": s.site_id,
                            "blockers": blockers,
                            "armed": False,
                        },
                    )
                    emit(
                        {
                            "armed": False,
                            "proposal": str(path),
                            "backup": backup,
                            "blockers": blockers,
                        }
                    )
                    return 0
                if store.get("stopped"):
                    emit(
                        {
                            "blocked": "Stopp verriegelt; neuer Datenordner fuer neuen lesenden Pilotlauf"
                        }
                    )
                    return 2
                controller = Controller(cfg)
                controller.discharge_latched = store.get("discharge_latched", False)
                executor = Executor(adapter, store, cfg, execute=args.execute)
                backoff = Backoff(store)
                attempts = 0
                last_failed = False
                while not store.get("stopped"):
                    now = datetime.now(UTC)
                    if backoff.ready(now):
                        attempts += 1
                        try:
                            if not adapter.live:
                                adapter.sample = replace(
                                    adapter.sample, observed_at=now, device_at=now
                                )
                            s = await adapter.read()
                            last_failed = False
                            backoff.success()
                            behavior = executor.confirm_behavior(s)
                            decision = controller.decide(s)
                            store.put("discharge_latched", controller.discharge_latched)
                            result = await executor.apply(s, decision)
                            store.cycle(s, decision, result)
                            emit(
                                {
                                    "state": decision.state,
                                    "reason": decision.reason,
                                    "alarms": decision.alarms,
                                    "action": result.action,
                                    "detail": result.detail,
                                    "behavior": behavior,
                                }
                            )
                        except Exception as exc:
                            last_failed = True
                            delay = backoff.failed(now)
                            # Niemals rohe Cloud-Fehlermeldungen (Tokens/URLs/IDs) protokollieren.
                            result = Result("communication-error", type(exc).__name__)
                            store.cycle(None, None, result, communication_error=True, now=now)
                            emit(
                                {
                                    "state": "FAULT_SAFE",
                                    "error_type": type(exc).__name__,
                                    "retry_after_s": delay,
                                    "alarm": "Nachtabschaltung nicht garantiert",
                                }
                            )
                        if args.cycles and attempts >= args.cycles:
                            break
                    # Stopp spaetestens nach einer Sekunde erkennen; Pollfrequenz bleibt 5 min.
                    deadline = asyncio.get_running_loop().time() + cfg.cycle_s
                    while asyncio.get_running_loop().time() < deadline and not store.get("stopped"):
                        await asyncio.sleep(1)
                return 2 if last_failed else 0
    finally:
        store.close()


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--data-dir", default="private")
    parser.add_argument("--adapter", choices=("cloud", "mock"), default="cloud")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("discover", "status", "arm", "stop"):
        sub.add_parser(name)
    run = sub.add_parser("run")
    group = run.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--execute", action="store_true")
    run.add_argument("--cycles", type=int, default=0, help="0 = dauerhaft")
    restore_parser = sub.add_parser("restore")
    restore_parser.add_argument("backup")
    restore_parser.add_argument("--execute", action="store_true")
    restore_parser.add_argument("--confirm")
    sim = sub.add_parser("simulate")
    sim.add_argument("--output", default="results/winter")
    sim.add_argument("--days", type=int, default=7)
    sim.add_argument("--seed", type=int, default=1600)
    sim.add_argument("--soc", type=float, default=50)
    sim.add_argument("--idle-w", type=float, default=2)
    sim.add_argument("--switch-seconds", type=int, choices=(0, 600), default=600)
    sim.add_argument("--zero-failure", action="store_true")
    sim.add_argument("--strategy", choices=("binary", "continuous"))
    sim.add_argument("--reserve", choices=("nominal", "adaptive"))
    analysis = sub.add_parser("analyze")
    analysis.add_argument("database")
    args = parser.parse_args(argv)
    if args.command == "simulate" and not (
        1 <= args.days <= 31 and 0 <= args.soc <= 100 and 0 <= args.idle_w <= 20
    ):
        parser.error("Simulation: 1..31 Tage, SOC 0..100, Eigenverbrauch 0..20 W")
    if args.command == "run" and args.cycles < 0:
        parser.error("cycles darf nicht negativ sein")
    try:
        returncode = asyncio.run(operate(args))
    except (ValueError, FileNotFoundError, KeyError, TypeError, BlockingIOError) as exc:
        emit(
            {
                "error_type": type(exc).__name__,
                "message": "Lokale Voraussetzungen/Dateien pruefen; keine Geraeteaenderung bestaetigt",
            }
        )
        returncode = 2
    except KeyboardInterrupt:
        emit({"stopped": "Prozess beendet; keine Geraeteabschaltung ausgefuehrt"})
        returncode = 130
    except Exception as exc:
        emit(
            {
                "error_type": type(exc).__name__,
                "message": "Abgebrochen; Nachtabschaltung nicht garantiert",
            }
        )
        returncode = 2
    raise SystemExit(returncode)


if __name__ == "__main__":
    main()
