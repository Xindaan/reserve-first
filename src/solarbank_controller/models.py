"""Explizite Messqualitaet, Sollwerte und Regelzustaende."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class State(StrEnum):
    CHARGE_PROTECT = "CHARGE_PROTECT"
    HOLD = "HOLD"
    PV_BYPASS = "PV_BYPASS"
    NIGHT_SAFE = "NIGHT_SAFE"
    FAULT_SAFE = "FAULT_SAFE"


@dataclass(frozen=True)
class Command:
    # 0 W wird durch den Schalter angefragt, nie als unzulaessiger Gen-1-Sollwert.
    allow_export: bool = False
    appliance_load: int = 100
    charge_priority_limit: int = 100
    discharge_priority: bool = False


@dataclass(frozen=True)
class Sample:
    observed_at: datetime
    device_at: datetime | None
    soc: float | None
    pv_w: float | None
    battery_w: float | None  # positiv Laden, negativ Entladen
    output_dc_w: float | None
    household_ac_w: float | None = None  # kein Haushaltsverbrauch ohne Smart Meter
    temperature_c: float | None = None
    battery_quality: str = "unknown"  # simulated, reported, estimated, unknown
    mode: str = "unknown"
    valid: bool = True
    timestamp_verified: bool = True
    model: str = "A17C0"
    inverter: str = "MI80"
    firmware: str = "unknown"
    device_id: str = "mock-device"
    site_id: str = "mock-site"
    bank_count: int = 1
    owner: bool = False
    schedule: dict[str, Any] = field(default_factory=dict)
    active: Command | None = None
    discharge_priority_known: bool = False


@dataclass(frozen=True)
class Decision:
    state: State
    command: Command | None
    reason: str
    alarms: tuple[str, ...] = ()
    reserve_low: float = 45
    reserve_high: float = 55
    data_age_s: float | None = None


@dataclass(frozen=True)
class Result:
    action: str
    detail: str
    writes: int = 0
