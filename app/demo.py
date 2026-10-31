"""Demo tools: load a sample learner, or reset all progress. No LLM calls.

Sample mastery is not hand-typed: each sample concept gets a few sample attempts, and mastery and
confidence come from replaying them through the real scoring functions, so the dashboard and the
attempt history always agree. Every sample row has is_sample = 1.
"""

import json
import sqlite3

from app import scoring
from app.schemas import GradeResult, Misconception


class RealDataPresent(ValueError):
    pass


# concept -> attempts as (correct, missing, misconception labels, hedging), oldest first,
# plus the review schedule (interval days, ease, days until next review; negative = overdue)
SAMPLE = {
    "supervised_learning": ([(3, 1, [], 0.3), (4, 0, [], 0.2), (4, 1, [], 0.1)], (6.0, 2.6, 3)),
    "train_test_split": ([(2, 2, [], 0.4), (3, 1, [], 0.3)], (2.5, 2.5, -1)),
    "loss_function": ([(2, 1, [], 0.5), (3, 1, [], 0.4)], (2.5, 2.5, -2)),
    "gradient_descent": ([(1, 2, ["thinks the gradient points downhill"], 0.6), (2, 2, [], 0.5)], (1.0, 2.3, 0)),
    "linear_regression": ([(2, 2, [], 0.3)], (2.5, 2.5, 1)),
    "overfitting": ([(1, 3, ["thinks more data always fixes overfitting"], 0.7),
                     (2, 2, ["thinks more data always fixes overfitting"], 0.6),
                     (2, 1, ["thinks more data always fixes overfitting"], 0.5)], (1.0, 2.1, -1)),
    "bias_variance": ([(1, 3, ["confuses bias with variance"], 0.7),
                       (1, 2, ["confuses bias with variance"], 0.6)], (1.0, 2.2, 0)),
    "decision_trees": ([(2, 1, ["thinks a perfectly fit tree is excellent"], 0.4)], (1.0, 2.3, 2)),
}


def _attempt_payload(correct: int, missing: int, labels: list[str]) -> GradeResult:
    return GradeResult(
        correct_claims=[f"(sample) correct point {i + 1}" for i in range(correct)],
        missing_points=[f"(sample) missing point {i + 1}" for i in range(min(missing, 3))],
        misconceptions=[Misconception(label=l, evidence="(sample answer)") for l in labels],
    )


def has_real_data(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT COUNT(*) FROM attempts WHERE is_sample = 0").fetchone()[0] > 0


def sample_loaded(conn: sqlite3.Connection) -> bool:
    return any(conn.execute(f"SELECT 1 FROM {t} WHERE is_sample = 1 LIMIT 1").fetchone()
               for t in ("learner_state", "misconceptions", "attempts"))


def _clear_sample(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM lessons WHERE attempt_id IN (SELECT id FROM attempts WHERE is_sample = 1)")
    conn.execute("DELETE FROM attempts WHERE is_sample = 1")
    conn.execute("DELETE FROM misconceptions WHERE is_sample = 1")
    conn.execute("""UPDATE learner_state SET mastery = 0, confidence = 0, interval_days = 1, ease = 2.5,
                    next_review_at = NULL, attempts_count = 0, is_sample = 0 WHERE is_sample = 1""")


def load_sample(conn: sqlite3.Connection) -> dict:
    if has_real_data(conn):
        raise RealDataPresent("Real practice data exists. Use Reset progress first, so it is not mixed with sample data.")
    _clear_sample(conn)
    n_attempts = 0
    for concept_id, (attempts, (interval, ease, review_in_days)) in SAMPLE.items():
        mastery = confidence = 0.0
        labels_seen: dict[str, int] = {}
        for i, (correct, missing, labels, hedge) in enumerate(attempts):
            grade = _attempt_payload(correct, missing, labels)
            score = scoring.attempt_score(grade, hedge)
            mastery = scoring.next_mastery(mastery, i, score)
            confidence = scoring.next_confidence(confidence, i, hedge)
            days_ago = 2 * (len(attempts) - i)
            conn.execute(
                """INSERT INTO attempts (concept_id, raw_transcript, clean_claims_json, hedging_score,
                     correct_json, missing_json, misconceptions_json, created_at, is_sample)
                   VALUES (?, '(sample attempt)', ?, ?, ?, ?, ?, datetime('now', ?), 1)""",
                (concept_id, json.dumps(grade.correct_claims), hedge, json.dumps(grade.correct_claims),
                 json.dumps(grade.missing_points), json.dumps([m.model_dump() for m in grade.misconceptions]),
                 f"-{days_ago} days"),
            )
            n_attempts += 1
            for label in labels:
                labels_seen[label] = labels_seen.get(label, 0) + 1
        conn.execute(
            """UPDATE learner_state SET mastery = ?, confidence = ?, interval_days = ?, ease = ?,
                 next_review_at = datetime('now', ?), attempts_count = ?, is_sample = 1 WHERE concept_id = ?""",
            (mastery, confidence, interval, ease, f"{review_in_days:+d} days", len(attempts), concept_id),
        )
        for label, count in labels_seen.items():
            conn.execute(
                """INSERT INTO misconceptions (concept_id, label, count, last_seen_at, is_sample)
                   VALUES (?, ?, ?, datetime('now', '-1 days'), 1)
                   ON CONFLICT(concept_id, label) DO UPDATE SET count = excluded.count, is_sample = 1""",
                (concept_id, label, count),
            )
    conn.commit()
    return {"concepts": len(SAMPLE), "attempts": n_attempts}


def reset_progress(conn: sqlite3.Connection) -> None:
    """Delete all learner and sample data (attempts, lessons, sessions, misconceptions, log). Never concepts."""
    for table in ("lessons", "session_steps", "sessions", "attempts", "misconceptions", "agent_log"):
        conn.execute(f"DELETE FROM {table}")
    conn.execute("""UPDATE learner_state SET mastery = 0, confidence = 0, interval_days = 1, ease = 2.5,
                    next_review_at = NULL, attempts_count = 0, is_sample = 0""")
    conn.commit()
