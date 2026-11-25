"""Reiner Zustandsautomat; keine API-Aufrufe oder optimistische Messannahmen."""

from collections import deque

from .config import Config
from .models import Command, Decision, Sample, State
from .schedule import is_day
from .telemetry import issues


class Controller:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state = State.NIGHT_SAFE
        self.changed_at = None
        self.pv = deque()
        self.last_device_at = None
        self.discharge_latched = False
        self.weak_days = 0

    def reserves(self):
        c = self.cfg
        if c.reserve_mode == "nominal":
            return c.soc_low, c.soc_high
        low = min(c.adaptive_max, c.adaptive_low + 5 * max(0, self.weak_days - 1))
        return low, low + 5

    def decide(self, s: Sample) -> Decision:
        c = self.cfg
        low, high = self.reserves()
        age = (
            (s.observed_at - s.device_at).total_seconds()
            if s.device_at and s.device_at.tzinfo and s.observed_at.tzinfo
            else None
        )

        def result(state, cmd, reason, alarms=()):
            if state != self.state:
                self.changed_at = s.observed_at
                self.state = state
            return Decision(state, cmd, reason, tuple(alarms), low, high, age)

        errors = issues(s, c)
        if s.model != "A17C0" or s.inverter != "MI80" or s.bank_count != 1:
            errors.append("Nur einzelne A17C0 mit MI80 freigegeben")
        if errors:
            self.pv.clear()
            return result(State.FAULT_SAFE, None, "Keine Steuerung mit ungueltigen Daten", errors)
        if s.device_at != self.last_device_at:
            if self.last_device_at and (s.device_at - self.last_device_at).total_seconds() <= 0:
                self.pv.clear()
                return result(
                    State.FAULT_SAFE, None, "Geraetezeit laeuft rueckwaerts", ("Zeitfehler",)
                )
            self.pv.append((s.device_at, s.pv_w))
            self.last_device_at = s.device_at
        while self.pv and (s.device_at - self.pv[0][0]).total_seconds() > c.stable_s:
            self.pv.popleft()
        if s.battery_w < -c.discharge_alarm_w or s.mode == "discharge":
            self.discharge_latched = True
        if self.discharge_latched:
            return result(
                State.FAULT_SAFE,
                Command(),
                "Entladung erkannt: Schutz bleibt verriegelt",
                ("Unerwartete Batterieentladung; manuelle Pruefung erforderlich",),
            )
        if not s.discharge_priority_known or (s.active and s.active.discharge_priority):
            return result(
                State.FAULT_SAFE,
                Command(),
                "Entladepriorisierung nicht sicher deaktiviert",
                ("Firmware und Zeitplan pruefen",),
            )
        if not is_day(s.observed_at, c) or s.pv_w < c.pv_stop_w:
            return result(State.NIGHT_SAFE, Command(), "Nacht oder unzureichende PV")
        if s.soc < low:
            return result(State.CHARGE_PROTECT, Command(), "Winterreserve hat Vorrang")
        if s.temperature_c is None or not 0 <= s.temperature_c <= 55:
            return result(
                State.HOLD,
                Command(),
                "Ladefaehigkeit bei Temperatur unbekannt/eingeschraenkt",
                ("Laden nicht garantiert; keine Entladung zum SOC-Ausgleich",),
            )
        if s.battery_quality not in {"simulated", "reported"} or age > c.active_age_s:
            return result(State.HOLD, Command(), "Batterierueckmeldung fuer Freigabe unzureichend")
        was_on = self.state == State.PV_BYPASS and s.active and s.active.allow_export
        span = (self.pv[-1][0] - self.pv[0][0]).total_seconds() if self.pv else 0
        gaps_ok = all(
            (b[0] - a[0]).total_seconds() <= c.cycle_s + 60
            for a, b in zip(self.pv, list(self.pv)[1:], strict=False)
        )
        stable = span >= c.stable_s and gaps_ok
        pv_min = min((p for _, p in self.pv), default=0)
        threshold = c.pv_stop_w if was_on else c.pv_start_w
        if not stable or pv_min < threshold:
            return result(State.HOLD, Command(), "PV noch nicht ausreichend stabil")
        if s.soc <= high and not was_on:
            return result(State.HOLD, Command(), "Hysterese: Reserveladung beibehalten")
        if (
            self.changed_at
            and self.state != State.PV_BYPASS
            and (s.observed_at - self.changed_at).total_seconds() < c.dwell_s
        ):
            return result(State.HOLD, Command(), "Mindestverweilzeit fuer Optimierung")
        watts = 100 if c.strategy == "binary" else min(800, max(100, int(pv_min * 0.8 / 10) * 10))
        return result(
            State.PV_BYPASS,
            Command(True, watts, 0, False),
            "Begrenzte PV-Freigabe; 100 W ist kein Nachweis fuer reine Durchleitung",
        )
