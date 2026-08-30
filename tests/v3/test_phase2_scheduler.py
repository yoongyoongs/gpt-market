from datetime import datetime, time

from app.utils.time import SHANGHAI
from scripts.v3_phase2_scheduler import seconds_until_next_run


def test_scheduler_uses_same_day_before_cutoff() -> None:
    now = datetime(2026, 8, 30, 18, 0, tzinfo=SHANGHAI)

    assert seconds_until_next_run(now, time(18, 30)) == 30 * 60


def test_scheduler_rolls_to_next_day_after_cutoff() -> None:
    now = datetime(2026, 8, 30, 19, 0, tzinfo=SHANGHAI)

    assert seconds_until_next_run(now, time(18, 30)) == 23.5 * 60 * 60
