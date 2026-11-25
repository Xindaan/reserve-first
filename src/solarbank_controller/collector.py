"""Dauerhafte Anker-Infomessung, strikt lesend und ohne Regler/Executor."""

import asyncio
import fcntl
import json
import logging
import math
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .api_adapter import CloudAdapter, WriteBlocked
from .config import Config
from .models import Result
from .observation import status_write
from .storage import Store, private_dir, private_json


class AnmeldungNoetig(RuntimeError):
    pass


@contextmanager
def prozesssperre(directory):
    private_dir(directory)
    fd = os.open(directory / "collector.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def cache_pruefen(path, jetzt=None):
    if path.is_symlink() or not path.is_file():
        raise AnmeldungNoetig()
    try:
        auth = json.loads(path.read_text())
        ende = float(auth.get("token_expires_at", 0))
        if not math.isfinite(ende) or ende <= (jetzt or datetime.now(UTC)).timestamp() + 120:
            raise AnmeldungNoetig()
        return ende
    except (ValueError, TypeError, OSError):
        raise AnmeldungNoetig() from None


def lesesitzung(directory):
    from anker_solix_api.apitypes import API_ENDPOINTS
    from anker_solix_api.session import AnkerSolixClientSession

    namen = {
        API_ENDPOINTS[k]: k
        for k in ("site_list", "scene_info", "bind_devices", "get_device_load", "solar_info")
    }

    class NurLesen(AnkerSolixClientSession):
        async def async_authenticate(self, restart=False):
            if restart:
                raise AnmeldungNoetig()
            cache_pruefen(Path(self._authFile))
            return await super().async_authenticate(restart=False)

        async def request(self, method, endpoint, **kwargs):
            if method.lower() != "post" or endpoint not in namen:
                raise WriteBlocked("Collector erlaubt nur fuenf Leseendpunkte")
            response = await super().request(method, endpoint, **kwargs)
            # Original-Szenenantwort fuer MI80-Verlauf erhalten; andere Felder
            # speichert Store normalisiert. Keine staendig duplizierten Authdateien.
            if namen[endpoint] == "scene_info":
                at = datetime.now(UTC)
                private_json(
                    directory / f"raw-{at.strftime('%Y%m%dT%H%M%S%f')}-scene_info.json",
                    {"received_at": at.isoformat(), "response": response},
                )
            return response

    return NurLesen


async def sammeln(adapter, directory, auth_path, *, intervall=300, jetzt=None, warten=None):
    """Kein Zeitlimit. Injizierte Uhr/Wartefunktion erlauben echte Ablaufregressionen."""
    if intervall < 300:
        raise ValueError("Mindestens 300 Sekunden zwischen Abfragen")
    jetzt = jetzt or (lambda: datetime.now(UTC))
    warten = warten or asyncio.sleep
    store = Store(directory)
    samples, errors = store.db.execute(
        "SELECT coalesce(sum(json_extract(payload,'$.communication_error')=0),0), "
        "coalesce(sum(json_extract(payload,'$.communication_error')=1),0) FROM cycles"
    ).fetchone()
    row = store.db.execute("SELECT at FROM cycles ORDER BY rowid DESC LIMIT 1").fetchone()
    letzter_versuch = datetime.fromisoformat(row[0]) if row else None
    erfolg = store.db.execute(
        "SELECT at FROM cycles WHERE json_extract(payload,'$.communication_error')=0 "
        "ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    status = dict(
        state="starting",
        pid=os.getpid(),
        samples=samples,
        errors=errors,
        last_sample=erfolg[0] if erfolg else None,
        settings_write_calls=0,
        mode="continuous-read-only",
        interval_s=intervall,
    )
    failures = 0

    def melden(state):
        status.update(state=state, updated_at=jetzt().isoformat())
        status_write(directory, status)

    try:
        melden("starting")
        # Neustarts duerfen den Mindestabstand nicht umgehen.
        if letzter_versuch:
            rest = max(0, intervall - (jetzt() - letzter_versuch).total_seconds())
            for _ in range(int(rest + 0.999)):
                if (directory / "STOP").exists():
                    break
                await warten(1)
        while not (directory / "STOP").exists():
            try:
                status["token_expires_at"] = cache_pruefen(auth_path, jetzt())
                sample = await asyncio.wait_for(adapter.read(), timeout=90)
                store.cycle(sample, None, Result("info", "Dauerhafte Infomessung; nur Lesen"))
                status["samples"] += 1
                status["last_sample"] = sample.observed_at.isoformat()
                status.pop("error_type", None)
                failures = 0
                melden("running")
            except Exception as exc:
                failures += 1
                status["errors"] += 1
                status["error_type"] = type(exc).__name__
                store.cycle(
                    None,
                    None,
                    Result("communication-error", type(exc).__name__),
                    communication_error=True,
                    now=jetzt(),
                )
                melden(
                    "authentication-required" if isinstance(exc, AnmeldungNoetig) else "retrying"
                )
                if isinstance(exc, WriteBlocked):
                    melden("blocked")
                    return
            delay = min(3600, intervall * 2 ** min(failures, 4))
            for _ in range(delay):
                if (directory / "STOP").exists():
                    break
                await warten(1)
        melden("stopped")
    finally:
        store.close()


async def run(directory, source):
    from aiohttp import ClientSession, ClientTimeout
    from anker_solix_api.api import AnkerSolixApi

    os.umask(0o077)
    with prozesssperre(directory):
        target = json.loads((source / "target.json").read_text())
        log = logging.getLogger("solarbank.collector.private")
        log.handlers = [logging.NullHandler()]
        log.propagate = False
        log.disabled = True
        async with ClientSession(timeout=ClientTimeout(total=30)) as web:
            session = lesesitzung(directory)("cache-only@local.invalid", "UNUSED", "DE", web, log)
            session._authFile = str(source / "auth.json")
            session.requestTimeout(10)
            adapter = CloudAdapter(
                AnkerSolixApi(apisession=session, logger=log),
                Config(),
                target["site_id"],
                target["device_id"],
            )
            await sammeln(adapter, directory, source / "auth.json")


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(run(args.output, args.source))
    except Exception as exc:
        print(json.dumps({"state": "failed", "error_type": type(exc).__name__}), flush=True)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
