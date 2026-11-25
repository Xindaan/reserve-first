import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from solarbank_controller.api_adapter import CloudAdapter, MockAdapter, WriteBlocked
from solarbank_controller.config import Config
from solarbank_controller.models import Command, Decision, State
from solarbank_controller.safety import Backoff, Executor
from solarbank_controller.schedule import fingerprint
from solarbank_controller.storage import Store


def decision():
    return Decision(State.PV_BYPASS, Command(True, 100, 0), "Test", reserve_low=60, reserve_high=65)


def apply(adapter, store, sample, *, execute=True, d=None):
    return asyncio.run(
        Executor(adapter, store, Config(), execute=execute).apply(sample, d or decision())
    )


def test_no_implicit_approval(tmp_path, sample):
    adapter, store = MockAdapter(sample), Store(tmp_path)
    assert apply(adapter, store, sample, execute=False).action == "dry-run"
    assert not adapter.writes


def test_write_confirm_backup_permissions(tmp_path, sample):
    adapter, store = MockAdapter(sample), Store(tmp_path)
    result = apply(adapter, store, sample)
    assert result.writes == 1 and result.action == "written"
    assert store.get("pending_behavior") and not store.get("inflight")
    backup = next(tmp_path.glob("backup-*.json"))
    assert backup.stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700
    assert store.get("expected") == fingerprint(adapter.sample.schedule)


@pytest.mark.parametrize("applied", [False, True])
def test_uncertain_write_never_replayed_after_restart(tmp_path, sample, applied):
    adapter, store = MockAdapter(sample), Store(tmp_path)
    adapter.failure, adapter.apply_before_failure = "write", applied
    assert apply(adapter, store, sample).action == "uncertain"
    store.close()
    store = Store(tmp_path)
    assert apply(adapter, store, sample).action == "blocked"
    assert len(adapter.writes) == 1


def test_app_conflict_not_overwritten(tmp_path, sample):
    adapter, store = MockAdapter(sample), Store(tmp_path)
    apply(adapter, store, sample, execute=False)
    adapter.sample.schedule["ranges"][0]["charge_priority"] = 50
    changed = asyncio.run(adapter.read())
    assert apply(adapter, store, changed).action == "conflict"
    assert not adapter.writes


def test_device_swap_before_write(tmp_path, sample):
    adapter, store = MockAdapter(replace(sample, device_id="wrong")), Store(tmp_path)
    assert apply(adapter, store, sample).action == "conflict"
    assert not adapter.writes


def test_deteriorating_fresh_read_before_write(tmp_path, sample):
    adapter = MockAdapter(replace(sample, soc=44))
    assert apply(adapter, Store(tmp_path), sample).action == "blocked"
    assert not adapter.writes


@pytest.mark.parametrize(
    "mutation", ["night", "low", "invalid", "other_model", "two_banks", "no_owner"]
)
def test_guard_rechecks_policy(tmp_path, sample, mutation):
    if mutation == "night":
        sample = replace(
            sample,
            observed_at=sample.observed_at.replace(hour=0),
            device_at=sample.device_at.replace(hour=0),
        )
    elif mutation == "low":
        sample = replace(sample, soc=44)
    elif mutation == "invalid":
        sample = replace(sample, valid=False)
    elif mutation == "other_model":
        sample = replace(sample, model="A17C1")
    elif mutation == "two_banks":
        sample = replace(sample, bank_count=2)
    else:
        sample = replace(sample, owner=False)
    adapter = MockAdapter(sample)
    assert apply(adapter, Store(tmp_path), sample).writes == 0


def test_budget_reserves_one_protection_write(tmp_path, sample):
    adapter, store = MockAdapter(sample), Store(tmp_path)
    for i in range(5):
        store.write_attempt(sample.observed_at - timedelta(minutes=i + 1), "export")
    assert apply(adapter, store, sample).action == "alarm"
    # Schutz darf bei fuenf Versuchen noch schreiben, falls der Zeitplan unsicher ist.
    adapter.sample.schedule["ranges"][0]["turn_on"] = True
    unsafe = asyncio.run(adapter.read())
    store.put("expected", fingerprint(unsafe.schedule))
    d = Decision(State.NIGHT_SAFE, Command(), "Schutz")
    assert apply(adapter, store, unsafe, d=d).writes == 1
    assert store.count(sample.observed_at.timestamp() - 3600) == 6


def test_backoff_429_persisted(tmp_path, sample):
    store = Store(tmp_path)
    backoff = Backoff(store)
    now = sample.observed_at
    assert [backoff.failed(now) for _ in range(6)] == [300, 600, 1200, 2400, 3600, 3600]
    assert not Backoff(Store(tmp_path)).ready(now)
    assert backoff.ready(now + timedelta(hours=1))
    backoff.success()
    assert backoff.ready(now)


def test_missing_and_bad_behavior_confirmation(tmp_path, sample):
    adapter, store = MockAdapter(sample), Store(tmp_path)
    assert apply(adapter, store, sample).writes == 1
    executor = Executor(adapter, store, Config(), execute=True)
    assert executor.confirm_behavior(sample) == "Einschwingzeit"
    late = replace(sample, observed_at=sample.observed_at + timedelta(minutes=5))
    assert "fehlt" in executor.confirm_behavior(late)
    assert store.get("blocked")


def test_live_writer_cannot_be_enabled(tmp_path, sample):
    adapter = CloudAdapter(None, Config(), "site", "device")
    assert apply(adapter, Store(tmp_path), sample).action == "blocked"
    with pytest.raises(WriteBlocked):
        asyncio.run(adapter.write({}, "hash"))


def test_stop_is_local(tmp_path, sample):
    store, adapter = Store(tmp_path), MockAdapter(sample)
    store.put("stopped", True)
    assert apply(adapter, store, sample).action == "stopped"
    assert not adapter.writes


def test_unchecked_effect_prevents_next_optimization(tmp_path, sample):
    adapter, store = MockAdapter(sample), Store(tmp_path)
    assert apply(adapter, store, sample).writes == 1
    now = sample.observed_at + timedelta(minutes=10)
    adapter.sample = replace(adapter.sample, observed_at=now, device_at=now)
    assert apply(adapter, store, asyncio.run(adapter.read())).action == "deferred"
    assert len(adapter.writes) == 1
