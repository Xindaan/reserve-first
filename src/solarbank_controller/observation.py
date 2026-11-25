"""Begrenzte reine Beobachtung: keine Executor-, Setter- oder Loginpfade."""

import asyncio
import json
import logging
import os
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .api_adapter import CloudAdapter, WriteBlocked
from .config import Config
from .controller import Controller
from .models import Result
from .schedule import fingerprint, is_day
from .storage import Store, private_json


def status_write(directory, status):
    temp = directory / "status.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(status, out, indent=2, default=str)
        out.flush()
        os.fsync(out.fileno())
    temp.replace(directory / "status.json")


class Coverage:
    def __init__(self, started, hours):
        self.started = started
        self.deadline = started + timedelta(hours=hours)
        self.first = self.last = None
        self.day = self.night = self.samples = self.errors = 0
        self.gap_s = 0
        self.previous_hash = None
        self.plan_changes = 0

    def sample(self, sample, cfg):
        if self.last:
            self.gap_s = max(self.gap_s, (sample.observed_at - self.last).total_seconds())
        self.first = self.first or sample.observed_at
        # 24 Stunden ab erstem erfolgreichen Read, nicht ab Prozessstart.
        if not self.samples:
            self.deadline = self.first + (self.deadline - self.started)
        self.last = sample.observed_at
        self.samples += 1
        self.day += int(is_day(sample.observed_at, cfg))
        self.night += int(not is_day(sample.observed_at, cfg))
        digest = fingerprint(sample.schedule)
        self.plan_changes += int(self.previous_hash is not None and digest != self.previous_hash)
        self.previous_hash = digest

    def status(self, state, now):
        span = (self.last - self.first).total_seconds() if self.first and self.last else 0
        return dict(
            state=state,
            updated_at=now.isoformat(),
            started_at=self.started.isoformat(),
            deadline=self.deadline.isoformat(),
            first_sample=str(self.first),
            last_sample=str(self.last),
            samples=self.samples,
            errors=self.errors,
            observation_span_s=span,
            max_successful_read_gap_s=self.gap_s,
            day_window_samples=self.day,
            night_window_samples=self.night,
            schedule_changes=self.plan_changes,
            settings_write_calls=0,
            window_24h_with_day_and_night=bool(span >= 86400 and self.day and self.night),
            physical_verification=False,
            pid=os.getpid(),
        )


async def observe(directory: Path, source: Path, hours: float = 24):
    from aiohttp import ClientSession, ClientTimeout
    from anker_solix_api.api import AnkerSolixApi
    from anker_solix_api.apitypes import API_ENDPOINTS
    from anker_solix_api.session import AnkerSolixClientSession

    if not 24 <= hours <= 48:
        raise ValueError("Messdauer muss 24..48 Stunden betragen")
    os.umask(0o077)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    cfg = Config()
    started = datetime.now(UTC)
    coverage = Coverage(started, hours)
    status_write(directory, coverage.status("starting", started))
    auth = json.loads((source / "auth.json").read_text())
    if (
        float(auth.get("token_expires_at", 0))
        < (coverage.deadline + timedelta(hours=1)).timestamp()
    ):
        raise ValueError("Token reicht nicht fuer das Messfenster")
    private_json(directory / "auth.json", auth)
    target = json.loads((source / "target.json").read_text())
    private_json(directory / "target.json", target)
    names = {
        API_ENDPOINTS[k]: k
        for k in ("site_list", "scene_info", "bind_devices", "get_device_load", "solar_info")
    }
    log = logging.getLogger("solarbank.observation.private")
    log.handlers = [logging.NullHandler()]
    log.propagate = False
    log.disabled = True

    class ReadOnlyCache(AnkerSolixClientSession):
        async def async_authenticate(self, restart=False):
            if restart or not Path(self._authFile).is_file():
                raise WriteBlocked("Neue Anmeldung im Recorder gesperrt")
            return await super().async_authenticate(restart=False)

        async def request(self, method, endpoint, **kwargs):
            if endpoint not in names:
                raise WriteBlocked("Recorder erlaubt nur fuenf Leseendpunkte")
            response = await super().request(method, endpoint, **kwargs)
            at = datetime.now(UTC)
            private_json(
                directory / f"raw-{at.strftime('%Y%m%dT%H%M%S%f')}-{names[endpoint]}.json",
                {"received_at": at.isoformat(), "response": response},
            )
            return response

    store = Store(directory)
    controller = Controller(cfg)
    hard_end = started + timedelta(hours=hours + 1)
    failures = 0
    try:
        async with ClientSession(timeout=ClientTimeout(total=30)) as web:
            session = ReadOnlyCache("cache-only@local.invalid", "UNUSED", "DE", web, log)
            session._authFile = str(directory / "auth.json")
            session.requestTimeout(10)
            api = AnkerSolixApi(apisession=session, logger=log)
            adapter = CloudAdapter(api, cfg, target["site_id"], target["device_id"])
            while datetime.now(UTC) < hard_end and not (directory / "STOP").exists():
                try:
                    sample = await asyncio.wait_for(adapter.read(), timeout=90)
                    coverage.sample(sample, cfg)
                    private_json(
                        directory / f"snapshot-{coverage.samples:04d}.json", asdict(sample)
                    )
                    if coverage.samples == 1:
                        store.backup(sample)
                    store.cycle(sample, controller.decide(sample), Result("observe", "Nur Lesen"))
                    failures = 0
                    status = coverage.status("running", datetime.now(UTC))
                    status["latest"] = {
                        k: getattr(sample, k)
                        for k in (
                            "soc",
                            "pv_w",
                            "battery_w",
                            "battery_quality",
                            "output_dc_w",
                            "device_at",
                            "mode",
                            "firmware",
                        )
                    }
                    status_write(directory, status)
                    if sample.observed_at >= coverage.deadline:
                        break
                except Exception as exc:
                    coverage.errors += 1
                    failures += 1
                    now = datetime.now(UTC)
                    store.cycle(
                        None,
                        None,
                        Result("communication-error", type(exc).__name__),
                        communication_error=True,
                        now=now,
                    )
                    status = coverage.status("retrying", now)
                    status["error_type"] = type(exc).__name__
                    status_write(directory, status)
                    if isinstance(exc, WriteBlocked):
                        break
                delay = min(3600, cfg.cycle_s * 2 ** min(failures, 4))
                # Stoppsignal jede Sekunde pruefen; API-Abstand bleibt mindestens 5 Minuten.
                for _ in range(delay):
                    if (directory / "STOP").exists():
                        break
                    await asyncio.sleep(1)
        final = coverage.status("finished", datetime.now(UTC))
        if not final["window_24h_with_day_and_night"]:
            final["state"] = "incomplete"
        status_write(directory, final)
    finally:
        store.close()


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(observe(args.output, args.source))
    except Exception as exc:
        # Keine API-Ausnahmetexte oder Identitaeten ausgeben.
        print(json.dumps({"state": "failed", "error_type": type(exc).__name__}), flush=True)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
