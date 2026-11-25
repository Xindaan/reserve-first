"""Begrenzter Anker-Historienimport neben dem unveraenderten Live-Collector.

Eigene Datenbank, vorhandener Token, zwei eng begrenzte Leseendpunkte.
A17C0-Tagesleistung in W trotz falschem wh-Label; kein SOC aus Null-Platzhaltern.
"""

import asyncio
import json
import logging
import math
import os
import re
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .api_adapter import WriteBlocked
from .collector import AnmeldungNoetig, cache_pruefen, prozesssperre


def private_json(path, payload):
    """Atomarer, privater Ersatz fuer mutable Historien-/Statusdateien."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
        name = out.name
        try:
            json.dump(payload, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        except BaseException:
            os.unlink(name)
            raise
    os.replace(name, path)


ZONE = ZoneInfo("Europe/Berlin")
ARTEN = {
    "solar_production": "solar_history_w",
    "solarbank": "solarbank_output_history_w",
    "inverter": "mi80_history_w",
}
EINHEITEN = {"solar_production": "W", "solarbank": "W", "inverter": "W"}


def punkte(raw, tag, art, jetzt):
    """Originalpunkte; Zukunft, reparierte MI80-Punkte und mehrdeutige Zeit auslassen."""
    if not isinstance(raw, dict):
        raise ValueError("Historienformat nicht bestaetigt")
    if art == "inverter":
        if raw.get("energyUnit") != "kWh":
            raise ValueError("Wechselrichterschema nicht bestaetigt")
        values = raw.get("curve")
    else:
        if str(raw.get("power_unit", "")).lower() != "wh":
            raise ValueError("Historieneinheit nicht bestaetigt")
        # A17C0, rangeType=day: Stuetzpunkte sind Leistung in W trotz wh-Label.
        # 13 Tagesbilanzen + bekannte ~100-W-Abgabe stuetzen diese Interpretation.
        # Nicht auf week/month/year oder charge_trend/SOC uebertragen.
        values = raw.get("power")
    if not isinstance(values, list) or not values:
        raise ValueError("Keine Historie geliefert")
    result = {}
    for item in values:
        if not isinstance(item, dict):
            continue
        if art == "inverter" and (
            item.get("isFuture") is not False or item.get("isFix") is not False
        ):
            continue
        text = item.get("time")
        if not isinstance(text, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d|24:00", text):
            continue
        # Tagesende ist ein zusaetzlicher Null-Platzhalter in den echten Antworten.
        # Nicht mit dem 00:00-Punkt des Folgetags doppelt importieren.
        if text == "24:00":
            continue
        hour, minute = map(int, text.split(":"))
        naive = datetime.combine(tag, datetime.min.time()) + timedelta(hours=hour, minutes=minute)
        local = naive.replace(tzinfo=ZONE)
        if local.utcoffset() != local.replace(fold=1).utcoffset():
            continue
        stamp = local.astimezone(UTC).timestamp()
        # Laufende/noch nicht abgeschlossene Intervalle nicht als Null auffuellen.
        if stamp + (300 if art == "inverter" else 1200) > jetzt:
            continue
        value = item.get("power" if art == "inverter" else "value")
        if value is None or isinstance(value, bool):
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value >= 0:
            result[text] = (stamp, value)
    return result


class Historie:
    def __init__(self, basis):
        self.basis = basis
        basis.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(basis / "history.sqlite")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS points(day TEXT,kind TEXT,label TEXT,time REAL,value REAL,received REAL,PRIMARY KEY(day,kind,label))"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS jobs(day TEXT,kind TEXT,due REAL,ok REAL,error TEXT,PRIMARY KEY(day,kind))"
        )
        self.db.commit()

    async def nachholen(self, lesen, jetzt, limit=4):
        heute = datetime.fromtimestamp(jetzt, ZONE).date()
        # Rollierender 30-Tage-Bereich. Alte Punkte bleiben dauerhaft gespeichert.
        with self.db:
            for offset in range(30):
                tag = (heute - timedelta(days=offset)).isoformat()
                for art in ARTEN:
                    self.db.execute(
                        "INSERT OR IGNORE INTO jobs VALUES(?,?,0,NULL,NULL)", (tag, art)
                    )
        jobs = self.db.execute(
            "SELECT day,kind FROM jobs WHERE due<=? AND day>=? ORDER BY day DESC,kind LIMIT ?",
            (jetzt, (heute - timedelta(days=29)).isoformat(), limit),
        ).fetchall()
        erfolge = 0
        for tag, art in jobs:
            try:
                raw = await lesen(tag, art)
                points = punkte(raw, datetime.fromisoformat(tag).date(), art, jetzt)
                if not points:
                    raise ValueError("Keine abgeschlossenen Historienpunkte")
                # Originalantwort privat fuer spaetere Einheiten-/Formatpruefung.
                private_json(self.basis / f"raw-{tag}-{art}.json", raw)
                alter = (heute - datetime.fromisoformat(tag).date()).days
                frist = 900 if alter == 0 else 3600 if alter == 1 else 7 * 86400
                with self.db:
                    for label, (stamp, value) in points.items():
                        self.db.execute(
                            "INSERT INTO points VALUES(?,?,?,?,?,?) ON CONFLICT(day,kind,label) DO UPDATE SET time=excluded.time,value=excluded.value,received=excluded.received",
                            (tag, art, label, stamp, value, jetzt),
                        )
                    self.db.execute(
                        "UPDATE jobs SET due=?,ok=?,error=NULL WHERE day=? AND kind=?",
                        (jetzt + frist, jetzt, tag, art),
                    )
                erfolge += 1
            except Exception as exc:
                with self.db:
                    self.db.execute(
                        "UPDATE jobs SET due=?,error=? WHERE day=? AND kind=?",
                        (jetzt + 1800, type(exc).__name__, tag, art),
                    )
                # Kein weiterer Netzverkehr bei gemeinsamem Transport-/Authfehler.
                if not isinstance(exc, ValueError):
                    with self.db:
                        self.db.execute(
                            "UPDATE jobs SET due=MAX(due,?) WHERE due<=?", (jetzt + 1800, jetzt)
                        )
                    break
        self.exportieren(jetzt)
        private_json(
            self.basis / "status.json",
            {
                "updated_at": jetzt,
                "successful_jobs": erfolge,
                "points": self.db.execute("SELECT count(*) FROM points").fetchone()[0],
                "pending_jobs": self.db.execute(
                    "SELECT count(*) FROM jobs WHERE ok IS NULL"
                ).fetchone()[0],
                "settings_write_calls": 0,
            },
        )
        return erfolge

    def exportieren(self, jetzt=None):
        jetzt = jetzt or datetime.now(UTC).timestamp()
        heute = datetime.fromtimestamp(jetzt, ZONE).date()
        unvollstaendig = set()
        tageswerte = []
        for path in sorted(self.basis.glob("raw-*-solarbank.json")):
            tag = path.name[4:14]
            datum = datetime.fromisoformat(tag).date()
            raw = json.loads(path.read_text())
            row = self.db.execute(
                "SELECT ok FROM jobs WHERE day=? AND kind='solarbank'", (tag,)
            ).fetchone()
            if not row or row[0] is None:
                continue

            def nummer(key, raw=raw):
                try:
                    n = float(raw[key])
                    return n if math.isfinite(n) and n >= 0 else None
                except (KeyError, TypeError, ValueError):
                    return None

            power = raw.get("power") or []

            # Gemeldete Tagesabgabe >0 bei durchgehend nuller Kurve ist keine
            # verlaessliche Nullmessung. Solche Tage bleiben in der Kurve offen.
            def positive_leistung(item):
                try:
                    return math.isfinite(float(item["value"])) and float(item["value"]) > 0
                except (KeyError, TypeError, ValueError):
                    return False

            if (nummer("discharge_total") or 0) > 0 and not any(
                positive_leistung(x) for x in power
            ):
                unvollstaendig.add(tag)
            tagesende = (
                datetime.combine(datum + timedelta(days=1), datetime.min.time())
                .replace(tzinfo=ZONE)
                .timestamp()
            )
            if datum >= heute or row[0] < tagesende:
                continue  # Nur nach Tagesende abgerufene Bilanzen, keine alten Teilstaende.
            stamp = (
                datetime.combine(datum, datetime.min.time())
                .replace(hour=12, tzinfo=ZONE)
                .timestamp()
            )
            for feld, key, unit in [
                ("battery_charge_day_kwh", "charge_total", "charge_unit"),
                ("solarbank_output_day_kwh", "discharge_total", "discharge_unit"),
            ]:
                value = nummer(key)
                if str(raw.get(unit, "")).lower() != "kwh" or value is None:
                    continue
                tageswerte.append(
                    {
                        "key": f"{tag}/{feld}",
                        "field": feld,
                        "time": stamp,
                        "value": value,
                        "received": row[0],
                        "unit": "kWh",
                    }
                )
        rows = self.db.execute(
            "SELECT day,kind,label,time,value,received FROM points ORDER BY time,kind"
        )
        points = [
            {
                "key": f"{day}/{kind}/{label}",
                "field": ARTEN[kind],
                "time": stamp,
                "value": value,
                "received": received,
                "unit": EINHEITEN[kind],
            }
            for day, kind, label, stamp, value, received in rows
            if not (kind == "solarbank" and day in unvollstaendig)
        ]
        private_json(
            self.basis / "history.json",
            {
                "version": 1,
                "normalization": "a17c0-day-power-w-v1",
                "incomplete_solarbank_days": sorted(unvollstaendig),
                "points": points + tageswerte,
            },
        )


def lesesitzung():
    from anker_solix_api.apitypes import API_ENDPOINTS
    from anker_solix_api.session import AnkerSolixClientSession

    class NurHistorie(AnkerSolixClientSession):
        async def async_authenticate(self, restart=False):
            if restart:
                raise AnmeldungNoetig()
            cache_pruefen(Path(self._authFile))
            return await super().async_authenticate(restart=False)

        async def request(self, method, endpoint, **kwargs):
            if method.lower() != "post" or endpoint not in {
                API_ENDPOINTS["energy_analysis"],
                API_ENDPOINTS["get_device_pv_statistics"],
            }:
                raise WriteBlocked("Nur energy_analysis und get_device_pv_statistics erlaubt")
            return await super().request(method, endpoint, **kwargs)

    return NurHistorie


async def run(source, output):
    from aiohttp import ClientSession, ClientTimeout
    from anker_solix_api.api import AnkerSolixApi

    os.umask(0o077)
    with prozesssperre(output):
        cache_pruefen(source / "auth.json")
        target = json.loads((source / "target.json").read_text())
        log = logging.getLogger("anker.history.private")
        log.disabled = True
        history = Historie(output)
        try:
            async with ClientSession(timeout=ClientTimeout(total=45)) as web:
                session = lesesitzung()("cache-only@local.invalid", "UNUSED", "DE", web, log)
                session._authFile = str(source / "auth.json")
                session.requestTimeout(10)
                api = AnkerSolixApi(apisession=session, logger=log)
                # Identitaet ausschliesslich aus bestehender Live-Aufzeichnung.
                # Kein weiterer Inventarabruf, kein Eingriff in den Recorder.
                scene_files = sorted((output.parent / "anker-info").glob("raw-*-scene_info.json"))
                inverter = []
                if scene_files:
                    scene = json.loads(scene_files[-1].read_text())
                    inverter = [
                        d
                        for d in scene.get("response", {}).get("data", {}).get("solar_list", [])
                        if d.get("device_pn") == "A5143"
                    ]

                async def lesen(tag, art):
                    if art == "inverter":
                        if len(inverter) != 1 or not inverter[0].get("device_sn"):
                            raise ValueError("Wechselrichter nicht eindeutig identifiziert")
                        return await api.get_device_pv_statistics(
                            deviceSn=inverter[0]["device_sn"],
                            rangeType="day",
                            startDay=datetime.fromisoformat(tag).replace(tzinfo=ZONE),
                        )
                    return await api.energy_analysis(
                        siteId=target["site_id"],
                        deviceSn=target["device_id"],
                        rangeType="day",
                        startDay=datetime.fromisoformat(tag).replace(tzinfo=ZONE),
                        devType=art,
                    )

                await history.nachholen(lesen, datetime.now(UTC).timestamp())
        finally:
            history.db.close()


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(run(args.source, args.output))
    except BlockingIOError:
        pass
    except Exception as exc:
        print(json.dumps({"state": "failed", "error_type": type(exc).__name__}), flush=True)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
