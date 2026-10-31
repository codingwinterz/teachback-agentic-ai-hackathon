"""Attempt history for one concept, read from the attempts table (no schema change).

mastery_after is recomputed by replaying the stored attempts through the same scoring functions
as the update step. Lesson-check bonuses are not attempts, so they are not part of this replay.
"""

import json
import sqlite3

from app import scoring
from app.schemas import GradeResult, Misconception


def concept_history(conn: sqlite3.Connection, concept_id: str) -> list[dict]:
    if conn.execute("SELECT 1 FROM concepts WHERE id = ?", (concept_id,)).fetchone() is None:
        raise KeyError(f"Unknown concept: {concept_id}")
    rows = conn.execute(
        """SELECT id, created_at, hedging_score, correct_json, missing_json, misconceptions_json, is_sample
           FROM attempts WHERE concept_id = ? ORDER BY created_at, id""",
        (concept_id,),
    ).fetchall()
    mastery = 0.0
    out = []
    for i, r in enumerate(rows):
        misconceptions = [Misconception(**m) for m in json.loads(r["misconceptions_json"])]
        grade = GradeResult(correct_claims=json.loads(r["correct_json"]),
                            missing_points=json.loads(r["missing_json"])[:3],
                            misconceptions=misconceptions)
        score = scoring.attempt_score(grade, r["hedging_score"])
        mastery = scoring.next_mastery(mastery, i, score)
        out.append({"attempt_id": r["id"], "created_at": r["created_at"], "attempt_score": score,
                    "mastery_after": mastery, "misconceptions": [m.label for m in misconceptions],
                    "is_sample": bool(r["is_sample"])})
    return out
