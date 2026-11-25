"""Konfiguration mit harten Grenzen fuer die konservative Regelung."""

from dataclasses import asdict, dataclass, fields
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Config:
    strategy: str = "binary"
    reserve_mode: str = "nominal"
    soc_low: float = 45
    soc_high: float = 55
    soc_target: float = 50
    adaptive_low: float = 60
    adaptive_max: float = 65
    weak_day_wh: float = 400
    pv_start_w: float = 60
    pv_stop_w: float = 40
    stable_s: int = 900
    dwell_s: int = 600
    cycle_s: int = 300
    active_age_s: int = 180
    standby_age_s: int = 3900
    write_interval_s: int = 600
    settle_s: int = 120
    max_writes_hour: int = 6
    max_writes_day: int = 48
    lease_minutes: int = 10
    discharge_alarm_w: float = 3
    latitude: float = 52.52
    longitude: float = 13.405
    sun_margin_minutes: int = 30
    timestamp_timezone: str = "Europe/Berlin"
    timestamp_verified: bool = False

    def __post_init__(self):
        if self.strategy not in {"binary", "continuous"}:
            raise ValueError("Unbekannte Strategie")
        if self.reserve_mode not in {"nominal", "adaptive"}:
            raise ValueError("Unbekannte Reserve")
        if not 45 <= self.soc_low < self.soc_target < self.soc_high <= 95:
            raise ValueError("Ungueltige SOC-Hysterese; Untergrenze mindestens 45")
        if not self.soc_high <= self.adaptive_low <= self.adaptive_max <= 90:
            raise ValueError("Adaptive Reserve ausserhalb der Grenzen")
        if not 20 < self.pv_stop_w < self.pv_start_w <= 800:
            raise ValueError("PV-Schwellen brauchen Abstand zur 20-W-Umschaltung")
        if not (60 <= self.cycle_s <= 300 and self.stable_s >= 600):
            raise ValueError("Zyklus 60..300 s, Stabilitaet mindestens 600 s")
        if not (600 <= self.write_interval_s <= 3600 and self.dwell_s >= 600):
            raise ValueError("Mindestabstand/Verweilzeit zu kurz")
        if not (120 <= self.settle_s <= self.write_interval_s):
            raise ValueError("Ungueltige Einschwingzeit")
        if not (1 <= self.active_age_s <= 300 < self.standby_age_s <= 7200):
            raise ValueError("Ungueltiges Datenalter")
        if not (2 <= self.max_writes_hour <= 6 and 2 <= self.max_writes_day <= 144):
            raise ValueError("Ungueltiges Schreibbudget")
        if not 5 <= self.lease_minutes <= 10:
            raise ValueError("Exportfreigabe hoechstens 10 Minuten")
        if not 0 <= self.discharge_alarm_w <= 5:
            raise ValueError("Entladealarm darf nicht abgeschaltet werden")
        if not (0 <= self.sun_margin_minutes <= 120 and self.weak_day_wh > 0):
            raise ValueError("Ungueltige Sonnen-/Ertragsparameter")
        if not (-90 <= self.latitude <= 90 and -180 <= self.longitude <= 180):
            raise ValueError("Ungueltiger Standort")
        # Auch NaN und bool statt Zahl explizit abweisen.
        import math

        for f in fields(self):
            value = getattr(self, f.name)
            if f.name == "timestamp_verified":
                if type(value) is not bool:
                    raise ValueError("timestamp_verified muss bool sein")
            elif f.type in {int, float} or isinstance(value, (int, float)):
                if isinstance(value, bool) or not math.isfinite(value):
                    raise ValueError("Ungueltige Zahl")
        for name in (
            "stable_s",
            "dwell_s",
            "cycle_s",
            "active_age_s",
            "standby_age_s",
            "write_interval_s",
            "settle_s",
            "max_writes_hour",
            "max_writes_day",
            "lease_minutes",
            "sun_margin_minutes",
        ):
            if type(getattr(self, name)) is not int:
                raise ValueError(f"{name} muss ganzzahlig sein")
        if self.timestamp_timezone != "Europe/Berlin":
            raise ValueError("Dieser Pilot ist auf Europe/Berlin begrenzt")

    @classmethod
    def load(cls, path: str | None):
        if not path:
            return cls()
        data = yaml.safe_load(Path(path).read_text())
        if not isinstance(data, dict):
            raise ValueError("Konfiguration muss ein Mapping sein")
        return cls(**data)

    def export(self):
        return asdict(self)
