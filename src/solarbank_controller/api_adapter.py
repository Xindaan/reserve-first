"""Bestehende Bibliothek nutzen; Cloud-Transport ausschliesslich lesend."""

import copy
import logging
import math
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config
from .models import Sample
from .schedule import active_command, fingerprint, mock_schedule, observed_command
from .storage import private_dir


class WriteBlocked(RuntimeError):
    pass


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def device_time(value, zone):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.year < 2020:
            return None
        if dt.tzinfo is None:
            # Doppelte und nicht existierende lokale Zeiten nicht erraten.
            z = ZoneInfo(zone)
            a, b = dt.replace(tzinfo=z, fold=0), dt.replace(tzinfo=z, fold=1)
            if a.utcoffset() != b.utcoffset():
                return None
            if a.astimezone(UTC).astimezone(z).replace(tzinfo=None) != dt:
                return None
            dt = a
        return dt.astimezone(UTC)
    except (ValueError, TypeError):
        return None


def device_firmware(dev, bound_devices):
    """Firmware nur aus eindeutig passenden Identitaeten zusammenfuehren."""
    matches = [d for d in bound_devices if d.get("device_sn") == dev.get("device_sn")]
    scene_version = dev.get("main_version") or ""
    if not matches:
        return scene_version or "unknown"
    if len(matches) != 1 or matches[0].get("product_code") != dev.get("device_pn"):
        return "unknown"
    bound_version = matches[0].get("device_sw_version") or ""
    if scene_version and bound_version and scene_version.lstrip("v") != bound_version.lstrip("v"):
        return "unknown"
    return scene_version or bound_version or "unknown"


def parse_scene(
    scene, load, solar, *, device_id, site_id, owner, cfg, now, bound_devices=()
) -> Sample:
    info = scene.get("solarbank_info") or {}
    banks = info.get("solarbank_list") or []
    found = [d for d in banks if d.get("device_sn") == device_id]
    if len(found) != 1:
        raise ValueError("Zielgeraet fehlt oder ist nicht eindeutig")
    dev = found[0]
    plan = load.get("home_load_data") or {}
    pv, dc = number(dev.get("photovoltaic_power")), number(dev.get("output_power"))
    charge, discharge = number(dev.get("bat_charge_power")), number(dev.get("bat_discharge_power"))
    quality = "unknown"
    battery = None
    if charge is not None and discharge is not None and charge >= 0 and discharge >= 0:
        battery, quality = charge - discharge, "reported"
        if pv is not None and dc is not None:
            balance = pv - dc
            # A17C0 kann bat_* = 0 trotz echter Ausgangsleistung liefern.
            # Eine Bilanz ist nur eine Schaetzung, nie ein direkter Messkanal.
            if (balance < -cfg.discharge_alarm_w and battery >= -cfg.discharge_alarm_w) or (
                charge == discharge == 0 and abs(balance) > cfg.discharge_alarm_w
            ):
                battery, quality = balance, "estimated"
    elif pv is not None and dc is not None:
        battery, quality = pv - dc, "estimated"
    try:
        active = observed_command(plan, now)
    except (ValueError, KeyError, TypeError):
        active = None
    # A5143 ist MI80; eine widerspruechliche App-Zuordnung bleibt ein Befund.
    inverter = "MI80" if solar.get("solar_model") == "A5143" else "unknown"
    modes = {
        "0": "detection",
        "1": "bypass",
        "2": "discharge",
        "3": "charge",
        "7": "standby",
        "6": "full_bypass",
    }
    return Sample(
        now,
        device_time(info.get("updated_time"), cfg.timestamp_timezone),
        number(dev.get("battery_power")),
        pv,
        battery,
        dc,
        temperature_c=None,
        battery_quality=quality,
        mode=modes.get(str(dev.get("charging_status")), "unknown"),
        valid=bool(info.get("is_display_data", True)) and not (charge and discharge),
        timestamp_verified=cfg.timestamp_verified,
        model=dev.get("device_pn", "unknown"),
        inverter=inverter,
        firmware=device_firmware(dev, bound_devices),
        device_id=device_id,
        site_id=site_id,
        bank_count=len(banks),
        owner=owner,
        schedule=plan,
        active=active,
        discharge_priority_known=bool(plan.get("ranges"))
        and all(s.get("priority_discharge_switch") in (0, 1) for s in plan["ranges"]),
    )


class CloudAdapter:
    live = True

    def __init__(self, api, cfg: Config, site_id: str, device_id: str):
        self.api, self.cfg = api, cfg
        self.site_id, self.device_id = site_id, device_id
        self.owner = False

    async def discover(self):
        sites = (await self.api.get_site_list()).get("site_list", [])
        bound_devices = (await self.api.get_bind_devices()).get("data", [])
        result = []
        for site in sites:
            scene = await self.api.get_scene_info(siteId=site["site_id"])
            for bank in (scene.get("solarbank_info") or {}).get("solarbank_list", []):
                result.append(
                    {
                        "site_id": site["site_id"],
                        "device_id": bank.get("device_sn"),
                        "model": bank.get("device_pn"),
                        "firmware": device_firmware(bank, bound_devices),
                        "owner": site.get("ms_type") == 1,
                    }
                )
        return result

    async def read(self) -> Sample:
        if not self.device_id or not self.site_id:
            raise ValueError("ANKER_DEVICE_SN und ANKER_SITE_ID fehlen")
        sites = (await self.api.get_site_list()).get("site_list", [])
        selected = [s for s in sites if s.get("site_id") == self.site_id]
        if len(selected) != 1:
            raise ValueError("Ausgewaehlte Anlage fehlt")
        self.owner = selected[0].get("ms_type") == 1
        scene = await self.api.get_scene_info(siteId=self.site_id)
        bound_devices = (await self.api.get_bind_devices()).get("data", [])
        # Detail-Reads setzen Eigentuemerrechte voraus; keine Schreibversuche.
        load = (
            await self.api.get_device_load(siteId=self.site_id, deviceSn=self.device_id)
            if self.owner
            else {}
        )
        solar = await self.api.get_solar_info(solarbankSn=self.device_id) if self.owner else {}
        return parse_scene(
            scene,
            load,
            solar,
            device_id=self.device_id,
            site_id=self.site_id,
            owner=self.owner,
            cfg=self.cfg,
            now=datetime.now(UTC),
            bound_devices=bound_devices,
        )

    async def write(self, plan, expected):
        raise WriteBlocked(
            "Live-Schreibpfad dieser Pilotversion ist gesperrt: MI80-Ausfallnachweis fehlt"
        )


@asynccontextmanager
async def cloud_connection(cfg: Config, directory: Path):
    # Import erst bei bewusstem Cloud-Aufruf. Simulation braucht das Extra nicht.
    from aiohttp import ClientSession, ClientTimeout
    from anker_solix_api.api import AnkerSolixApi
    from anker_solix_api.apitypes import API_ENDPOINTS, API_LOGIN
    from anker_solix_api.session import AnkerSolixClientSession

    allowed = {
        API_LOGIN,
        *[
            API_ENDPOINTS[k]
            for k in ("site_list", "scene_info", "bind_devices", "get_device_load", "solar_info")
        ],
    }

    class ReadOnlySession(AnkerSolixClientSession):
        async def request(self, method, endpoint, **kwargs):
            if endpoint not in allowed:
                raise WriteBlocked("Nicht freigegebener Cloud-Endpunkt")
            return await super().request(method, endpoint, **kwargs)

    email, password = os.environ.get("ANKER_EMAIL"), os.environ.get("ANKER_PASSWORD")
    if not email or not password:
        raise ValueError("ANKER_EMAIL und ANKER_PASSWORD fehlen")
    private_dir(directory)
    logger = logging.getLogger("solarbank.private.sdk")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    logger.disabled = True
    async with ClientSession(timeout=ClientTimeout(total=30)) as web:
        session = ReadOnlySession(
            email, password, os.environ.get("ANKER_COUNTRY", "DE"), web, logger
        )
        # Upstream hat keinen oeffentlichen Cachepfad-Setter. Gepinnter, getesteter Shim.
        session._authFile = str(directory / "auth.json")
        if Path(session._authFile).is_symlink():
            raise ValueError("Authcache-Symlink abgewiesen")
        session.requestTimeout(10)
        api = AnkerSolixApi(apisession=session, logger=logger)
        yield CloudAdapter(
            api, cfg, os.environ.get("ANKER_SITE_ID", ""), os.environ.get("ANKER_DEVICE_SN", "")
        )


class MockAdapter:
    live = False

    def __init__(self, sample: Sample):
        self.sample = replace(sample, schedule=copy.deepcopy(sample.schedule or mock_schedule()))
        self.writes = []
        self.failure = None
        self.apply_before_failure = False

    async def read(self):
        if self.failure == "read":
            raise ConnectionError("Simulierter Ausfall")
        return replace(
            self.sample, active=active_command(self.sample.schedule, self.sample.observed_at)
        )

    async def write(self, plan, expected):
        if fingerprint(self.sample.schedule) != expected:
            raise ValueError("Externe Zeitplanaenderung")
        self.writes.append(copy.deepcopy(plan))
        if self.failure != "write" or self.apply_before_failure:
            self.sample = replace(self.sample, schedule=copy.deepcopy(plan))
        if self.failure == "write":
            raise ConnectionError("Simulierter Ausfall nach ungewissem Schreibstatus")
