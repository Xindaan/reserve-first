import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from solarbank_controller.api_adapter import WriteBlocked
from solarbank_controller.collector import (
    AnmeldungNoetig,
    cache_pruefen,
    lesesitzung,
    prozesssperre,
    sammeln,
)
from solarbank_controller.models import Result
from solarbank_controller.storage import Store


def cache(tmp_path, now):
    p = tmp_path / "auth.json"
    p.write_text(json.dumps({"token_expires_at": (now + timedelta(days=30)).timestamp()}))
    return p


def test_laenger_als_48h_ohne_regler_oder_schreiben(tmp_path, sample):
    now = [datetime(2026, 11, 15, tzinfo=UTC)]
    auth = cache(tmp_path, now[0])
    calls = []

    class Adapter:
        async def read(self):
            calls.append(now[0])
            if len(calls) == 4:
                (tmp_path / "STOP").touch()
            return replace(sample, observed_at=now[0])

        async def write(self, *args):
            pytest.fail("Geraeteschreiben verboten")

    async def wait(seconds):
        now[0] += timedelta(minutes=10)

    asyncio.run(sammeln(Adapter(), tmp_path, auth, jetzt=lambda: now[0], warten=wait))
    s = json.loads((tmp_path / "status.json").read_text())
    assert s["state"] == "stopped" and s["samples"] == 4
    assert calls[-1] - calls[0] > timedelta(hours=48)
    store = Store(tmp_path)
    rows = [json.loads(r[0]) for r in store.db.execute("SELECT payload FROM cycles")]
    assert all(r["decision"] is None for r in rows)
    assert store.db.execute("SELECT count(*) FROM writes").fetchone()[0] == 0
    store.close()


def test_neustart_setzt_fort_und_wartet_mindestabstand(tmp_path, sample):
    start = datetime.now(UTC)
    auth = cache(tmp_path, start)
    store = Store(tmp_path)
    store.cycle(replace(sample, observed_at=start), None, Result("info", "test"))
    store.close()
    now = [start + timedelta(seconds=30)]
    calls = []

    class Adapter:
        async def read(self):
            calls.append(now[0])
            (tmp_path / "STOP").touch()
            return replace(sample, observed_at=now[0])

    async def wait(s):
        now[0] += timedelta(seconds=s)

    asyncio.run(sammeln(Adapter(), tmp_path, auth, jetzt=lambda: now[0], warten=wait))
    assert (calls[0] - start).total_seconds() >= 300
    s = json.loads((tmp_path / "status.json").read_text())
    assert s["samples"] == 2 and s["settings_write_calls"] == 0


def test_fehlender_zugang_kein_netz_und_keine_geheimnisse(tmp_path):
    adapter = type("Adapter", (), {"read": AsyncMock()})()
    states = []

    async def wait(s):
        states.append(json.loads((tmp_path / "status.json").read_text())["state"])
        (tmp_path / "STOP").touch()

    asyncio.run(sammeln(adapter, tmp_path, tmp_path / "missing", warten=wait))
    adapter.read.assert_not_called()
    assert states == ["authentication-required"]


def test_prozesssperre_verhindert_doppelten_collector(tmp_path):
    with prozesssperre(tmp_path):
        with pytest.raises(BlockingIOError), prozesssperre(tmp_path):
            pytest.fail("Doppelstart zugelassen")
    with prozesssperre(tmp_path):
        pass


def test_lesesitzung_blockiert_login_und_setter_vor_netz(tmp_path, monkeypatch):
    pytest.importorskip("anker_solix_api")
    from aiohttp import ClientSession
    from anker_solix_api.apitypes import API_ENDPOINTS, API_LOGIN

    network = AsyncMock(return_value={"code": 0, "data": {}})
    monkeypatch.setattr("anker_solix_api.session.AnkerSolixClientSession.request", network)

    async def check():
        async with ClientSession() as web:
            session = lesesitzung(tmp_path)("fake@example.invalid", "UNUSED", "DE", web)
            for endpoint in (API_LOGIN, API_ENDPOINTS["set_device_parm"]):
                with pytest.raises(WriteBlocked):
                    await session.request("post", endpoint)
            with pytest.raises(WriteBlocked):
                await session.request("delete", API_ENDPOINTS["scene_info"])
            with pytest.raises(AnmeldungNoetig):
                await session.async_authenticate(restart=True)
            network.assert_not_called()
            await session.request("post", API_ENDPOINTS["scene_info"])
            assert len(list(tmp_path.glob("raw-*-scene_info.json"))) == 1

    asyncio.run(check())


def test_fehlerluecke_backoff_und_folgender_erfolg(tmp_path, sample):
    start = datetime.now(UTC)
    auth = cache(tmp_path, start)
    now = [start]
    calls = []

    class Adapter:
        async def read(self):
            calls.append(now[0])
            if len(calls) == 1:
                raise ConnectionError("secret-must-not-leak")
            (tmp_path / "STOP").touch()
            return replace(sample, observed_at=now[0])

    async def wait(s):
        now[0] += timedelta(seconds=s)

    asyncio.run(sammeln(Adapter(), tmp_path, auth, jetzt=lambda: now[0], warten=wait))
    s = json.loads((tmp_path / "status.json").read_text())
    assert s["samples"] == 1 and s["errors"] == 1
    assert (calls[1] - calls[0]).total_seconds() == 600
    assert "secret-must-not-leak" not in (tmp_path / "events.jsonl").read_text()


def test_abgelaufener_cache(tmp_path):
    p = tmp_path / "auth.json"
    p.write_text('{"token_expires_at":0}')
    with pytest.raises(AnmeldungNoetig):
        cache_pruefen(p)
