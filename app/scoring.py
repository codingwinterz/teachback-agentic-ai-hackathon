"""Deterministic mastery math. The LLM judges; code does the arithmetic."""

from app.schemas import GradeResult

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


def next_confidence(prev_confidence: float, attempts_count: int, hedging_score: float) -> float:
    current = 1 - hedging_score
    if attempts_count == 0:
        return round(current, 3)
    return round((1 - ALPHA) * prev_confidence + ALPHA * current, 3)
