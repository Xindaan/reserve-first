"""Messvalidierung ohne aus Abfragezeiten erfundene Datenfrische."""

import math

from .config import Config
from .models import Sample


def issues(sample: Sample, cfg: Config) -> list[str]:
    errors = []
    if not sample.valid:
        errors.append("Datenquelle meldet ungueltige Daten")
    if not sample.timestamp_verified:
        errors.append("Zeitbasis der Geraetedaten nicht verifiziert")
    if (
        sample.observed_at.tzinfo is None
        or sample.device_at is None
        or sample.device_at.tzinfo is None
    ):
        errors.append("Geraetezeitstempel fehlt oder ist nicht zeitzonenbewusst")
    else:
        age = (sample.observed_at - sample.device_at).total_seconds()
        limit = cfg.standby_age_s if sample.mode == "standby" else cfg.active_age_s
        if age < -30 or age > limit:
            errors.append("Geraetedaten veraltet oder aus der Zukunft")
    for name, low, high in (
        ("soc", 0, 100),
        ("pv_w", 0, 850),
        ("output_dc_w", 0, 850),
        ("battery_w", -850, 850),
    ):
        value = getattr(sample, name)
        if (
            value is None
            or isinstance(value, bool)
            or not math.isfinite(value)
            or not low <= value <= high
        ):
            errors.append(f"{name} fehlt oder ist unplausibel")
    if sample.temperature_c is not None and (
        not math.isfinite(sample.temperature_c) or not -40 <= sample.temperature_c <= 80
    ):
        errors.append("Unplausible Temperatur")
    if not errors and abs(sample.pv_w - sample.output_dc_w - sample.battery_w) > 50:
        errors.append("Leistungsbilanz widerspruechlich")
    return errors
