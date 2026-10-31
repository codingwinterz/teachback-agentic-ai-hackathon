"""Deterministic mastery math. The LLM judges; code does the arithmetic."""

from datetime import datetime, timedelta, timezone

from app.schemas import GradeResult

# ---------- spaced repetition (simple SM-2 style) ----------

GOOD_SCORE = 0.6  # an attempt scoring at least this counts as a good recall
MIN_EASE, MAX_EASE = 1.3, 2.8
SQLITE_TIME = "%Y-%m-%d %H:%M:%S"  # same format as SQLite datetime('now'), UTC


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def schedule_review(interval_days: float, ease: float, score: float,
                    now: datetime | None = None) -> tuple[float, float, str]:
    """Good attempt: interval *= ease (ease creeps up). Bad attempt: interval resets to 1 day
    (ease drops). Returns (interval_days, ease, next_review_at as a SQLite UTC timestamp)."""
    if score >= GOOD_SCORE:
        interval, ease = round(interval_days * ease, 2), min(MAX_EASE, round(ease + 0.05, 2))
    else:
        interval, ease = 1.0, max(MIN_EASE, round(ease - 0.2, 2))
    next_at = (now or utcnow()) + timedelta(days=interval)
    return interval, ease, next_at.strftime(SQLITE_TIME)


def is_due(next_review_at: str | None, now: datetime | None = None) -> bool:
    if not next_review_at:
        return False
    return datetime.strptime(next_review_at, SQLITE_TIME) <= (now or utcnow())

ALPHA = 0.5  # weight of the newest attempt in the moving average
HEDGE_DISCOUNT = 0.5  # a fully hedged answer counts for half
MISSING_WEIGHT = 0.5  # an omission costs half as much as a correct claim or a misconception


def attempt_score(grade: GradeResult, hedging_score: float) -> float:
    correct = len(grade.correct_claims)
    total = correct + MISSING_WEIGHT * len(grade.missing_points) + len(grade.misconceptions)
    raw = correct / total if total else 0.0
    return round(raw * (1 - HEDGE_DISCOUNT * hedging_score), 3)


def next_mastery(prev_mastery: float, attempts_count: int, score: float) -> float:
    if attempts_count == 0:
        return score
    return round((1 - ALPHA) * prev_mastery + ALPHA * score, 3)


CHECK_ALPHA = 0.25  # a lesson check answer counts for half as much as a full attempt


def mastery_after_check(prev_mastery: float, correct: bool) -> float:
    """A correct check answer pulls mastery toward 1; a wrong one leaves it unchanged."""
    if not correct:
        return prev_mastery
    return round((1 - CHECK_ALPHA) * prev_mastery + CHECK_ALPHA * 1.0, 3)


def next_confidence(prev_confidence: float, attempts_count: int, hedging_score: float) -> float:
    current = 1 - hedging_score
    if attempts_count == 0:
        return round(current, 3)
    return round((1 - ALPHA) * prev_confidence + ALPHA * current, 3)
