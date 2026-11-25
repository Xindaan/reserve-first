import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from solarbank_controller.api_adapter import WriteBlocked
from solarbank_controller.collector import AnmeldungNoetig
from solarbank_controller.history import ZONE, Historie, lesesitzung, punkte


def antwort(wert="12.5"):
    return {
        "power_unit": "wh",
        "power": [
            {"time": "12:00", "value": wert},
            {"time": "23:40", "value": "0"},
            {"time": "24:00", "value": "0"},
        ],
        "charge_level": [{"time": "12:00", "value": "0"}],
    }


def test_originalzeit_einheit_null_und_zukunft():
    now = datetime(2026, 11, 16, 14, tzinfo=ZONE)
    p = punkte(antwort("0"), now.date(), "solar_production", now.timestamp())
    assert list(p) == ["12:00"]
    assert p["12:00"] == (now.replace(hour=12).timestamp(), 0)
    with pytest.raises(ValueError):
        punkte({"power_unit": "w", "power": []}, now.date(), "solar_production", now.timestamp())
    assert not punkte(antwort("NaN"), now.date(), "solar_production", now.timestamp())


def test_zeitumstellung_keine_erfundene_doppelte_stunde():
    now = datetime(2026, 10, 25, 12, tzinfo=ZONE)
    data = {
        "power_unit": "wh",
        "power": [{"time": "02:20", "value": "4"}, {"time": "03:20", "value": "5"}],
    }
    assert list(punkte(data, now.date(), "solarbank", now.timestamp())) == ["03:20"]


def test_mi80_keine_zukunft_oder_hersteller_interpolation():
    now = datetime(2026, 11, 16, 14, tzinfo=ZONE)
    raw = {
        "energyUnit": "kWh",
        "curve": [
            {"time": "12:00", "power": 52, "isFuture": False, "isFix": False},
            {"time": "12:05", "power": 44, "isFuture": False, "isFix": True},
            {"time": "12:10", "power": 0, "isFuture": True, "isFix": False},
            {"time": "12:15", "power": None, "isFuture": False, "isFix": False},
            {"time": "15:00", "power": 0, "isFuture": False, "isFix": False},
        ],
    }
    result = punkte(raw, now.date(), "inverter", now.timestamp())
    assert list(result) == ["12:00"]
    assert result["12:00"][1] == 52


def test_offline_neustart_nachholen_korrektur_ohne_duplikate(tmp_path):
    now = datetime(2026, 11, 16, 14, tzinfo=ZONE).timestamp()
    h = Historie(tmp_path)
    failed = AsyncMock(side_effect=ConnectionError("secret"))
    assert asyncio.run(h.nachholen(failed, now)) == 0
    assert failed.await_count == 1
    assert h.db.execute("SELECT count(*) FROM points").fetchone()[0] == 0
    h.db.close()
    h = Historie(tmp_path)

    async def daten(tag, art, value="12.5"):
        if art == "inverter":
            return {
                "energyUnit": "kWh",
                "curve": [
                    {"time": t, "power": float(value), "isFuture": False, "isFix": False}
                    for t in ("12:00", "23:40", "24:00")
                ],
            }
        return antwort(value)

    read = AsyncMock(side_effect=daten)
    assert asyncio.run(h.nachholen(read, now + 1801)) == 4
    assert h.db.execute("SELECT count(*) FROM points").fetchone()[0] == 5
    before = json.loads((tmp_path / "history.json").read_text())["points"]
    assert len({p["key"] for p in before}) == len(before)
    # Derselbe Zeitbereich mit spaeter korrigiertem Herstellerwert.
    h.db.execute("UPDATE jobs SET due=0")
    h.db.commit()

    async def korrektur(tag, art):
        return await daten(tag, art, "15")

    asyncio.run(h.nachholen(AsyncMock(side_effect=korrektur), now + 3601))
    after = json.loads((tmp_path / "history.json").read_text())["points"]
    assert len(after) == len(before)
    assert {p["key"] for p in after} == {p["key"] for p in before}
    assert any(p["value"] == 15 for p in after)
    assert "secret" not in (tmp_path / "history.json").read_text()
    assert all(p["field"] != "soc" for p in after)
    assert all(p["unit"] == "W" for p in after)
    h.db.close()


def test_endpunkt_sperre_kein_login_kein_geraeteschreiben(tmp_path, monkeypatch):
    pytest.importorskip("anker_solix_api")
    from aiohttp import ClientSession
    from anker_solix_api.apitypes import API_ENDPOINTS, API_LOGIN

    network = AsyncMock(return_value={"data": {}})
    monkeypatch.setattr("anker_solix_api.session.AnkerSolixClientSession.request", network)

    async def check():
        async with ClientSession() as web:
            s = lesesitzung()("fake@example.invalid", "unused", "DE", web)
            for endpoint in (
                API_LOGIN,
                API_ENDPOINTS["scene_info"],
                API_ENDPOINTS["set_device_parm"],
            ):
                with pytest.raises(WriteBlocked):
                    await s.request("post", endpoint)
            with pytest.raises(AnmeldungNoetig):
                await s.async_authenticate(restart=True)
            network.assert_not_called()
            await s.request("post", API_ENDPOINTS["energy_analysis"])
            assert network.await_count == 1

    asyncio.run(check())


def test_tagesbilanz_nullplatzhalter_und_watt_export(tmp_path):
    now = datetime(2026, 11, 16, 14, tzinfo=ZONE).timestamp()
    h = Historie(tmp_path)
    raw = antwort("0") | {
        "charge_total": "0.04",
        "charge_unit": "kwh",
        "discharge_total": "0.07",
        "discharge_unit": "kwh",
    }
    for tag, received in [("2026-11-15", now), ("2026-11-14", now - 2 * 86400)]:
        (tmp_path / f"raw-{tag}-solarbank.json").write_text(json.dumps(raw))
        h.db.execute("INSERT INTO jobs VALUES(?,?,0,?,NULL)", (tag, "solarbank", received))
        h.db.execute(
            "INSERT INTO points VALUES(?,?,?,?,?,?)",
            (tag, "solarbank", "12:00", received - 86400, 0, received),
        )
    h.db.execute(
        "INSERT INTO points VALUES(?,?,?,?,?,?)",
        ("2026-11-15", "solar_production", "12:00", now - 86400, 42, now),
    )
    h.db.commit()
    h.exportieren(now)
    result = json.loads((tmp_path / "history.json").read_text())
    p = result["points"]
    assert result["incomplete_solarbank_days"] == ["2026-11-14", "2026-11-15"]
    assert not any(x["field"] == "solarbank_output_history_w" for x in p)
    assert [(x["value"], x["unit"]) for x in p if x["field"] == "solar_history_w"] == [(42, "W")]
    # Nov14 raw fetched BEFORE midnight Nov15: it must not become a final daily total.
    daily = [x for x in p if x["unit"] == "kWh"]
    assert {x["field"]: x["value"] for x in daily} == {
        "battery_charge_day_kwh": 0.04,
        "solarbank_output_day_kwh": 0.07,
    }
    assert len(daily) == 2 and all(x["key"].startswith("2026-11-15/") for x in daily)
    assert h.db.execute("SELECT count(*) FROM points").fetchone()[0] == 3
    h.exportieren(now)
    assert json.loads((tmp_path / "history.json").read_text()) == result
    h.db.close()
