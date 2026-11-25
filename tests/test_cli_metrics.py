import json
from dataclasses import replace
from datetime import timedelta

import pytest

from solarbank_controller.analysis import analyze
from solarbank_controller.cli import main
from solarbank_controller.config import Config
from solarbank_controller.controller import Controller
from solarbank_controller.models import Result
from solarbank_controller.storage import Store


def invoke(args, capsys):
    with pytest.raises(SystemExit) as exc:
        main(args)
    return exc.value.code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("cmd", ["status", "discover", "arm", "stop"])
def test_local_mock_commands(tmp_path, capsys, cmd):
    code, out = invoke(["--data-dir", str(tmp_path), "--adapter", "mock", cmd], capsys)
    assert code == 0
    if cmd == "arm":
        assert out["armed"] is False
        proposal = json.loads(open(out["proposal"]).read())
        assert proposal["before"] and proposal["after"]


def test_live_execute_blocks_before_auth(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("ANKER_EMAIL", raising=False)
    code, out = invoke(["--data-dir", str(tmp_path), "run", "--execute", "--cycles", "1"], capsys)
    assert code == 2 and out["cloud_contact"] is False


def test_dry_run_is_default(tmp_path, capsys):
    code, out = invoke(
        ["--data-dir", str(tmp_path), "--adapter", "mock", "run", "--cycles", "1"], capsys
    )
    assert code == 0 and out["action"] in {"unchanged", "dry-run"}


def test_json_log_redacts_identity_and_handles_nan(tmp_path, sample):
    store = Store(tmp_path)
    sample = replace(sample, device_id="SECRET-SERIAL", site_id="SECRET-SITE", soc=float("nan"))
    d = Controller(Config()).decide(sample)
    store.cycle(sample, d, Result("alarm", "invalid"))
    text = (tmp_path / "events.jsonl").read_text()
    assert "SECRET" not in text and "NaN" not in text
    assert json.loads(text)["sample"]["soc"] is None


def test_metrics_do_not_bridge_stale_gap(tmp_path, sample):
    store = Store(tmp_path)
    c = Controller(Config())
    for m in (0, 5, 120):
        now = sample.observed_at + timedelta(minutes=m)
        s = replace(sample, observed_at=now, device_at=now)
        store.cycle(s, c.decide(s), Result("dry-run", "test"))
    report = analyze(tmp_path / "telemetry.sqlite")
    assert report["availability_percent"] == pytest.approx(100 * 5 / 120)
    assert report["energy_coverage_seconds"] == 300
    assert report["estimated_pv_bypass_dc_wh"] == 0


def test_metrics_include_fault_discharge_instead_of_hiding_it(tmp_path, sample):
    store = Store(tmp_path)
    c = Controller(Config())
    for m in (0, 5):
        now = sample.observed_at.replace(hour=0) + timedelta(minutes=m)
        s = replace(
            sample,
            observed_at=now,
            device_at=now,
            soc=44,
            pv_w=0,
            battery_w=-100,
            output_dc_w=100,
            mode="discharge",
        )
        store.cycle(s, c.decide(s), Result("alarm", "discharge"))
    report = analyze(tmp_path / "telemetry.sqlite")
    assert report["estimated_night_discharge_wh"] == pytest.approx(100 / 12)
    assert report["estimated_discharge_below45_wh"] == pytest.approx(100 / 12)


@pytest.mark.parametrize("error", [ConnectionError, TimeoutError])
def test_cloud_failure_at_44_soc_is_reported(tmp_path, capsys, monkeypatch, sample, error):
    from solarbank_controller.api_adapter import MockAdapter

    async def fail(self):
        raise error("nicht protokollieren: SECRET")

    monkeypatch.setattr(MockAdapter, "read", fail)
    code, out = invoke(
        ["--data-dir", str(tmp_path), "--adapter", "mock", "run", "--dry-run", "--cycles", "1"],
        capsys,
    )
    assert code == 2 and out["state"] == "FAULT_SAFE" and out["retry_after_s"] == 300
    assert "SECRET" not in json.dumps(out)
    report = analyze(tmp_path / "mock" / "telemetry.sqlite")
    assert report["communication_errors"] == 1 and report["writes"] == 0


def test_outage_time_is_in_availability_denominator(tmp_path, sample):
    store = Store(tmp_path)
    c = Controller(Config())
    for m in (0, 5, 10, 15):
        now = sample.observed_at + timedelta(minutes=m)
        if m == 10:
            store.cycle(
                None,
                None,
                Result("communication-error", "timeout"),
                communication_error=True,
                now=now,
            )
        else:
            s = replace(sample, observed_at=now, device_at=now)
            store.cycle(s, c.decide(s), Result("dry-run", "test"))
    r = analyze(tmp_path / "telemetry.sqlite")
    assert r["availability_percent"] == pytest.approx(100 / 3)


def test_http_429_uses_backoff_without_writes(tmp_path, capsys, monkeypatch):
    aiohttp = pytest.importorskip("aiohttp")
    from solarbank_controller.api_adapter import MockAdapter

    async def fail(self):
        raise aiohttp.ClientResponseError(None, (), status=429)

    monkeypatch.setattr(MockAdapter, "read", fail)
    code, out = invoke(
        ["--data-dir", str(tmp_path), "--adapter", "mock", "run", "--cycles", "1"], capsys
    )
    assert code == 2 and out["retry_after_s"] == 300
    assert out["error_type"] == "ClientResponseError"
    assert analyze(tmp_path / "mock" / "telemetry.sqlite")["writes"] == 0


@pytest.mark.parametrize("confirm", [False, True])
def test_restore_requires_explicit_hash(tmp_path, capsys, confirm):
    from pathlib import Path

    code, out = invoke(["--data-dir", str(tmp_path), "--adapter", "mock", "arm"], capsys)
    assert code == 0
    backup = tmp_path / "mock" / out["backup"]
    payload = json.loads(Path(backup).read_text())
    args = ["--data-dir", str(tmp_path), "--adapter", "mock", "restore", str(backup), "--execute"]
    if confirm:
        args += ["--confirm", payload["sha256"]]
    code, out = invoke(args, capsys)
    assert code == (0 if confirm else 2)
    assert ("restored" in out) is confirm
