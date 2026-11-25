from dataclasses import replace
from datetime import timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from solarbank_controller.config import Config
from solarbank_controller.controller import Controller
from solarbank_controller.models import Command
from solarbank_controller.schedule import propose, validate_command, validate_schedule


@given(soc=st.floats(min_value=45, max_value=100), watts=st.integers(100, 800))
@settings(max_examples=50)
def test_no_write_without_explicit_opt_in(sample, soc, watts):
    import asyncio
    import tempfile
    from pathlib import Path

    from solarbank_controller.api_adapter import MockAdapter
    from solarbank_controller.models import Decision, State
    from solarbank_controller.safety import Executor
    from solarbank_controller.storage import Store

    s = replace(sample, soc=soc)
    adapter = MockAdapter(s)
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory))
        try:
            executor = Executor(adapter, store, Config())
            d = Decision(State.PV_BYPASS, Command(True, watts, 0), "property")
            result = asyncio.run(executor.apply(s, d))
            assert result.writes == 0 and not adapter.writes
        finally:
            store.close()


@given(
    soc=st.floats(min_value=0, max_value=44.99),
    pv=st.floats(min_value=0, max_value=800),
    hour=st.integers(0, 23),
)
@settings(max_examples=150)
def test_no_low_soc_export(sample, soc, pv, hour):
    now = sample.observed_at.replace(hour=hour)
    d = Controller(Config()).decide(
        replace(sample, observed_at=now, device_at=now, soc=soc, pv_w=pv, battery_w=pv)
    )
    assert d.command is None or not d.command.allow_export


@given(soc=st.floats(min_value=0, max_value=100), hour=st.sampled_from([0, 1, 2, 3, 22, 23]))
def test_no_night_export(sample, soc, hour):
    now = sample.observed_at.replace(hour=hour)
    d = Controller(Config()).decide(replace(sample, observed_at=now, device_at=now, soc=soc))
    assert d.command is None or not d.command.allow_export


@given(age=st.integers(min_value=301, max_value=1000000))
def test_invalid_data_never_optimize(sample, age):
    s = replace(sample, device_at=sample.device_at - timedelta(seconds=age))
    assert Controller(Config()).decide(s).command is None


@given(watts=st.integers(min_value=-10000, max_value=10000))
def test_no_unknown_api_values(watts):
    try:
        validate_command(Command(True, watts, 0))
    except ValueError:
        assert not 100 <= watts <= 800
    else:
        assert 100 <= watts <= 800


@given(minute=st.integers(0, 59), watts=st.integers(100, 800))
def test_generated_plans_cover_day_without_gaps(sample, minute, watts):
    now = sample.observed_at.replace(minute=minute)
    plan = propose(sample.schedule, Command(True, watts, 0), now, Config())
    validate_schedule(plan, winter=True)
    assert len(plan["ranges"]) >= 2
    assert all(s["priority_discharge_switch"] == 0 for s in plan["ranges"])
