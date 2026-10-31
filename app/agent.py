"""The core loop: clean -> grade -> update -> decide."""

import json
import sqlite3
import time
import uuid
from typing import Protocol

from app import hedging, prompts, scoring, tools
from app.llm import LLM, LLMResult
from app.schemas import (
    AttemptResult, CleanResult, Decision, DecisionContext, GradeResult,
)


class Decider(Protocol):
    """Seam for step 4. Today: one structured call. Later: a tool-use loop
    (list_due_reviews, pick_next_concept, generate_followup_question) that
    implements the same interface and returns a Decision."""

    def decide(self, ctx: DecisionContext, log: tools.AgentLogger) -> Decision: ...


class SingleShotDecider:
    def __init__(self, llm: LLM):
        self.llm = llm

    def decide(self, ctx: DecisionContext, log: tools.AgentLogger) -> Decision:
        user = prompts.DECIDE_USER.format(
            concept_id=ctx.concept.id,
            concept_name=ctx.concept.name,
            score=ctx.attempt_score,
            hedging=ctx.hedging_score,
            state=ctx.state.model_dump_json(),
            grade=ctx.grade.model_dump_json(),
            prereqs=json.dumps([s.model_dump() for s in ctx.prerequisite_states]),
            next_ids=ctx.next_concept_ids,
        )
        out = self.llm.structured(prompts.DECIDE_SYSTEM, user, Decision, thinking="low")
        _log_llm(log, "decide", "llm.decide", {"concept_id": ctx.concept.id}, out)
        return out.parsed


def _log_llm(log: tools.AgentLogger, step: str, tool_name: str, args: dict, out: LLMResult) -> None:
    log.log(
        step, tool_name, args,
        {"output": out.parsed.model_dump(), "raw_text": out.raw_text, "timing": out.timing()},
        model=out.model, elapsed_s=out.elapsed_s,
    )


def run_attempt(
    conn: sqlite3.Connection,
    llm: LLM,
    concept_id: str,
    transcript: str,
    decider: Decider | None = None,
) -> AttemptResult:
    run_start = time.perf_counter()
    decider = decider or SingleShotDecider(llm)
    log = tools.AgentLogger(conn, uuid.uuid4().hex[:8])
    concept = tools.get_concept(conn, log, concept_id)

    # 1. CLEAN
    out = llm.structured(
        prompts.CLEAN_SYSTEM,
        prompts.CLEAN_USER.format(concept_name=concept.name, transcript=transcript),
        CleanResult,
        thinking="low",
    )
    clean: CleanResult = out.parsed
    _log_llm(log, "clean", "llm.clean", {"chars": len(transcript)}, out)

    start = time.perf_counter()
    counts = hedging.code_hedging(transcript)
    hedge = hedging.blend(clean.hedging_score, counts.score)
    hedge_info = {"llm": clean.hedging_score, "code": counts.model_dump(), "blended": hedge}
    log.log("clean", "hedging.blend", {"llm_score": clean.hedging_score}, hedge_info,
            elapsed_s=round(time.perf_counter() - start, 4))

    # 2. GRADE
    known = tools.get_open_misconceptions(conn, concept_id)
    out = llm.structured(
        prompts.GRADE_SYSTEM,
        prompts.GRADE_USER.format(
            concept_name=concept.name,
            reference_notes=concept.reference_notes,
            known_labels=json.dumps(known),
            claims="\n".join(f"- {c}" for c in clean.claims),
        ),
        GradeResult,
    )
    grade: GradeResult = out.parsed
    _log_llm(log, "grade", "llm.grade", {"claims": len(clean.claims), "known_labels": known}, out)

    # 3. UPDATE
    prior = tools.get_learner_state(conn, log, concept_id, step="update")
    score = scoring.attempt_score(grade, hedge)
    state = tools.update_learner_state(
        conn, log, concept_id,
        mastery=scoring.next_mastery(prior.mastery, prior.attempts_count, score),
        confidence=scoring.next_confidence(prior.confidence, prior.attempts_count, hedge),
        misconceptions=grade.misconceptions,
    )

    # 4. DECIDE
    ctx = DecisionContext(
        concept=concept,
        grade=grade,
        hedging_score=hedge,
        attempt_score=score,
        state=state,
        prerequisite_states=[
            tools.get_learner_state(conn, log, p, step="decide") for p in concept.prerequisites
        ],
        next_concept_ids=tools.get_dependent_concepts(conn, concept_id),
    )
    decision = decider.decide(ctx, log)

    cur = conn.execute(
        """INSERT INTO attempts (concept_id, raw_transcript, clean_claims_json, hedging_score,
             correct_json, missing_json, misconceptions_json) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (concept_id, transcript, json.dumps(clean.claims), hedge,
         json.dumps(grade.correct_claims), json.dumps(grade.missing_points),
         json.dumps([m.model_dump() for m in grade.misconceptions])),
    )
    conn.commit()

    return AttemptResult(
        run_id=log.run_id,
        attempt_id=cur.lastrowid,
        concept_id=concept_id,
        clean=clean,
        hedging=hedge_info,
        grade=grade,
        attempt_score=score,
        state=state,
        decision=decision,
        step_timings=log.step_timings(),
        total_s=round(time.perf_counter() - run_start, 2),
        agent_log=log.entries(),
    )
