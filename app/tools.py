"""Tools the agent calls. Each call is written to agent_log."""

import json
import sqlite3
import time
from typing import Callable

from app.schemas import Concept, LearnerState, Lesson, Misconception


class AgentLogger:
    def __init__(
        self, conn: sqlite3.Connection, run_id: str,
        on_event: Callable[[dict], None] | None = None,
    ):
        self.conn = conn
        self.run_id = run_id
        self.on_event = on_event or (lambda event: None)  # live trace for the UI
        self.step_models: dict[str, str] = {}

    def log(
        self, step: str, tool_name: str, args: dict, result,
        model: str | None = None, elapsed_s: float | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO agent_log (run_id, step, tool_name, args_json, result_json, model, elapsed_s)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (self.run_id, step, tool_name, json.dumps(args, default=str),
             json.dumps(result, default=str), model, elapsed_s),
        )
        self.conn.commit()
        if model:
            self.step_models[step] = model
        self.on_event({"type": "tool", "step": step, "tool_name": tool_name,
                       "model": model, "elapsed_s": elapsed_s})

    def step_start(self, step: str) -> None:
        self.on_event({"type": "step_start", "step": step})

    def step_skipped(self, step: str) -> None:
        self.on_event({"type": "step_skipped", "step": step})

    def step_done(self, step: str) -> None:
        self.on_event({"type": "step_done", "step": step,
                       "model": self.step_models.get(step, "code"),
                       "elapsed_s": self.step_timings().get(step, 0.0)})

    def step_timings(self) -> dict:
        """Seconds per step for this run, summed over its log entries."""
        rows = self.conn.execute(
            """SELECT step, ROUND(SUM(COALESCE(elapsed_s, 0)), 3) AS s FROM agent_log
               WHERE run_id = ? GROUP BY step ORDER BY MIN(id)""",
            (self.run_id,),
        ).fetchall()
        return {r["step"]: r["s"] for r in rows}

    def entries(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM agent_log WHERE run_id = ? ORDER BY id", (self.run_id,)
        ).fetchall()
        return [
            {**dict(r), "args": json.loads(r["args_json"]), "result": json.loads(r["result_json"])}
            for r in rows
        ]


def _since(start: float) -> float:
    return round(time.perf_counter() - start, 4)


def get_concept(conn: sqlite3.Connection, log: AgentLogger, concept_id: str) -> Concept:
    start = time.perf_counter()
    row = conn.execute("SELECT * FROM concepts WHERE id = ?", (concept_id,)).fetchone()
    if row is None:
        raise KeyError(f"Unknown concept: {concept_id}")
    concept = Concept(
        id=row["id"], name=row["name"], subject=row["subject"],
        reference_notes=row["reference_notes"],
        prerequisites=json.loads(row["prerequisites_json"]),
    )
    log.log("context", "get_concept", {"concept_id": concept_id}, {"name": concept.name},
            elapsed_s=_since(start))
    return concept


def get_learner_state(
    conn: sqlite3.Connection, log: AgentLogger, concept_id: str, step: str = "context"
) -> LearnerState:
    start = time.perf_counter()
    row = conn.execute("SELECT * FROM learner_state WHERE concept_id = ?", (concept_id,)).fetchone()
    state = LearnerState(**dict(row)) if row else LearnerState(concept_id=concept_id)
    log.log(step, "get_learner_state", {"concept_id": concept_id}, state.model_dump(),
            elapsed_s=_since(start))
    return state


def get_open_misconceptions(conn: sqlite3.Connection, concept_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT label FROM misconceptions WHERE concept_id = ? AND resolved = 0 ORDER BY count DESC",
        (concept_id,),
    ).fetchall()
    return [r["label"] for r in rows]


def get_dependent_concepts(conn: sqlite3.Connection, concept_id: str) -> list[str]:
    rows = conn.execute("SELECT id, prerequisites_json FROM concepts").fetchall()
    return [r["id"] for r in rows if concept_id in json.loads(r["prerequisites_json"])]


def save_lesson(
    conn: sqlite3.Connection, log: AgentLogger, attempt_id: int, concept_id: str,
    label: str | None, lesson: Lesson,
) -> int:
    start = time.perf_counter()
    cur = conn.execute(
        "INSERT INTO lessons (attempt_id, concept_id, misconception_label, lesson_json) VALUES (?, ?, ?, ?)",
        (attempt_id, concept_id, label, lesson.model_dump_json()),
    )
    conn.commit()
    log.log("teach", "save_lesson", {"attempt_id": attempt_id, "label": label},
            {"lesson_id": cur.lastrowid}, elapsed_s=_since(start))
    return cur.lastrowid


def get_lesson(conn: sqlite3.Connection, lesson_id: int) -> tuple[sqlite3.Row, Lesson]:
    row = conn.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
    if row is None:
        raise KeyError(f"Unknown lesson: {lesson_id}")
    return row, Lesson.model_validate_json(row["lesson_json"])


def apply_check_result(
    conn: sqlite3.Connection, log: AgentLogger, lesson_id: int, concept_id: str,
    label: str | None, answer: str, correct: bool, mastery: float,
) -> LearnerState:
    """Record the check answer; if correct, raise mastery and resolve the misconception."""
    start = time.perf_counter()
    conn.execute("UPDATE lessons SET answer = ?, correct = ? WHERE id = ?",
                 (answer, int(correct), lesson_id))
    if correct:
        conn.execute("UPDATE learner_state SET mastery = ? WHERE concept_id = ?", (mastery, concept_id))
        if label:
            conn.execute(
                "UPDATE misconceptions SET resolved = 1 WHERE concept_id = ? AND label = ?",
                (concept_id, label),
            )
    conn.commit()
    row = conn.execute("SELECT * FROM learner_state WHERE concept_id = ?", (concept_id,)).fetchone()
    state = LearnerState(**dict(row))
    log.log("check", "apply_check_result",
            {"lesson_id": lesson_id, "correct": correct, "label": label},
            state.model_dump(), elapsed_s=_since(start))
    return state


def update_learner_state(
    conn: sqlite3.Connection,
    log: AgentLogger,
    concept_id: str,
    mastery: float,
    confidence: float,
    misconceptions: list[Misconception],
) -> LearnerState:
    start = time.perf_counter()
    conn.execute(
        """UPDATE learner_state SET mastery = ?, confidence = ?, attempts_count = attempts_count + 1
           WHERE concept_id = ?""",
        (mastery, confidence, concept_id),
    )
    for m in misconceptions:
        conn.execute(
            """INSERT INTO misconceptions (concept_id, label) VALUES (?, ?)
               ON CONFLICT(concept_id, label) DO UPDATE SET
                 count = count + 1, last_seen_at = datetime('now'), resolved = 0""",
            (concept_id, m.label),
        )
    conn.commit()
    row = conn.execute("SELECT * FROM learner_state WHERE concept_id = ?", (concept_id,)).fetchone()
    state = LearnerState(**dict(row))
    log.log(
        "update", "update_learner_state",
        {"concept_id": concept_id, "mastery": mastery, "confidence": confidence,
         "misconceptions": [m.label for m in misconceptions]},
        state.model_dump(),
        elapsed_s=_since(start),
    )
    return state
