"""
scoring.py - the ONE place where points, rounding, status and streaks are defined.

Everything here is a pure function (no database, no Telegram), so it is
deterministic and easy to test.

STUDY-HOUR ROUNDING POLICY (explicit, configurable per session)
----------------------------------------------------------------
rounding_policy = "tolerance_up" (default)
    Complete hours always count. A leftover part of an hour counts as one more
    hour if it is within `tolerance_minutes` of the next full hour.
    With tolerance = 10:  7h49m -> 7 hours,  7h50m -> 8 hours,  8h09m -> 8 hours,
    8h50m -> 9 hours.  The SAME tolerance decides the full-green threshold:
    7h50m (470 min) is "full green" for an 8-hour threshold.
rounding_policy = "floor"
    Only complete hours count and the tolerance is ignored everywhere
    (7h59m -> 7 hours, not full green).
"""
from dataclasses import dataclass, fields
from datetime import date, timedelta

# ---- visual / logical day statuses (internal values, never inferred from colour) ----
GREEN, HALF, RED, EMPTY, PENDING, HEALTH = "GREEN", "HALF", "RED", "EMPTY", "PENDING", "HEALTH"
ALL_STATUSES = (GREEN, HALF, RED, EMPTY, PENDING, HEALTH)
OVERRIDE_STATUSES = (GREEN, HALF, RED, HEALTH, EMPTY)  # what an admin may force

ROUND_FLOOR = "floor"
ROUND_TOLERANCE = "tolerance_up"

# sanity limits for user input
MAX_MINUTES_PER_FIELD = 24 * 60
MAX_EXERCISE_COUNT = 10
MAX_QUESTIONS = 1000


@dataclass(frozen=True)
class Rules:
    full_green_minutes: int = 480
    tolerance_minutes: int = 10
    rounding_policy: str = ROUND_TOLERANCE
    points_per_hour: int = 1
    max_exercise_points: int = 2
    subject_min_questions: int = 15
    subject_point_value: int = 1
    penalty_wasted: int = 5
    penalty_partial: int = 2
    penalty_health: int = 3
    apply_partial_penalty: bool = True
    health_penalty_confirmed: bool = False

    @classmethod
    def from_row(cls, row) -> "Rules":
        data = {f.name: row[f.name] for f in fields(cls)}
        data["apply_partial_penalty"] = bool(data["apply_partial_penalty"])
        data["health_penalty_confirmed"] = bool(data["health_penalty_confirmed"])
        return cls(**data)


@dataclass(frozen=True)
class DailyPoints:
    raw_minutes: int
    hours_credited: int
    study_points: int
    exercise_points: int
    subject_points: int
    subjects_met: tuple  # (physics, chemistry, biology) booleans

    @property
    def gross(self) -> int:
        return self.study_points + self.exercise_points + self.subject_points

    def breakdown(self) -> str:
        h, m = divmod(self.raw_minutes, 60)
        subj = ", ".join(n for n, ok in zip(("Physics", "Chemistry", "Biology"), self.subjects_met) if ok) or "none"
        return (
            f"Recorded study: {h}h {m:02d}m\n"
            f"Study-hour points: {self.study_points} ({self.hours_credited} credited hours)\n"
            f"Exercise points: {self.exercise_points}\n"
            f"Subject points: {self.subject_points} ({subj})\n"
            f"Gross points: {self.gross}"
        )


def effective_tolerance(rules: Rules) -> int:
    return rules.tolerance_minutes if rules.rounding_policy == ROUND_TOLERANCE else 0


def credited_hours(minutes: int, rules: Rules) -> int:
    """Whole study hours credited for `minutes` under the configured policy."""
    if minutes < 0:
        raise ValueError("minutes cannot be negative")
    hours, rest = divmod(minutes, 60)
    tol = effective_tolerance(rules)
    if tol > 0 and rest >= 60 - tol:
        hours += 1
    return hours


def is_full_green(minutes: int, rules: Rules) -> bool:
    return minutes + effective_tolerance(rules) >= rules.full_green_minutes


def is_partial(minutes: int, rules: Rules) -> bool:
    return minutes > 0 and not is_full_green(minutes, rules)


def exercise_points(count: int, rules: Rules) -> int:
    if count < 0:
        raise ValueError("exercise count cannot be negative")
    return min(count, rules.max_exercise_points)


def subject_points(physics: int, chemistry: int, biology: int, rules: Rules):
    if min(physics, chemistry, biology) < 0:
        raise ValueError("question counts cannot be negative")
    met = tuple(q >= rules.subject_min_questions for q in (physics, chemistry, biology))
    return sum(met) * rules.subject_point_value, met


def compute_daily_points(total_minutes, exercise_count, physics_q, chemistry_q, biology_q, rules: Rules) -> DailyPoints:
    hours = credited_hours(total_minutes, rules)
    sp, met = subject_points(physics_q, chemistry_q, biology_q, rules)
    return DailyPoints(
        raw_minutes=total_minutes,
        hours_credited=hours,
        study_points=hours * rules.points_per_hour,
        exercise_points=exercise_points(exercise_count, rules),
        subject_points=sp,
        subjects_met=met,
    )


def day_status(report_status, total_minutes, rules: Rules, override=None) -> str:
    """Visual status of a day. Independent from points."""
    if override:
        return override
    if report_status is None:
        return EMPTY
    if report_status == "approved":
        if total_minutes <= 0:
            return RED
        return GREEN if is_full_green(total_minutes, rules) else HALF
    if report_status in ("pending", "correction_requested"):
        return PENDING
    return EMPTY  # draft / rejected


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def compute_streaks(statuses: dict, start: date, end: date, today: date):
    """
    Streak policy: a streak is a run of consecutive GREEN days (approved, full-green).
    * Future days are ignored.
    * Today never breaks the *current* streak while it is still EMPTY or PENDING.
    * Any other non-GREEN past day (HALF, RED, HEALTH, EMPTY, PENDING) ends the run.
    Returns (current, longest).
    """
    last = min(end, today)
    longest = run = 0
    for d in daterange(start, last):
        if statuses.get(d, EMPTY) == GREEN:
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    d = last
    if d == today and statuses.get(d, EMPTY) in (EMPTY, PENDING):
        d -= timedelta(days=1)
    current = 0
    while d >= start and statuses.get(d, EMPTY) == GREEN:
        current += 1
        d -= timedelta(days=1)
    return current, longest
