"""Study session: tool loop, cap, fallback, walk-back policy, budget, pacing and the API (fake LLM only)."""

import json

import pytest

from app import config, db, session
from app.fake_llm import FakeLLM
from app.llm import LLMError, LLMResult
from app.schemas import AgentStep, FinalChoice, ToolCall

WEAK_ANSWER = "Um, I think it's when the model always learns stuff."
STRONG_LOSS = ("A loss function measures how wrong a prediction is; training minimizes the average loss. "
               "MSE for regression, cross-entropy for classification.")


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "session.db"))
    db.init_db(c)
    db.seed_concepts(c)
    return c


def _knows_roots(conn):
    """A learner with the roots done, so deeper concepts are unlocked."""
    for cid, m in [("supervised_learning", 0.8), ("train_test_split", 0.55), ("loss_function", 0.75)]:
        conn.execute("UPDATE learner_state SET mastery = ?, attempts_count = 1 WHERE concept_id = ?", (m, cid))
    conn.commit()


class ScriptedAgent(FakeLLM):
    """FakeLLM for the pipeline, but AgentStep outputs come from a script (or raise)."""

    def __init__(self, script):
        super().__init__()
        self.script = script
        self.agent_calls = 0

    def structured(self, system, user, schema, thinking=None):
        if schema is AgentStep:
            self.agent_calls += 1
            out = self.script(self.agent_calls, user)
            if isinstance(out, Exception):
                raise out
            return LLMResult(parsed=out, raw_text=out.model_dump_json(), model="scripted")
        return super().structured(system, user, schema, thinking)


def _tool_names(conn, prefix="tool."):
    return [r[0] for r in conn.execute(
        "SELECT tool_name FROM agent_log WHERE tool_name LIKE ? ORDER BY id", (prefix + "%",))]


# ---------------------------------------------------------------- tools


def test_pick_next_concept_strategies(conn):
    _knows_roots(conn)
    weakest = session.pick_next_concept(conn, "weakest")
    assert weakest["concept_id"] == "gradient_descent"  # unlocked (loss_function >= 0.4) and untouched
    assert session.pick_next_concept(conn, "due")["strategy_used"] == "weakest"  # nothing due -> falls back
    conn.execute("UPDATE learner_state SET next_review_at = datetime('now', '-1 day') WHERE concept_id = 'loss_function'")
    assert session.pick_next_concept(conn, "due")["concept_id"] == "loss_function"
    gap = session.pick_next_concept(conn, "prerequisite_gap", current="overfitting")
    assert gap["concept_id"] == "train_test_split" and gap["strategy_used"] == "prerequisite_gap"
    assert "error" in session.run_tool(conn, ToolCall(tool="get_prerequisites", concept_id="nope"), None)


# ---------------------------------------------------------------- tool loop


def test_tool_loop_happy_path_logs_every_tool_with_args(conn):
    _knows_roots(conn)
    events = []
    view = session.start_session(conn, FakeLLM(), on_event=events.append)
    assert view.status == "active" and view.current.step_no == 1
    assert view.current.concept_id == "gradient_descent" and view.current.reason
    assert view.call_count == 2  # one round of tools + the final choice
    tool_events = [e for e in events if e["type"] == "tool" and e["tool_name"].startswith("tool.")]
    assert [e["tool_name"] for e in tool_events] == [
        "tool.list_due_reviews", "tool.list_weak_concepts", "tool.pick_next_concept"]
    assert tool_events[2]["args"] == {"strategy": "due"} and "gradient_descent" in tool_events[2]["result"]
    assert [e["type"] for e in events if e["type"].startswith("step")] == ["step_start", "step_done"]


def test_tool_loop_cap_then_falls_back_to_single_shot(conn):
    _knows_roots(conn)
    never_finishes = ScriptedAgent(lambda n, user: AgentStep(calls=[
        ToolCall(tool="list_weak_concepts"), ToolCall(tool="list_due_reviews"), ToolCall(tool="list_weak_concepts")]))
    view = session.start_session(conn, FakeLLM())
    result, view = session.answer_session(conn, never_finishes, view.id, WEAK_ANSWER)
    assert len(_tool_names(conn)) - 3 == 6  # 3 tools from the start + exactly 6 (the cap) for this decision
    assert "fallback.single_shot" in _tool_names(conn, "fallback.")
    assert view.status == "active" and view.current.step_no == 2  # session keeps going


@pytest.mark.parametrize("bad", [
    lambda n, u: LLMError("Validation failed after retry"),
    lambda n, u: AgentStep(final=FinalChoice(action="advance", next_concept_id="made_up_concept",
                                             question="?", why="because")),
])
def test_bad_agent_output_falls_back_and_session_continues(conn, bad):
    _knows_roots(conn)
    view = session.start_session(conn, FakeLLM())
    result, view = session.answer_session(conn, ScriptedAgent(bad), view.id, WEAK_ANSWER)
    assert result is not None and view.status == "active"
    assert _tool_names(conn, "fallback.") == ["fallback.single_shot"]
    assert view.current.question  # rule-based fill-in when single-shot omits it


def test_start_falls_back_to_rules_when_agent_fails(conn):
    view = session.start_session(conn, ScriptedAgent(lambda n, u: LLMError("boom")))
    assert view.status == "active" and view.current.concept_id == "supervised_learning"
    assert _tool_names(conn, "fallback.") == ["fallback.rule_based"]


def test_walk_back_after_two_failed_attempts(conn):
    _knows_roots(conn)
    view = session.start_session(conn, FakeLLM())
    assert view.current.concept_id == "gradient_descent"
    _, view = session.answer_session(conn, FakeLLM(), view.id, WEAK_ANSWER)
    assert view.current.concept_id == "gradient_descent" and view.current.action == "ask_followup"
    _, view = session.answer_session(conn, FakeLLM(), view.id, WEAK_ANSWER)
    assert view.current.action == "revisit_prerequisite"
    assert view.current.concept_id == "loss_function"  # the only prerequisite of gradient descent
    assert "twice" in view.current.reason and "Loss Function" in view.current.reason


def test_walk_back_policy_overrides_an_agent_that_ignores_it(conn):
    _knows_roots(conn)
    stubborn = ScriptedAgent(lambda n, user: AgentStep(final=FinalChoice(
        action="advance", next_concept_id="boosting", question="Explain boosting.", why="Why not.")))
    view = session.start_session(conn, FakeLLM())
    session.answer_session(conn, stubborn, view.id, WEAK_ANSWER)  # 1st weak attempt: agent's choice stands
    conn.execute("UPDATE session_steps SET concept_id = 'gradient_descent' WHERE step_no = 2")
    conn.commit()
    _, view = session.answer_session(conn, stubborn, view.id, WEAK_ANSWER)  # 2nd weak attempt
    assert view.current.concept_id == "loss_function" and view.current.action == "revisit_prerequisite"
    assert "twice" in view.current.reason
    assert "policy.walk_back" in _tool_names(conn, "policy.")


# ---------------------------------------------------------------- budget + pacing


def test_session_stops_after_five_questions(conn):
    _knows_roots(conn)
    view = session.start_session(conn, FakeLLM())
    for _ in range(10):
        if view.status != "active":
            break
        _, view = session.answer_session(conn, FakeLLM(), view.id, STRONG_LOSS)
    assert view.status == "finished" and len(view.steps) == config.SESSION_MAX_QUESTIONS
    assert all(st.attempt_id for st in view.steps)
    assert view.call_count <= config.SESSION_MAX_CALLS
    with pytest.raises(session.SessionClosed):
        session.answer_session(conn, FakeLLM(), view.id, STRONG_LOSS)


def test_call_budget_stops_gracefully(conn):
    _knows_roots(conn)
    view = session.start_session(conn, FakeLLM(), max_calls=8)
    result, view = session.answer_session(conn, FakeLLM(), view.id, WEAK_ANSWER, max_calls=8)
    assert view.status == "active" and view.call_count <= 8
    result, view = session.answer_session(conn, FakeLLM(), view.id, WEAK_ANSWER, max_calls=8)
    assert result is None and view.status == "stopped"
    assert "used its 8 AI requests" in view.message and view.call_count == 8


def test_quota_error_ends_session_gracefully(conn):
    class OutOfQuota(FakeLLM):
        def structured(self, *a, **k):
            raise LLMError("All models failed (...). Last: 429")

    view = session.start_session(conn, FakeLLM())
    result, view = session.answer_session(conn, OutOfQuota(), view.id, WEAK_ANSWER)
    assert result is None and view.status == "stopped" and "out of free requests" in view.message


def test_budget_counts_provider_retries_and_paces_calls(conn):
    sid = conn.execute("INSERT INTO sessions DEFAULT VALUES").lastrowid

    class RetriedOnce(FakeLLM):
        def structured(self, *a, **k):
            out = super().structured(*a, **k)
            out.failed_attempts = [{"model": "m", "status": 503}, {"model": "x", "status": "skipped_exhausted"}]
            return out

    sleeps, now = [], [0.0]
    budget = session.BudgetedLLM(RetriedOnce(), conn, sid, max_calls=30, min_interval_s=4.0,
                                 sleep=lambda s: (sleeps.append(round(s, 2)), now.__setitem__(0, now[0] + s)),
                                 clock=lambda: now[0])
    from app.schemas import CheckResult
    prompt = "Expected answer (key points): noise\n<answer>noise</answer>"
    budget.structured("s", prompt, CheckResult)
    now[0] += 1.0  # 1s later
    budget.structured("s", prompt, CheckResult)
    assert sleeps == [3.0]  # waited the remaining 3s of the 4s interval
    assert budget.used() == 4  # 2 calls + 1 real retry each (skipped models don't count)


# ---------------------------------------------------------------- API


def test_session_api_streams_events_and_closes(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app import llm as llm_module
    from app import main

    monkeypatch.setattr(config, "LLM_MODE", "fake")
    monkeypatch.setattr(config, "FAKE_LLM_DELAY_S", 0.0)
    llm_module.get_llm.cache_clear()
    monkeypatch.setattr(main, "conn", db.connect(str(tmp_path / "api.db")))

    def events(resp):
        return [json.loads(line[6:]) for line in resp.iter_lines() if line.startswith("data: ")]

    with TestClient(main.app) as client:
        with client.stream("POST", "/session/start") as s:
            ev = events(s)
        view = ev[-1]["data"]
        assert ev[-1]["type"] == "session" and view["status"] == "active"
        assert any(e["type"] == "tool" and e["tool_name"] == "tool.pick_next_concept" for e in ev)
        sid = view["id"]

        with client.stream("POST", f"/session/{sid}/answer", json={"transcript": WEAK_ANSWER}) as s:
            ev = events(s)
        types = [e["type"] for e in ev]
        assert "result" in types and types[-1] == "session"
        assert ev[-1]["data"]["current"]["step_no"] == 2

        ended = client.post(f"/session/{sid}/end").json()
        assert ended["status"] == "finished" and ended["current"] is None
        with client.stream("POST", f"/session/{sid}/answer", json={"transcript": "x"}) as s:
            ev = events(s)
        assert ev[-1]["type"] == "error" and ev[-1]["status"] == 409
        assert client.get("/session/9999").status_code == 404
    llm_module.get_llm.cache_clear()
