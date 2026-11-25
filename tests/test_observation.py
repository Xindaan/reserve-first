import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from solarbank_controller.config import Config
from solarbank_controller.observation import Coverage, observe


def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "auth.json").write_text(
        json.dumps({"token_expires_at": (datetime.now(UTC) + timedelta(days=10)).timestamp()})
    )
    (root / "target.json").write_text(
        json.dumps({"site_id": "fake-site", "device_id": "fake-bank"})
    )
    return root


def test_coverage_needs_24_hours_and_both_windows(sample):
    noon = datetime(2026, 11, 12, 11, tzinfo=UTC)
    c = Coverage(noon, 24)
    for offset in (0, 12):
        c.sample(replace(sample, observed_at=noon + timedelta(hours=offset)), Config())
    assert not c.status("test", noon)["window_24h_with_day_and_night"]
    c.sample(replace(sample, observed_at=noon + timedelta(hours=24)), Config())
    s = c.status("test", noon)
    assert s["window_24h_with_day_and_night"] and s["max_successful_read_gap_s"] == 43200
    assert not s["physical_verification"]


def test_recorder_runs_to_end_without_executor(tmp_path, monkeypatch, sample):
    pytest.importorskip("anker_solix_api")
    noon = datetime.now(UTC).replace(hour=11, minute=0, second=0, microsecond=0)
    readings = [replace(sample, observed_at=noon + timedelta(hours=h)) for h in (0, 12, 24)]
    read = AsyncMock(side_effect=readings)
    write = AsyncMock(side_effect=AssertionError("Device writing forbidden"))
    monkeypatch.setattr("solarbank_controller.observation.CloudAdapter.read", read)
    monkeypatch.setattr("solarbank_controller.observation.CloudAdapter.write", write)
    monkeypatch.setattr("solarbank_controller.observation.asyncio.sleep", AsyncMock())
    out = tmp_path / "out"
    asyncio.run(observe(out, source(tmp_path)))
    result = json.loads((out / "status.json").read_text())
    assert result["state"] == "finished" and result["samples"] == 3
    assert result["window_24h_with_day_and_night"]
    assert len(list(out.glob("snapshot-*.json"))) == 3
    assert (out / "auth.json").stat().st_mode & 0o777 == 0o600
    write.assert_not_called()


def test_real_session_blocks_setter_before_network(tmp_path, monkeypatch):
    pytest.importorskip("anker_solix_api")
    from anker_solix_api.apitypes import API_ENDPOINTS

    network = AsyncMock(side_effect=AssertionError("Network forbidden"))
    monkeypatch.setattr("anker_solix_api.session.AnkerSolixClientSession.request", network)

    async def try_write(adapter):
        await adapter.api.apisession.request("post", API_ENDPOINTS["set_device_parm"])

    monkeypatch.setattr("solarbank_controller.observation.CloudAdapter.read", try_write)
    out = tmp_path / "out"
    asyncio.run(observe(out, source(tmp_path)))
    status = json.loads((out / "status.json").read_text())
    assert status["state"] == "incomplete" and status["samples"] == 0 and status["errors"] == 1
    network.assert_not_called()


def test_error_and_stop_are_not_success(tmp_path, monkeypatch):
    pytest.importorskip("anker_solix_api")
    out = tmp_path / "out"

    async def fail(adapter):
        (out / "STOP").touch()
        raise ConnectionError("Do not log secret example-token")

    monkeypatch.setattr("solarbank_controller.observation.CloudAdapter.read", fail)
    asyncio.run(observe(out, source(tmp_path)))
    status = json.loads((out / "status.json").read_text())
    assert status["state"] == "incomplete" and status["errors"] == 1
    assert "example-token" not in (out / "events.jsonl").read_text()


def test_expired_token_rejected_before_read(tmp_path, monkeypatch):
    pytest.importorskip("anker_solix_api")
    root = source(tmp_path)
    (root / "auth.json").write_text('{"token_expires_at":0}')
    read = AsyncMock()
    monkeypatch.setattr("solarbank_controller.observation.CloudAdapter.read", read)
    with pytest.raises(ValueError, match="Token"):
        asyncio.run(observe(tmp_path / "out", root))
    read.assert_not_called()
