"""Gen-1-Zeitplaene: pruefen, kopieren und begrenzte Freigaben vorschlagen."""

import copy
import hashlib
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from astral import Observer
from astral.sun import sun

from .config import Config
from .models import Command

BERLIN = ZoneInfo("Europe/Berlin")


def minute(text: str) -> int:
    if not isinstance(text, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d|24:00", text):
        raise ValueError("Ungueltige Intervallzeit")
    h, m = map(int, text.split(":"))
    return h * 60 + m


def hhmm(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def validate_command(command: Command):
    if type(command.allow_export) is not bool or type(command.discharge_priority) is not bool:
        raise ValueError("Schalter muessen bool sein")
    if command.discharge_priority:
        raise ValueError("Entladepriorisierung ist im Winter verboten")
    if type(command.appliance_load) is not int or not 100 <= command.appliance_load <= 800:
        raise ValueError("A17C0 Einzelbank: Sollwert 100..800 W")
    if type(command.charge_priority_limit) is not int or command.charge_priority_limit not in range(
        0, 101, 5
    ):
        raise ValueError("Ladeprioritaet 0..100 in 5-Prozent-Schritten")


def validate_schedule(plan: dict, *, winter: bool = False):
    ranges = plan.get("ranges")
    if not isinstance(ranges, list) or len(ranges) < 2:
        raise ValueError("Mindestens zwei lueckenlose Intervalle erforderlich")
    end = 0
    for slot in ranges:
        start, stop = minute(slot["start_time"]), minute(slot["end_time"])
        if start != end or stop <= start:
            raise ValueError("Luecke, Ueberlappung oder falsche Reihenfolge")
        end = stop
        loads = slot.get("appliance_loads")
        if not isinstance(loads, list) or len(loads) != 1:
            raise ValueError("Nur genau eine Haushaltslast unterstuetzt")
        prio = slot.get("priority_discharge_switch")
        if prio not in (None, 0, 1):
            raise ValueError("Unbekannte Entladeprioritaet")
        if winter and prio != 0:
            raise ValueError("Winterplan braucht explizit deaktivierte Entladeprioritaet")
        command = Command(slot["turn_on"], loads[0]["power"], slot["charge_priority"], False)
        validate_command(command)
        if slot.get("power_setting_mode") not in (None, 1):
            raise ValueError("Individuelle Mehrbanklast ist nicht unterstuetzt")
        if slot.get("device_power_loads"):
            raise ValueError("Geraetespezifische Mehrbanklast braucht gesonderte Pruefung")
    if end != 1440:
        raise ValueError("Zeitplan muss den ganzen Tag abdecken")


def fingerprint(plan: dict) -> str:
    return hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def active_command(plan: dict, now: datetime) -> Command:
    validate_schedule(plan)
    local = now.astimezone(BERLIN)
    m = local.hour * 60 + local.minute
    for slot in plan["ranges"]:
        if minute(slot["start_time"]) <= m < minute(slot["end_time"]):
            return Command(
                slot["turn_on"],
                slot["appliance_loads"][0]["power"],
                slot["charge_priority"],
                bool(slot.get("priority_discharge_switch")),
            )
    raise ValueError("Kein aktives Intervall")


def observed_command(plan: dict, now: datetime) -> Command:
    """Bestandsvorgabe lesen, ohne damit einen schreibbaren Plan zu bescheinigen.

    Geraetelastanteile und unbekannte Metadaten bleiben unveraendert im Original.
    Die Haushaltsvorgabe ist weder gemessene Leistung noch ein Einzelbankanteil.
    """
    ranges = plan.get("ranges")
    if not isinstance(ranges, list) or not ranges:
        raise ValueError("Bestandsintervalle fehlen")
    local = now.astimezone(BERLIN)
    current = local.hour * 60 + local.minute
    end, active = 0, None
    for slot in ranges:
        start, stop = minute(slot["start_time"]), minute(slot["end_time"])
        if start != end or stop <= start:
            raise ValueError("Bestandsplan mit Luecke oder Ueberlappung")
        end = stop
        loads = slot.get("appliance_loads")
        if not isinstance(loads, list) or len(loads) != 1:
            raise ValueError("Haushaltsvorgabe nicht eindeutig")
        power, priority = loads[0]["power"], slot["charge_priority"]
        export, discharge = slot["turn_on"], slot.get("priority_discharge_switch")
        if (
            type(power) is not int
            or not 100 <= power <= 800
            or type(priority) is not int
            or not 0 <= priority <= 100
            or type(export) is not bool
            or type(discharge) is not int
            or discharge not in (0, 1)
            or slot.get("power_setting_mode") not in (None, 1)
        ):
            raise ValueError("Unbekannte Bestandsvorgabe")
        if start <= current < stop:
            active = Command(export, power, priority, bool(discharge))
    if end != 1440 or active is None:
        raise ValueError("Bestandsplan nicht vollstaendig")
    return active


def solar_window(now: datetime, cfg: Config) -> tuple[datetime, datetime]:
    local = now.astimezone(BERLIN)
    times = sun(Observer(cfg.latitude, cfg.longitude), date=local.date(), tzinfo=BERLIN)
    margin = timedelta(minutes=cfg.sun_margin_minutes)
    return times["sunrise"] + margin, times["sunset"] - margin


def is_day(now: datetime, cfg: Config) -> bool:
    start, end = solar_window(now, cfg)
    return start <= now < end


def safe_baseline(plan: dict) -> dict:
    validate_schedule(plan)
    new = copy.deepcopy(plan)
    for slot in new["ranges"]:
        _set(slot, Command())
    validate_schedule(new, winter=True)
    return new


def _set(slot: dict, cmd: Command):
    slot["turn_on"] = cmd.allow_export
    slot["charge_priority"] = cmd.charge_priority_limit
    slot["priority_discharge_switch"] = 0
    slot["appliance_loads"][0]["power"] = cmd.appliance_load


def propose(plan: dict, cmd: Command, now: datetime, cfg: Config) -> dict:
    """Gesamten Diff vorlegen: sichere Basis plus kurze Tagesfreigabe.

    Wiederholung am Folgetag ist bei Gen 1 moeglich! Keine echte TTL.
    Offline-Sicherheit muss am Geraet separat nachgewiesen werden.
    """
    validate_command(cmd)
    new = safe_baseline(plan)
    if not cmd.allow_export:
        return new
    if not is_day(now, cfg):
        raise ValueError("Export ausserhalb des Sonnenfensters verboten")
    local = now.astimezone(BERLIN)
    start = local.hour * 60 + local.minute
    _, sunset = solar_window(now, cfg)
    end = min(start + cfg.lease_minutes, sunset.hour * 60 + sunset.minute)
    if end <= start:
        raise ValueError("Keine sichere Tagesfreigabe mehr moeglich")
    result = []
    for slot in new["ranges"]:
        a, b = minute(slot["start_time"]), minute(slot["end_time"])
        cuts = sorted({a, b, *[m for m in (start, end) if a < m < b]})
        for lo, hi in zip(cuts, cuts[1:], strict=False):
            part = copy.deepcopy(slot)
            part["start_time"], part["end_time"] = hhmm(lo), hhmm(hi)
            if start <= lo < end:
                _set(part, cmd)
            result.append(part)
    # Gleiche Nachbarintervalle zusammenfassen, Metadaten unveraendert lassen.
    merged = []
    for part in result:

        def key(s):
            return {k: v for k, v in s.items() if k not in {"start_time", "end_time"}}

        if merged and key(merged[-1]) == key(part):
            merged[-1]["end_time"] = part["end_time"]
        else:
            merged.append(part)
    new["ranges"] = merged
    validate_schedule(new, winter=True)
    return new


def mock_schedule() -> dict:
    return {
        "min_load": 100,
        "max_load": 800,
        "ranges": [
            {
                "id": 0,
                "start_time": a,
                "end_time": b,
                "turn_on": False,
                "appliance_loads": [{"id": 0, "name": "Custom", "power": 100, "number": 1}],
                "charge_priority": 100,
                "priority_discharge_switch": 0,
            }
            for a, b in (("00:00", "12:00"), ("12:00", "24:00"))
        ],
    }
