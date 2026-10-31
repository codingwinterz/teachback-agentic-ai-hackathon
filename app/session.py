"""Study sessions: an agent that chooses its own tools to pick the next concept and question.

The tool loop runs through llm.structured(...) with the AgentStep schema: each round the model
asks for up to 3 tools (plain DB reads, no LLM cost) or gives its final choice. ToolLoopDecider
plugs into the existing Decider seam, so the five-step pipeline is unchanged.
"""

import json
import sqlite3
import time
import uuid
from typing import Callable

from pydantic import ValidationError

from app import config, prompts, scoring, tools
from app.agent import Decider, SingleShotDecider, _log_llm, run_attempt
from app.llm import LLM, LLMError, LLMResult
from app.schemas import (
    AgentStep, AttemptResult, Decision, DecisionContext, FinalChoice, PlanStep, SessionStepView,
    SessionView, StudyPlan, ToolCall,
)

WEAK = 0.4  # below this a concept counts as failing; prerequisites must reach it to unlock
MASTERED = 0.85


class SessionBudgetExceeded(LLMError):
    pass


class SessionClosed(ValueError):
    pass


class ToolLoopError(RuntimeError):
    pass


# ---------------------------------------------------------------- data helpers


def _concepts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT id, name, prerequisites_json, reference_notes FROM concepts ORDER BY rowid")
    return [{"id": r["id"], "name": r["name"], "prerequisites": json.loads(r["prerequisites_json"]),
             "notes": r["reference_notes"]} for r in rows]


def _states(conn: sqlite3.Connection) -> dict[str, dict]:
    return {r["concept_id"]: dict(r) for r in conn.execute("SELECT * FROM learner_state")}


def _concept(conn: sqlite3.Connection, concept_id: str) -> dict:
    for c in _concepts(conn):
        if c["id"] == concept_id:
            return c
    raise KeyError(f"Unknown concept: {concept_id}")


def _mastery(states: dict, cid: str) -> float:
    return float(states.get(cid, {}).get("mastery", 0.0))


def concept_depths(concepts: list[dict]) -> dict[str, int]:
    """Longest prerequisite chain below each concept (0 = no prerequisites)."""
    by_id = {c["id"]: c for c in concepts}
    depth: dict[str, int] = {}

    def visit(cid: str) -> int:
        if cid not in depth:
            prereqs = [p for p in by_id[cid]["prerequisites"] if p in by_id]
            depth[cid] = 1 + max(map(visit, prereqs)) if prereqs else 0
        return depth[cid]

    for c in concepts:
        visit(c["id"])
    return depth


def ancestors(conn: sqlite3.Connection, concept_id: str) -> set[str]:
    by_id = {c["id"]: c for c in _concepts(conn)}
    seen: set[str] = set()
    stack = list(by_id[concept_id]["prerequisites"])
    while stack:
        p = stack.pop()
        if p not in seen and p in by_id:
            seen.add(p)
            stack.extend(by_id[p]["prerequisites"])
    return seen


# ---------------------------------------------------------------- the six tools


def get_learner_state(conn: sqlite3.Connection, concept_id: str) -> dict:
    c = _concept(conn, concept_id)
    s = _states(conn).get(concept_id, {})
    return {"concept_id": concept_id, "name": c["name"], "mastery": s.get("mastery", 0.0),
            "confidence": s.get("confidence", 0.0), "attempts": s.get("attempts_count", 0),
            "next_review_at": s.get("next_review_at"),
            "open_misconceptions": tools.get_open_misconceptions(conn, concept_id)}


def list_due_reviews(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT ls.concept_id, c.name, ls.mastery, ls.next_review_at FROM learner_state ls
           JOIN concepts c ON c.id = ls.concept_id
           WHERE ls.next_review_at IS NOT NULL AND ls.next_review_at <= datetime('now')
           ORDER BY ls.next_review_at"""
    ).fetchall()
    return [dict(r) for r in rows]


def list_weak_concepts(conn: sqlite3.Connection, limit: int = 5) -> list[dict]:
    rows = conn.execute(
        """SELECT ls.concept_id, c.name, ls.mastery, ls.attempts_count FROM learner_state ls
           JOIN concepts c ON c.id = ls.concept_id
           WHERE ls.attempts_count > 0 AND ls.mastery < 0.7 ORDER BY ls.mastery, c.rowid LIMIT ?""",
        (limit,),
    ).fetchall()
    return [{**dict(r), "open_misconceptions": tools.get_open_misconceptions(conn, r["concept_id"])}
            for r in rows]


def get_prerequisites(conn: sqlite3.Connection, concept_id: str) -> list[dict]:
    states = _states(conn)
    by_id = {c["id"]: c for c in _concepts(conn)}
    return [{"concept_id": p, "name": by_id[p]["name"], "mastery": _mastery(states, p),
             "attempts": states.get(p, {}).get("attempts_count", 0)}
            for p in _concept(conn, concept_id)["prerequisites"] if p in by_id]


def weakest_prerequisite(conn: sqlite3.Connection, concept_id: str) -> dict | None:
    prereqs = get_prerequisites(conn, concept_id)
    return min(prereqs, key=lambda p: p["mastery"]) if prereqs else None


def pick_next_concept(conn: sqlite3.Connection, strategy: str, concept_id: str | None = None,
                      current: str | None = None) -> dict:
    """weakest: lowest-mastery unlocked concept (not the current one, not mastered).
    due: oldest overdue review. prerequisite_gap: weakest prerequisite of concept_id/current.
    Falls back to weakest, and says so, when the strategy has nothing to offer."""
    concepts = _concepts(conn)
    states = _states(conn)
    order = concept_depths(concepts)
    note = ""

    if strategy == "due":
        due = [d for d in list_due_reviews(conn) if d["concept_id"] != current]
        if due:
            return {"concept_id": due[0]["concept_id"], "name": due[0]["name"], "strategy_used": "due",
                    "why": f"{due[0]['name']} is due for review."}
        note = "Nothing is due for review, so picking the weakest concept. "
    elif strategy == "prerequisite_gap":
        target = concept_id or current
        wp = weakest_prerequisite(conn, target) if target else None
        if wp:
            return {"concept_id": wp["concept_id"], "name": wp["name"], "strategy_used": "prerequisite_gap",
                    "why": f"{wp['name']} is the weakest prerequisite of {_concept(conn, target)['name']} "
                           f"(mastery {round(wp['mastery'] * 100)}%)."}
        note = "No prerequisite to walk back to, so picking the weakest concept. "

    unlocked = [c for c in concepts
                if c["id"] != current and _mastery(states, c["id"]) < MASTERED
                and all(_mastery(states, p) >= WEAK for p in c["prerequisites"])]
    if not unlocked:
        unlocked = [c for c in concepts if c["id"] != current] or concepts
    best = min(unlocked, key=lambda c: (_mastery(states, c["id"]), order[c["id"]],
                                        concepts.index(c)))
    return {"concept_id": best["id"], "name": best["name"], "strategy_used": "weakest",
            "why": note + f"{best['name']} is your weakest unlocked concept "
                          f"(mastery {round(_mastery(states, best['id']) * 100)}%)."}


def generate_followup_question(conn: sqlite3.Connection, concept_id: str,
                               misconception: str | None = None) -> dict:
    """A question built from the concept and reference notes (no LLM call)."""
    name = _concept(conn, concept_id)["name"]
    if misconception:
        q = (f"Earlier you said something like \"{misconception}\". Explain {name} again out loud, "
             f"and say where that idea goes wrong.")
    else:
        q = f"Explain {name} out loud as if teaching a friend: what it is, why it matters, and one common mistake."
    return {"concept_id": concept_id, "question": q}


TOOLS: dict[str, Callable] = {
    "get_learner_state": lambda conn, call, current: get_learner_state(conn, call.concept_id or current),
    "list_due_reviews": lambda conn, call, current: list_due_reviews(conn),
    "list_weak_concepts": lambda conn, call, current: list_weak_concepts(conn),
    "pick_next_concept": lambda conn, call, current: pick_next_concept(
        conn, call.strategy or "weakest", call.concept_id, current),
    "get_prerequisites": lambda conn, call, current: get_prerequisites(conn, call.concept_id or current),
    "generate_followup_question": lambda conn, call, current: generate_followup_question(
        conn, call.concept_id or current, call.misconception),
}


def run_tool(conn: sqlite3.Connection, call: ToolCall, current: str | None) -> dict | list:
    try:
        return TOOLS[call.tool](conn, call, current)
    except (KeyError, TypeError) as e:  # bad concept id or missing argument: tell the model
        return {"error": f"{call.tool} failed: {e}"}


# ---------------------------------------------------------------- budget + pacing


class BudgetedLLM:
    """Wraps an LLM for one session: counts every request against SESSION_MAX_CALLS
    (including retries/fallbacks the provider made) and paces calls."""

    def __init__(self, llm: LLM, conn: sqlite3.Connection, session_id: int,
                 max_calls: int = config.SESSION_MAX_CALLS, min_interval_s: float = 0.0,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.llm, self.conn, self.session_id = llm, conn, session_id
        self.max_calls, self.min_interval_s = max_calls, min_interval_s
        self.sleep, self.clock = sleep, clock
        self._last: float | None = None

    def used(self) -> int:
        return self.conn.execute("SELECT call_count FROM sessions WHERE id = ?", (self.session_id,)).fetchone()[0]

    def _add(self, n: int) -> None:
        self.conn.execute("UPDATE sessions SET call_count = call_count + ? WHERE id = ?", (n, self.session_id))
        self.conn.commit()

    def structured(self, system: str, user: str, schema, thinking: str | None = None) -> LLMResult:
        if self.used() >= self.max_calls:
            raise SessionBudgetExceeded(f"This session used its {self.max_calls} AI requests.")
        if self.min_interval_s and self._last is not None:
            wait = self.min_interval_s - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
        self._add(1)
        try:
            out = self.llm.structured(system, user, schema, thinking=thinking)
        finally:
            self._last = self.clock()
        extra = sum(1 for f in out.failed_attempts if f.get("status") != "skipped_exhausted")
        if extra:
            self._add(extra)
        return out


# ---------------------------------------------------------------- the tool-loop decider


def _session_history(conn: sqlite3.Connection, session_id: int) -> str:
    rows = conn.execute(
        "SELECT step_no, concept_id, mastery_after FROM session_steps WHERE session_id = ? ORDER BY step_no",
        (session_id,),
    ).fetchall()
    lines = [f"Q{r['step_no']}: {r['concept_id']}, mastery after "
             f"{'-' if r['mastery_after'] is None else round(r['mastery_after'], 2)}" for r in rows]
    return "\n".join(lines) or "(no questions yet)"


def session_attempts_on(conn: sqlite3.Connection, session_id: int, concept_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM session_steps WHERE session_id = ? AND concept_id = ? AND attempt_id IS NOT NULL",
        (session_id, concept_id),
    ).fetchone()[0]


class ToolLoopDecider:
    """Decider whose decision comes from a tool-use loop. Falls back to SingleShotDecider."""

    MAX_ROUNDS = 4

    def __init__(self, conn: sqlite3.Connection, llm: LLM, session_id: int, tool_cap: int = 6):
        self.conn, self.llm, self.session_id, self.tool_cap = conn, llm, session_id, tool_cap

    def _loop(self, task: str, current: str | None, log: tools.AgentLogger, step: str) -> FinalChoice:
        calls_text: list[str] = []
        used = 0
        system = prompts.SESSION_SYSTEM.format(tool_cap=self.tool_cap)
        for rnd in range(1, self.MAX_ROUNDS + 1):
            user = prompts.SESSION_USER.format(
                task=task, current=current or "(none)", history=_session_history(self.conn, self.session_id),
                used=used, tool_cap=self.tool_cap, calls="\n".join(calls_text) or "(none)",
                finish_note="You have used all tool calls: give `final` now." if used >= self.tool_cap else "",
            )
            out = self.llm.structured(system, user, AgentStep, thinking="low")
            _log_llm(log, step, "llm.agent_step", {"round": rnd}, out)
            agent_step: AgentStep = out.parsed
            if agent_step.final:
                final = agent_step.final
                _concept(self.conn, final.next_concept_id)  # KeyError if the model invented an id
                if not final.question.strip() or not final.why.strip():
                    raise ToolLoopError("final choice is missing a question or a reason")
                return final
            if used >= self.tool_cap:
                raise ToolLoopError(f"tool cap of {self.tool_cap} reached without a final choice")
            for call in agent_step.calls:
                if used >= self.tool_cap:
                    break
                start = time.perf_counter()
                result = run_tool(self.conn, call, current)
                used += 1
                args = call.model_dump(exclude_none=True, exclude={"tool"})
                log.log(step, f"tool.{call.tool}", args, result,
                        elapsed_s=round(time.perf_counter() - start, 4))
                calls_text.append(f"CALL {used}: {call.tool}({json.dumps(args)}) -> {json.dumps(result, default=str)}")
        raise ToolLoopError(f"no final choice after {self.MAX_ROUNDS} rounds")

    def decide(self, ctx: DecisionContext, log: tools.AgentLogger) -> Decision:
        n = session_attempts_on(self.conn, self.session_id, ctx.concept.id) + 1  # + this attempt
        task = (f"The student just answered a question on {ctx.concept.id} ({ctx.concept.name}). "
                f"Attempt score {ctx.attempt_score}, mastery now {ctx.state.mastery}. "
                f"Misconceptions: {json.dumps([m.label for m in ctx.grade.misconceptions])}. "
                f"Missing: {json.dumps(ctx.grade.missing_points)}. "
                f"Attempts on this concept this session: {n}. Choose the next question.")
        try:
            final = self._loop(task, ctx.concept.id, log, "decide")
            decision = Decision(action=final.action, reason=final.why, followup_question=final.question,
                                target_concept_id=final.next_concept_id)
        except SessionBudgetExceeded:
            raise
        except (ToolLoopError, ValidationError, LLMError, KeyError, ValueError) as e:
            log.log("decide", "fallback.single_shot", {"error": str(e)[:300]}, {})
            decision = SingleShotDecider(self.llm).decide(ctx, log)
        return apply_walk_back(self.conn, self.session_id, ctx.concept.id, ctx.state.mastery, decision, log)

    def choose_first(self, log: tools.AgentLogger) -> FinalChoice:
        try:
            return self._loop("Start a new study session: choose the first concept and question.",
                              None, log, "choose")
        except SessionBudgetExceeded:
            raise
        except (ToolLoopError, ValidationError, LLMError, KeyError, ValueError) as e:
            pick = pick_next_concept(self.conn, "due")
            q = generate_followup_question(self.conn, pick["concept_id"])["question"]
            log.log("choose", "fallback.rule_based", {"error": str(e)[:300]}, pick)
            return FinalChoice(action="review_due" if pick["strategy_used"] == "due" else "advance",
                               next_concept_id=pick["concept_id"], question=q, why=pick["why"])


def apply_walk_back(conn: sqlite3.Connection, session_id: int, concept_id: str, mastery: float,
                    decision: Decision, log: tools.AgentLogger) -> Decision:
    """Policy: two attempts this session with mastery < 0.4 -> walk back to the weakest prerequisite."""
    attempts = session_attempts_on(conn, session_id, concept_id) + 1
    if attempts < 2 or mastery >= WEAK:
        return decision
    if decision.action == "revisit_prerequisite" and decision.target_concept_id in ancestors(conn, concept_id):
        return decision  # the agent already walked back
    wp = weakest_prerequisite(conn, concept_id)
    if wp is None:
        return decision  # nothing below this concept to walk back to
    name = _concept(conn, concept_id)["name"]
    walked = Decision(
        action="revisit_prerequisite",
        reason=(f"You've scored below 40% on {name} twice this session, so let's strengthen its "
                f"prerequisite {wp['name']} first (mastery {round(wp['mastery'] * 100)}%)."),
        followup_question=generate_followup_question(conn, wp["concept_id"])["question"],
        target_concept_id=wp["concept_id"],
    )
    log.log("decide", "policy.walk_back",
            {"concept_id": concept_id, "attempts": attempts, "mastery": mastery,
             "agent_choice": decision.target_concept_id},
            walked.model_dump())
    return walked


# ---------------------------------------------------------------- study plan


def _review_date(next_review_at: str | None) -> str | None:
    return next_review_at[:10] if next_review_at else None


def _plan_line(r: dict) -> str:
    return (f"- {r['concept_id']} | {r['name']} | mastery {r['mastery']:.2f} | "
            f"misconceptions: {json.dumps(r['misconceptions'])} | next review {r['next_review'] or 'none'}")


def plan_rows(conn: sqlite3.Connection, session_id: int) -> tuple[list[dict], list[dict]]:
    """Concepts practised in the session (in order), and other weak concepts to pad the plan."""
    states = _states(conn)
    names = {c["id"]: c["name"] for c in _concepts(conn)}

    def row(cid: str) -> dict:
        s = states.get(cid, {})
        return {"concept_id": cid, "name": names[cid], "mastery": float(s.get("mastery", 0.0)),
                "misconceptions": tools.get_open_misconceptions(conn, cid),
                "next_review": _review_date(s.get("next_review_at"))}

    practised: list[str] = []
    for r in conn.execute("SELECT concept_id FROM session_steps WHERE session_id = ? AND attempt_id IS NOT NULL "
                          "ORDER BY step_no", (session_id,)):
        if r["concept_id"] not in practised:
            practised.append(r["concept_id"])
    # pad with weak attempted concepts, then the rest in "weakest unlocked first" order
    concepts = _concepts(conn)
    depths = concept_depths(concepts)
    unlocked_first = sorted(
        concepts,
        key=lambda c: (not all(_mastery(states, p) >= WEAK for p in c["prerequisites"]),
                       _mastery(states, c["id"]), depths[c["id"]], concepts.index(c)))
    extra_ids: list[str] = []
    for cid in [w["concept_id"] for w in list_weak_concepts(conn, limit=10)] + [c["id"] for c in unlocked_first]:
        if cid not in practised and cid not in extra_ids and _mastery(states, cid) < MASTERED:
            extra_ids.append(cid)
    return [row(c) for c in practised], [row(c) for c in extra_ids[:5]]


def plan_from_rows(rows: list[dict], extra: list[dict], today: str, source: str = "offline") -> StudyPlan:
    """Rule-based plan (used by the fake LLM and as the offline fallback): weakest practised first,
    padded with other weak concepts to at least 3 steps, at most 5."""
    from datetime import date, timedelta

    tomorrow = (date.fromisoformat(today) + timedelta(days=1)).isoformat()
    in_three = (date.fromisoformat(today) + timedelta(days=3)).isoformat()
    chosen = sorted(rows, key=lambda r: r["mastery"])[:5]
    for r in extra:
        if len(chosen) >= 3:
            break
        chosen.append(r)
    steps = []
    for r in chosen:
        m = r["misconceptions"][0] if r["misconceptions"] else None
        if m:
            todo = f"Re-explain {r['name']} out loud and fix the idea that it {m.removeprefix('thinks ')}."
        elif r["mastery"] < 0.4:
            todo = f"Re-read the key points of {r['name']}, then explain it again from scratch."
        elif r["mastery"] < 0.7:
            todo = f"Practise {r['name']} once more and cover what you missed."
        else:
            todo = f"Quick out-loud review of {r['name']}."
        steps.append(PlanStep(concept_id=r["concept_id"], concept_name=r["name"], what_to_do=todo,
                              misconception=m, next_review=r["next_review"] or (tomorrow if r["mastery"] < 0.7 else in_three)))
    focus = steps[0].concept_name if steps else "your weakest concept"
    summary = (f"You practised {len(rows)} concept{'s' if len(rows) != 1 else ''} this session. "
               f"Start with {focus}." if rows else f"Start with {focus}.")
    return StudyPlan(summary=summary, steps=steps, source=source)


def generate_plan(conn: sqlite3.Connection, llm: LLM, session_id: int, log: tools.AgentLogger,
                  use_llm: bool = True) -> StudyPlan:
    """One structured LLM call; offline rule-based plan if there was nothing to analyse, or the
    budget/quota ran out, or the model returned something unusable."""
    rows, extra = plan_rows(conn, session_id)
    today = scoring.utcnow().date().isoformat()
    plan = None
    if use_llm and rows:
        try:
            out = llm.structured(prompts.PLAN_SYSTEM,
                                 prompts.PLAN_USER.format(today=today, rows="\n".join(map(_plan_line, rows)),
                                                          extra="\n".join(map(_plan_line, extra)) or "(none)"),
                                 StudyPlan, thinking="low")
            _log_llm(log, "plan", "llm.plan", {"concepts": [r["concept_id"] for r in rows]}, out)
            known = {c["id"] for c in _concepts(conn)}
            if all(s.concept_id in known for s in out.parsed.steps):
                plan = out.parsed.model_copy(update={"source": "model"})
        except (LLMError, ValidationError) as e:
            log.log("plan", "fallback.offline_plan", {"error": str(e)[:300]}, {})
    if plan is None:
        plan = plan_from_rows(rows, extra, today)
        log.log("plan", "plan_from_rows", {"concepts": [r["concept_id"] for r in rows]}, plan.model_dump())
    conn.execute("UPDATE sessions SET plan_json = ? WHERE id = ?", (plan.model_dump_json(), session_id))
    conn.commit()
    return plan


def finish_session(conn: sqlite3.Connection, llm: LLM, session_id: int, status: str, message: str,
                   log: tools.AgentLogger, use_llm: bool = True) -> None:
    end_session(conn, session_id, status, message)
    log.step_start("plan")
    generate_plan(conn, llm, session_id, log, use_llm=use_llm)
    log.step_done("plan")


# ---------------------------------------------------------------- session flow


def _add_step(conn, session_id: int, step_no: int, concept_id: str, question: str, action: str, reason: str):
    conn.execute(
        """INSERT INTO session_steps (session_id, step_no, concept_id, question, action, reason)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (session_id, step_no, concept_id, question, action, reason),
    )
    conn.commit()


def end_session(conn: sqlite3.Connection, session_id: int, status: str, message: str) -> None:
    conn.execute(
        "UPDATE sessions SET status = ?, message = ?, ended_at = datetime('now') WHERE id = ? AND status = 'active'",
        (status, message, session_id),
    )
    conn.commit()


def stop_message(e: Exception, max_calls: int) -> str:
    if isinstance(e, SessionBudgetExceeded):
        return f"Session stopped: it used its {max_calls} AI requests. Your progress so far is saved."
    return ("Session stopped: the AI models are busy or out of free requests right now. "
            "Your progress so far is saved; start a new session later.")


def session_view(conn: sqlite3.Connection, session_id: int,
                 max_calls: int = config.SESSION_MAX_CALLS) -> SessionView:
    s = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if s is None:
        raise KeyError(f"Unknown session: {session_id}")
    rows = conn.execute(
        """SELECT st.*, c.name AS concept_name FROM session_steps st JOIN concepts c ON c.id = st.concept_id
           WHERE st.session_id = ? ORDER BY st.step_no""",
        (session_id,),
    ).fetchall()
    steps = [SessionStepView(step_no=r["step_no"], concept_id=r["concept_id"], concept_name=r["concept_name"],
                             question=r["question"], action=r["action"], reason=r["reason"],
                             attempt_id=r["attempt_id"], mastery_after=r["mastery_after"]) for r in rows]
    current = next((st for st in steps if st.attempt_id is None), None) if s["status"] == "active" else None
    plan = StudyPlan.model_validate_json(s["plan_json"]) if s["plan_json"] else None
    return SessionView(id=s["id"], status=s["status"], started_at=s["started_at"], ended_at=s["ended_at"],
                       call_count=s["call_count"], max_calls=max_calls,
                       max_questions=config.SESSION_MAX_QUESTIONS, message=s["message"],
                       steps=steps, current=current, plan=plan)


def start_session(conn: sqlite3.Connection, llm: LLM, on_event=None,
                  max_calls: int = config.SESSION_MAX_CALLS, min_interval_s: float = 0.0) -> SessionView:
    session_id = conn.execute("INSERT INTO sessions DEFAULT VALUES").lastrowid
    conn.commit()
    budget = BudgetedLLM(llm, conn, session_id, max_calls, min_interval_s)
    log = tools.AgentLogger(conn, f"s{session_id}-{uuid.uuid4().hex[:6]}", on_event=on_event)
    log.step_start("choose")
    try:
        final = ToolLoopDecider(conn, budget, session_id).choose_first(log)
    except LLMError as e:
        finish_session(conn, budget, session_id, "stopped", stop_message(e, max_calls), log, use_llm=False)
        return session_view(conn, session_id, max_calls)
    _add_step(conn, session_id, 1, final.next_concept_id, final.question, final.action, final.why)
    log.step_done("choose")
    return session_view(conn, session_id, max_calls)


def llm_for_lesson(conn: sqlite3.Connection, llm: LLM, lesson_id: int,
                   max_calls: int = config.SESSION_MAX_CALLS, min_interval_s: float = 0.0) -> LLM:
    """A lesson check answered during an active session counts against that session's budget."""
    row = conn.execute(
        """SELECT s.id FROM lessons l JOIN session_steps st ON st.attempt_id = l.attempt_id
           JOIN sessions s ON s.id = st.session_id WHERE l.id = ? AND s.status = 'active'""",
        (lesson_id,),
    ).fetchone()
    return BudgetedLLM(llm, conn, row["id"], max_calls, min_interval_s) if row else llm


def end_by_user(conn: sqlite3.Connection, llm: LLM, session_id: int, on_event=None,
                max_calls: int = config.SESSION_MAX_CALLS, min_interval_s: float = 0.0) -> SessionView:
    view = session_view(conn, session_id, max_calls)
    if view.status == "active":
        budget = BudgetedLLM(llm, conn, session_id, max_calls, min_interval_s)
        log = tools.AgentLogger(conn, f"s{session_id}-end-{uuid.uuid4().hex[:6]}", on_event=on_event)
        finish_session(conn, budget, session_id, "finished", "Session ended.", log)
    return session_view(conn, session_id, max_calls)


def _ensure_next(conn: sqlite3.Connection, decision: Decision, current: str) -> Decision:
    """A single-shot fallback may omit the next concept or question: fill them in by rule."""
    target = decision.target_concept_id
    if not target:
        target = current if decision.action in ("ask_followup", "re_explain") else \
            pick_next_concept(conn, "weakest", current=current)["concept_id"]
    question = decision.followup_question or generate_followup_question(conn, target)["question"]
    return decision.model_copy(update={"target_concept_id": target, "followup_question": question})


def answer_session(conn: sqlite3.Connection, llm: LLM, session_id: int, transcript: str, on_event=None,
                   max_calls: int = config.SESSION_MAX_CALLS, min_interval_s: float = 0.0,
                   ) -> tuple[AttemptResult | None, SessionView]:
    view = session_view(conn, session_id, max_calls)
    if view.status != "active" or view.current is None:
        raise SessionClosed(f"Session {session_id} is {view.status}")
    cur = view.current
    budget = BudgetedLLM(llm, conn, session_id, max_calls, min_interval_s)
    last = cur.step_no >= config.SESSION_MAX_QUESTIONS
    decider: Decider = SingleShotDecider(budget) if last else ToolLoopDecider(conn, budget, session_id)
    plan_log = tools.AgentLogger(conn, f"s{session_id}-plan-{uuid.uuid4().hex[:6]}", on_event=on_event)
    try:
        result = run_attempt(conn, budget, cur.concept_id, transcript, decider=decider, on_event=on_event)
    except LLMError as e:  # budget or quota: stop, with a rule-based plan (no further calls)
        finish_session(conn, budget, session_id, "stopped", stop_message(e, max_calls), plan_log, use_llm=False)
        return None, session_view(conn, session_id, max_calls)

    conn.execute(
        "UPDATE session_steps SET attempt_id = ?, mastery_after = ? WHERE session_id = ? AND step_no = ?",
        (result.attempt_id, result.state.mastery, session_id, cur.step_no),
    )
    conn.commit()
    if last:
        finish_session(conn, budget, session_id, "finished",
                       f"Session complete: {config.SESSION_MAX_QUESTIONS} questions answered.", plan_log)
    else:
        d = _ensure_next(conn, result.decision, cur.concept_id)
        _add_step(conn, session_id, cur.step_no + 1, d.target_concept_id, d.followup_question, d.action, d.reason)
    return result, session_view(conn, session_id, max_calls)
