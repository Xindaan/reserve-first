from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from hypothesis import HealthCheck, settings

from solarbank_controller.models import Sample
from solarbank_controller.schedule import BERLIN, active_command, mock_schedule

# Die Property-Tests ersetzen ausschliesslich die unveraenderliche Sample-Fixture.
settings.register_profile(
    "project", derandomize=True, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
settings.load_profile("project")


@pytest.fixture
def sample():
    now = datetime(2026, 11, 7, 12, tzinfo=BERLIN)
    plan = mock_schedule()
    return Sample(
        now,
        now,
        70,
        100,
        100,
        0,
        temperature_c=10,
        battery_quality="simulated",
        mode="charge",
        owner=True,
        firmware="SIMULATED",
        discharge_priority_known=True,
        schedule=plan,
        active=active_command(plan, now),
    )


def stabilized(controller, sample, minutes=20):
    decision = None
    for m in range(0, minutes + 1, 5):
        now = sample.observed_at + timedelta(minutes=m)
        decision = controller.decide(replace(sample, observed_at=now, device_at=now))
    return decision
