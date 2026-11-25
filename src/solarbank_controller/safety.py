"""Schreibtransaktionen: Opt-in, Konflikte, Budgets und ungewisse Antworten."""

from datetime import timedelta

from .config import Config
from .models import Decision, Result, State
from .schedule import fingerprint, is_day, propose, validate_command, validate_schedule
from .telemetry import issues


class Executor:
    def __init__(self, adapter, store, cfg: Config, *, execute=False):
        self.adapter, self.store, self.cfg, self.execute = adapter, store, cfg, execute

    async def apply(self, sample, decision: Decision) -> Result:
        c, st, now = self.cfg, self.store, sample.observed_at
        if st.get("stopped", False):
            return Result("stopped", "Lokaler Stopp; bestehender Geraeteplan bleibt aktiv")
        if st.get("inflight") or st.get("blocked"):
            return Result(
                "blocked", "Ungeklaerte Transaktion/Konflikt; manuelle Pruefung erforderlich"
            )
        if self.execute and self.adapter.live:
            return Result("blocked", "Live-Schreiben in dieser Pilotversion gesperrt")
        cmd = decision.command
        if cmd is None:
            return Result(
                "alarm", "Nachtabschaltung kann mit diesen Daten nicht gewaehrleistet werden"
            )
        try:
            validate_command(cmd)
            validate_schedule(sample.schedule)
            errors = issues(sample, c)
            if (
                errors
                or sample.model != "A17C0"
                or sample.inverter != "MI80"
                or sample.bank_count != 1
            ):
                raise ValueError("Ungueltige Daten oder Hardware")
            if not sample.owner:
                raise ValueError("Schreibberechtigung nicht bestaetigt")
            if cmd.allow_export and (
                sample.soc < max(45, decision.reserve_low)
                or not is_day(now, c)
                or sample.pv_w < c.pv_stop_w
                or sample.battery_w < -c.discharge_alarm_w
                or sample.mode == "discharge"
                or decision.state != State.PV_BYPASS
                or sample.battery_quality not in {"simulated", "reported"}
                or not sample.discharge_priority_known
                or sample.temperature_c is None
                or not 0 <= sample.temperature_c <= 55
                or (now - sample.device_at).total_seconds() > c.active_age_s
            ):
                raise ValueError("Export verletzt Schutzbedingungen")
            oldhash = fingerprint(sample.schedule)
            expected = st.get("expected")
            if expected and expected != oldhash:
                st.put("blocked", "external_change")
                return Result(
                    "conflict", "Externe Aenderung erkannt; keine automatische Ueberschreibung"
                )
            st.put("expected", oldhash)
            plan = propose(sample.schedule, cmd, now, c)
        except (ValueError, KeyError, TypeError):
            return Result(
                "blocked", "Zeitplan, Identitaet, Messwerte oder Stellwerte nicht validiert"
            )
        if not cmd.allow_export and fingerprint(plan) == oldhash:
            return Result(
                "unchanged", "Schutzplan bereits bestaetigt; physische Wirkung separat pruefen"
            )
        if not self.execute:
            return Result("dry-run", "Zeitplanaenderung nur berechnet; keine Wirkung am Geraet")
        if cmd.allow_export and st.get("pending_behavior"):
            return Result("deferred", "Vorherige Wirkung muss zuerst geprueft werden")
        # Eine Abschaltung darf die Verweilzeit umgehen; ein Budgetplatz bleibt reserviert.
        emergency = not cmd.allow_export
        last = st.get("last_write", 0)
        if not emergency and now.timestamp() - last < c.write_interval_s:
            return Result("deferred", "Mindestabstand zwischen Schreibversuchen")
        reserve = 0 if emergency else 1
        if (
            st.count((now - timedelta(hours=1)).timestamp()) >= c.max_writes_hour - reserve
            or st.count((now - timedelta(days=1)).timestamp()) >= c.max_writes_day - reserve
        ):
            return Result("alarm", "Schreibbudget erschoepft; Schutzwirkung nicht garantiert")
        fresh = await self.adapter.read()
        if (
            fresh.device_id != sample.device_id
            or fresh.site_id != sample.site_id
            or fingerprint(fresh.schedule) != oldhash
        ):
            st.put("blocked", "race_or_identity")
            return Result("conflict", "Identitaet/Zeitplan zwischen Lesen und Schreiben geaendert")
        if (
            issues(fresh, c)
            or not fresh.owner
            or fresh.model != "A17C0"
            or fresh.inverter != "MI80"
            or fresh.bank_count != 1
        ):
            return Result("blocked", "Frische Vorpruefung vor Schreiben fehlgeschlagen")
        if cmd.allow_export and (
            fresh.soc < max(45, decision.reserve_low)
            or not is_day(fresh.observed_at, c)
            or fresh.pv_w < c.pv_stop_w
            or fresh.battery_w < -c.discharge_alarm_w
            or fresh.mode == "discharge"
            or fresh.battery_quality not in {"simulated", "reported"}
            or fresh.active is None
            or fresh.active.discharge_priority
            or not fresh.discharge_priority_known
            or fresh.temperature_c is None
            or not 0 <= fresh.temperature_c <= 55
            or (fresh.observed_at - fresh.device_at).total_seconds() > c.active_age_s
        ):
            return Result("blocked", "Messwerte vor Schreibzugriff verschlechtert")
        # Bei Wiederanlauf nie blind wiederholen. Journal vor dem Netzaufruf dauerhaft sichern.
        backup = st.backup(fresh)
        st.put("inflight", {"before": oldhash, "after": fingerprint(plan), "backup": backup})
        st.write_attempt(now, "protect" if emergency else "export")
        st.put("last_write", now.timestamp())
        try:
            await self.adapter.write(plan, expected=oldhash)
            confirm = await self.adapter.read()
            if (
                fingerprint(confirm.schedule) != fingerprint(plan)
                or confirm.device_id != sample.device_id
                or confirm.site_id != sample.site_id
            ):
                raise ValueError("Zeitplanbestaetigung fehlgeschlagen")
        except Exception:
            return Result("uncertain", "Schreibstatus ungewiss; gesperrt, keine Wiederholung", 1)
        st.put("expected", fingerprint(plan))
        st.put("inflight", None)
        st.put("pending_behavior", {"at": now.isoformat(), "export": cmd.allow_export})
        return Result("written", "Plan rueckgelesen; physische Wirkung noch ausstehend", 1)

    def confirm_behavior(self, sample) -> str | None:
        pending = self.store.get("pending_behavior")
        if not pending:
            return None
        from datetime import datetime

        after = datetime.fromisoformat(pending["at"]) + timedelta(seconds=self.cfg.settle_s)
        if sample.observed_at < after:
            return "Einschwingzeit"
        if (
            issues(sample, self.cfg)
            or sample.device_at < after
            or sample.battery_quality not in {"simulated", "reported"}
        ):
            self.store.put("blocked", "missing_behavior")
            return "Wirkungsnachweis fehlt; Optimierung gesperrt"
        if sample.battery_w < -self.cfg.discharge_alarm_w:
            # Controller loest Schutz aus; nicht den Schutzbefehl selbst blockieren.
            self.store.put("pending_behavior", None)
            return "Batterieentladung trotz Anforderung; sofortiger Schutz erforderlich"
        if not pending["export"] and sample.output_dc_w > 3:
            self.store.put("blocked", "zero_output_failed")
            return "0-W-Wirkung nicht bestaetigt; manuelle Pruefung erforderlich"
        self.store.put("pending_behavior", None)
        return "Frische simulierte/gemeldete Rueckmeldung plausibel; kein Offline-Nachweis"


class Backoff:
    def __init__(self, store, base=300, maximum=3600):
        self.store, self.base, self.maximum = store, base, maximum

    def failed(self, now):
        n = min(12, self.store.get("failures", 0) + 1)
        delay = min(self.maximum, self.base * 2 ** (n - 1))
        self.store.put("failures", n)
        self.store.put("next_read", now.timestamp() + delay)
        return delay

    def ready(self, now):
        return now.timestamp() >= self.store.get("next_read", 0)

    def success(self):
        self.store.put("failures", 0)
        self.store.put("next_read", 0)
