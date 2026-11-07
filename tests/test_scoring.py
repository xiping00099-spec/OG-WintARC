from datetime import date

import pytest

import scoring
from scoring import Rules, compute_daily_points, compute_streaks, day_status

R = Rules()  # 8h threshold, 10 min tolerance, tolerance_up rounding
D = date


@pytest.mark.parametrize("minutes,hours", [(0, 0), (49, 0), (50, 1), (59, 1), (60, 1), (109, 1), (110, 2),
                                           (469, 7), (470, 8), (480, 8), (489, 8), (529, 8), (530, 9)])
def test_study_hour_rounding_with_tolerance(minutes, hours):
    assert scoring.credited_hours(minutes, R) == hours


def test_floor_policy_ignores_tolerance_everywhere():
    f = Rules(rounding_policy="floor")
    assert scoring.credited_hours(479, f) == 7
    assert not scoring.is_full_green(479, f)
    assert scoring.is_full_green(480, f)


def test_eight_hour_threshold_uses_same_tolerance():
    assert not scoring.is_full_green(469, R)
    assert scoring.is_full_green(470, R)  # 7h50m counts as 8h, and also earns 8 hour-points
    assert scoring.credited_hours(470, R) == 8


def test_partial_day_status():
    assert scoring.is_partial(300, R) and not scoring.is_partial(0, R) and not scoring.is_partial(480, R)
    assert day_status("approved", 300, R) == scoring.HALF
    assert day_status("approved", 480, R) == scoring.GREEN
    assert day_status("approved", 0, R) == scoring.RED


def test_points_breakdown_and_exercise_cap():
    p = compute_daily_points(480, 5, 15, 15, 15, R)
    assert (p.study_points, p.exercise_points, p.subject_points, p.gross) == (8, 2, 3, 13)


def test_subject_points_threshold():
    p = compute_daily_points(0, 0, 14, 15, 30, R)
    assert p.subject_points == 2 and p.subjects_met == (False, True, True)


def test_negative_inputs_rejected():
    with pytest.raises(ValueError):
        compute_daily_points(-1, 0, 0, 0, 0, R)
    with pytest.raises(ValueError):
        compute_daily_points(10, -1, 0, 0, 0, R)
    with pytest.raises(ValueError):
        compute_daily_points(10, 0, -5, 0, 0, R)


def test_pending_and_other_statuses():
    assert day_status("pending", 480, R) == scoring.PENDING
    assert day_status("correction_requested", 480, R) == scoring.PENDING
    assert day_status("rejected", 480, R) == scoring.EMPTY
    assert day_status(None, 0, R) == scoring.EMPTY
    assert day_status("approved", 480, R, override=scoring.HEALTH) == scoring.HEALTH


G, H, E, P = scoring.GREEN, scoring.HALF, scoring.EMPTY, scoring.PENDING


def seq(*sts):
    return {D(2026, 11, 1 + i): s for i, s in enumerate(sts)}


def test_current_and_longest_streak():
    s = seq(G, G, H, G, G, G)
    assert compute_streaks(s, D(2026, 11, 1), D(2026, 11, 30), D(2026, 11, 6)) == (3, 3)


def test_today_unreported_does_not_break_streak():
    s = seq(G, G, G, E)
    assert compute_streaks(s, D(2026, 11, 1), D(2026, 11, 30), D(2026, 11, 4)) == (3, 3)
    assert compute_streaks(seq(G, G, G, P), D(2026, 11, 1), D(2026, 11, 30), D(2026, 11, 4))[0] == 3


def test_yesterday_missing_breaks_streak():
    s = seq(G, G, E, E)
    assert compute_streaks(s, D(2026, 11, 1), D(2026, 11, 30), D(2026, 11, 4)) == (0, 2)


def test_future_days_are_ignored():
    s = seq(G, G)
    assert compute_streaks(s, D(2026, 11, 1), D(2026, 11, 30), D(2026, 11, 2)) == (2, 2)
    assert compute_streaks(s, D(2026, 11, 1), D(2026, 11, 30), D(2026, 10, 1)) == (0, 0)


def test_health_day_ends_streak_by_policy():
    s = seq(G, G, scoring.HEALTH, G)
    assert compute_streaks(s, D(2026, 11, 1), D(2026, 11, 30), D(2026, 11, 5)) == (1, 2)
