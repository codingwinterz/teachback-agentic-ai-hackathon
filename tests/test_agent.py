import json
from types import SimpleNamespace

import httpx
import pytest
from google.genai import errors

from app import config, db, hedging
from app.agent import run_attempt
from app.fake_llm import FakeLLM
from app.llm import GeminiLLM, LLMError, LLMResult
from app.schemas import CleanResult, Decision, GradeResult, Misconception

MESSY = (
    "Um, okay so overfitting is, like, when the model, uh, I think it's when it fits "
    "the training data too well, no wait, when it fits the noise in the training data. "
    "So it does really great on the training set but, um, badly on new data. "
    "And I guess, you know, adding more data always fixes it."
)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "test.db"))
    db.init_db(c)
    db.seed_concepts(c)
    return c


class CannedLLM:
    """Returns canned outputs keyed by schema, so the loop runs without an API key."""

    def __init__(self):
        self.calls = []
        self.thinking = {}
        self.outputs = {
            CleanResult: CleanResult(
                claims=[
                    "Overfitting is when the model fits the noise in the training data.",
                    "An overfit model does well on training data but badly on new data.",
                    "Adding more data always fixes overfitting.",
                ],
                self_corrections=["fits the training data too well -> fits the noise in the training data"],
                hedging_score=0.6,
            ),
            GradeResult: GradeResult(
                correct_claims=[
                    "Overfitting is when the model fits the noise in the training data.",
                    "An overfit model does well on training data but badly on new data.",
                ],
                missing_points=["causes such as model complexity", "remedies like regularization"],
                misconceptions=[Misconception(
                    label="thinks more data always fixes overfitting",
                    evidence="Adding more data always fixes overfitting.",
                )],
            ),
            Decision: Decision(
                action="ask_followup",
                reason="You have the core idea; let's test the 'more data' claim.",
                followup_question="Can more data ever fail to fix overfitting?",
            ),
        }

    def structured(self, system, user, schema, thinking=None):
        self.calls.append((schema.__name__, user))
        self.thinking[schema.__name__] = thinking
        out = self.outputs[schema]
        return LLMResult(parsed=out, raw_text=out.model_dump_json(), model="fake-model",
                         elapsed_s=0.5, last_call_s=0.5)


def test_code_hedging_counts_messy_transcript():
    counts = hedging.code_hedging(MESSY)
    assert counts.fillers >= 4  # um, like, uh, um, you know
    assert counts.hedges == 2  # I think, I guess
    assert counts.reversals == 1  # no wait
    assert 0 < counts.score <= 1


def test_code_hedging_clean_text_is_zero():
    assert hedging.code_hedging("Overfitting means fitting noise instead of signal.").score == 0


def test_core_loop_with_fake_llm(conn):
    llm = CannedLLM()
    result = run_attempt(conn, llm, "overfitting", MESSY)

    assert [c[0] for c in llm.calls] == ["CleanResult", "GradeResult", "Decision"]
    assert llm.thinking == {"CleanResult": "low", "GradeResult": None, "Decision": "low"}
    # blended hedge = mean of LLM 0.6 and the code score
    code = hedging.code_hedging(MESSY).score
    assert result.hedging["blended"] == round((0.6 + code) / 2, 3)
    # score = 2 correct / (2 + 0.5*2 missing + 1 misconception) = 0.5, discounted by hedging
    assert result.attempt_score == round(0.5 * (1 - 0.5 * result.hedging["blended"]), 3)
    assert result.state.mastery == result.attempt_score  # first attempt
    assert result.state.attempts_count == 1
    assert result.decision.action == "ask_followup"

    misc = conn.execute("SELECT label, count FROM misconceptions").fetchall()
    assert [tuple(r) for r in misc] == [("thinks more data always fixes overfitting", 1)]
    assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1
    tool_names = [e["tool_name"] for e in result.agent_log]
    assert tool_names[:4] == ["get_concept", "llm.clean", "hedging.blend", "llm.grade"]
    assert "update_learner_state" in tool_names and tool_names[-1] == "llm.decide"

    # model + elapsed_s recorded per entry, and summarized per step
    llm_entries = [e for e in result.agent_log if e["tool_name"].startswith("llm.")]
    assert all(e["model"] == "fake-model" and e["elapsed_s"] == 0.5 for e in llm_entries)
    assert all(e["elapsed_s"] is not None for e in result.agent_log)
    assert list(result.step_timings) == ["context", "clean", "grade", "update", "decide"]
    assert result.step_timings["clean"] >= 0.5 and result.step_timings["decide"] >= 0.5
    assert result.total_s >= 0

    # second attempt: misconception count increments, mastery is a moving average
    second = run_attempt(conn, llm, "overfitting", MESSY)
    assert second.state.attempts_count == 2
    assert conn.execute("SELECT count FROM misconceptions").fetchone()[0] == 2


def _gemini_response(text, finish="STOP"):
    return SimpleNamespace(
        text=text, candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name=finish))]
    )


class FakeGeminiClient:
    """Stands in for genai.Client: pops queued results (responses or exceptions)."""

    def __init__(self, queue):
        self.queue = list(queue)
        self.prompts = []
        self.called_models = []
        self.configs = []
        self.models = self

    def generate_content(self, model, contents, config):
        self.prompts.append(contents)
        self.called_models.append(model)
        self.configs.append(config)
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_gemini_backs_off_on_rate_limit():
    rate_limited = errors.ClientError(429, {"error": {
        "code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED",
        "details": [{"retryDelay": "3s"}]}})
    ok = Decision(action="advance", reason="Solid.").model_dump_json()
    client = FakeGeminiClient([rate_limited, errors.ServerError(503, {}), _gemini_response(ok)])
    sleeps = []
    llm = GeminiLLM(model="m", client=client, sleep=sleeps.append, fallback_models=[])

    result = llm.structured("sys", "user", Decision)

    assert result.parsed.action == "advance"
    assert len(sleeps) == 2
    assert 3 <= sleeps[0] < 4  # honored the server's retryDelay
    assert 4 <= sleeps[1] < 5  # exponential: 2 * 2**1


def test_gemini_falls_back_to_next_model_when_primary_stays_overloaded():
    overloaded = lambda: errors.ServerError(503, {"error": {"code": 503, "message": "high demand"}})
    ok = Decision(action="advance", reason="Solid.").model_dump_json()
    client = FakeGeminiClient([overloaded(), overloaded(), overloaded(), _gemini_response(ok)])
    llm = GeminiLLM(model="primary", client=client, sleep=lambda s: None, fallback_models=["backup"])

    result = llm.structured("sys", "user", Decision)

    assert client.called_models == ["primary"] * 3 + ["backup"]
    assert result.model == "backup"
    assert [(f["model"], f["status"]) for f in result.failed_attempts] == [("primary", 503)] * 3
    assert result.timing()["api_retries"] == 3

    client = FakeGeminiClient([overloaded() for _ in range(6)])
    llm = GeminiLLM(model="primary", client=client, sleep=lambda s: None, fallback_models=["backup"])
    with pytest.raises(LLMError, match="All models failed"):
        llm.structured("sys", "user", Decision)


def test_gemini_timeout_is_retryable_and_config_carries_timeout_and_thinking():
    ok = Decision(action="advance", reason="Solid.").model_dump_json()
    deadline = errors.ServerError(504, {"error": {"code": 504, "message": "Deadline expired"}})
    client = FakeGeminiClient([httpx.ReadTimeout("slow"), deadline, _gemini_response(ok)])
    llm = GeminiLLM(model="m", client=client, sleep=lambda s: None, fallback_models=[],
                    call_timeout_s=12)

    result = llm.structured("sys", "user", Decision, thinking="low")

    assert result.parsed.action == "advance"
    assert [f["status"] for f in result.failed_attempts] == ["timeout", 504]
    cfg = client.configs[0]
    assert cfg.http_options.timeout == 12000
    assert cfg.thinking_config.thinking_level.value == "LOW"

    client = FakeGeminiClient([_gemini_response(ok)])
    GeminiLLM(model="m", client=client, sleep=lambda s: None).structured("s", "u", Decision)
    assert client.configs[0].thinking_config is None  # grade path: model default


def test_gemini_daily_quota_skips_model_without_sleeping():
    daily = errors.ClientError(429, {"error": {"code": 429, "message": "quota",
                                               "details": [{"retryDelay": "72629s"}]}})
    ok = Decision(action="advance", reason="Solid.").model_dump_json()
    client = FakeGeminiClient([daily, _gemini_response(ok), _gemini_response(ok)])
    sleeps = []
    llm = GeminiLLM(model="primary", client=client, sleep=sleeps.append, fallback_models=["backup"])

    first = llm.structured("s", "u", Decision)
    second = llm.structured("s", "u", Decision)

    assert sleeps == []  # never waits out a 20h retryDelay
    assert client.called_models == ["primary", "backup", "backup"]  # primary skipped on 2nd call
    assert first.model == second.model == "backup"
    assert second.failed_attempts == [{"model": "primary", "status": "skipped_exhausted", "call_s": 0.0}]


CHAIN = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.6-flash", "gemini-3.8-flash"]


def test_default_model_chain_order():
    assert [config.DEFAULT_MODELS["gemini"]] + config.DEFAULT_FALLBACK_MODELS.split(",") == CHAIN


def test_full_chain_skips_every_exhausted_model_on_later_calls():
    def daily():
        return errors.ClientError(429, {"error": {"code": 429, "message": "quota",
                                                  "details": [{"retryDelay": "72629s"}]}})

    ok = Decision(action="advance", reason="Solid.").model_dump_json()
    client = FakeGeminiClient([daily(), daily(), daily(), _gemini_response(ok), _gemini_response(ok)])
    llm = GeminiLLM(model=CHAIN[0], client=client, sleep=lambda s: None, fallback_models=CHAIN[1:])

    first = llm.structured("s", "u", Decision)
    second = llm.structured("s", "u", Decision)

    assert client.called_models == CHAIN + ["gemini-3.8-flash"]  # 2nd call: one request only
    assert first.model == second.model == "gemini-3.8-flash"
    assert [f["status"] for f in second.failed_attempts] == ["skipped_exhausted"] * 3


def test_gemini_does_not_fall_back_on_client_errors():
    client = FakeGeminiClient([errors.ClientError(400, {"error": {"code": 400, "message": "bad schema"}})])
    llm = GeminiLLM(model="primary", client=client, sleep=lambda s: None, fallback_models=["backup"])
    with pytest.raises(LLMError, match="400"):
        llm.structured("sys", "user", Decision)
    assert client.called_models == ["primary"]


def test_gemini_retries_once_on_validation_failure():
    bad = '{"action": "celebrate", "reason": "x"}'  # not a valid Action
    good = Decision(action="re_explain", reason="Try again.").model_dump_json()
    client = FakeGeminiClient([_gemini_response(bad), _gemini_response(good)])
    result = GeminiLLM(model="m", client=client, sleep=lambda s: None).structured("s", "u", Decision)
    assert result.parsed.action == "re_explain"
    assert "failed validation" in client.prompts[1]

    client = FakeGeminiClient([_gemini_response(bad), _gemini_response(bad)])
    with pytest.raises(LLMError, match="Validation failed after retry"):
        GeminiLLM(model="m", client=client, sleep=lambda s: None).structured("s", "u", Decision)


def test_init_db_migrates_old_agent_log(tmp_path):
    c = db.connect(str(tmp_path / "old.db"))
    c.execute("""CREATE TABLE agent_log (id INTEGER PRIMARY KEY, run_id TEXT, step TEXT,
                 tool_name TEXT, args_json TEXT, result_json TEXT, created_at TEXT)""")
    db.init_db(c)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(agent_log)")}
    assert {"model", "elapsed_s"} <= cols


def test_dev_fake_llm_runs_messy_transcript_end_to_end(conn):
    llm = FakeLLM()
    result = run_attempt(conn, llm, "overfitting", MESSY)

    assert any("noise in the training data" in c for c in result.clean.claims)
    assert not any("too well" in c for c in result.clean.claims)  # self-correction resolved
    assert len(result.clean.self_corrections) == 1
    assert result.clean.hedging_score == hedging.code_hedging(MESSY).score
    assert [m.evidence for m in result.grade.misconceptions] == ["Adding more data always fixes it."]
    assert 1 <= len(result.grade.missing_points) <= 3
    assert result.decision.action == "ask_followup" and result.decision.followup_question
    assert {e["model"] for e in result.agent_log if e["tool_name"].startswith("llm.")} == {"fake-dev"}

    # second attempt reuses the stored label, so the misconception count goes up
    run_attempt(conn, llm, "overfitting", MESSY)
    assert [tuple(r) for r in conn.execute("SELECT count FROM misconceptions")] == [(2,)]


def test_dev_fake_llm_advances_on_a_strong_answer(conn):
    strong = ("Overfitting is when a model learns the noise in the training data instead of the "
              "underlying pattern. It shows low training error but high error on unseen validation "
              "data. Causes include a model too complex for the data. Remedies include regularization, "
              "early stopping, simpler models and more representative data.")
    result = run_attempt(conn, FakeLLM(), "overfitting", strong)
    assert result.grade.misconceptions == []
    assert result.decision.action == "advance"
    assert result.decision.target_concept_id == "bias_variance"


def test_get_llm_defaults_to_fake_and_health_reports_it(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app import llm as llm_module
    from app import main

    monkeypatch.setattr(config, "LLM_MODE", "fake")
    monkeypatch.setattr(config, "FAKE_LLM_DELAY_S", 0.0)
    llm_module.get_llm.cache_clear()
    assert isinstance(llm_module.get_llm(), FakeLLM)

    monkeypatch.setattr(main, "conn", db.connect(str(tmp_path / "api.db")))
    with TestClient(main.app) as client:
        assert client.get("/health").json()["llm_mode"] == "fake"
        r = client.post("/attempt", json={"concept_id": "overfitting", "transcript": MESSY})
        assert r.status_code == 200 and r.json()["decision"]["action"] == "ask_followup"

        # streaming endpoint: step events arrive in order, then the full result
        with client.stream("POST", "/attempt/stream",
                           json={"concept_id": "overfitting", "transcript": MESSY}) as s:
            events = [json.loads(line[6:]) for line in s.iter_lines() if line.startswith("data: ")]
        steps = [(e["type"], e["step"]) for e in events if e["type"] in ("step_start", "step_done")]
        assert steps == [(t, s) for s in ("clean", "grade", "update", "decide")
                         for t in ("step_start", "step_done")]
        done = {e["step"]: e for e in events if e["type"] == "step_done"}
        assert done["clean"]["model"] == "fake-dev" and done["update"]["model"] == "code"
        assert events[-1]["type"] == "result"
        assert events[-1]["data"]["state"]["attempts_count"] == 2

        bad = client.post("/attempt/stream", json={"concept_id": "nope", "transcript": "x"})
        assert '"type": "error"' in bad.text and '"status": 404' in bad.text

        # the single-page UI is served at /
        page = client.get("/")
        assert page.status_code == 200 and 'id="transcript"' in page.text and "autofocus" in page.text
        assert client.get("/app.js").status_code == 200
    llm_module.get_llm.cache_clear()


@pytest.mark.skipif(
    config.LLM_MODE != "real" or not config.GEMINI_API_KEY,
    reason="costs real quota: run with LLM_MODE=real (and GEMINI_API_KEY set)",
)
def test_core_loop_real_model(conn):
    from app.llm import get_llm

    result = run_attempt(conn, get_llm(), "overfitting", MESSY)
    print(f"\n===== RAW MODEL OUTPUT PER STEP (primary: {config.LLM_MODEL}) =====")
    for e in result.agent_log:
        if e["tool_name"].startswith("llm."):
            print(f"\n--- {e['tool_name']} ---\n{e['result']['raw_text']}")
    print("\n===== HEDGING =====\n" + json.dumps(result.hedging, indent=2))
    print(f"\nattempt_score={result.attempt_score}  mastery={result.state.mastery}")
    print("\n===== TIMING PER LLM CALL =====")
    for e in result.agent_log:
        if e["tool_name"].startswith("llm."):
            t = e["result"]["timing"]
            print(f"{e['tool_name']:<11} model={t['model']:<22} elapsed={t['elapsed_s']:>6.2f}s "
                  f"last_call={t['last_call_s']:>6.2f}s api_retries={t['api_retries']} "
                  f"validation_retries={t['validation_retries']} failed={t['failed_attempts']}")
    print(f"step_timings={result.step_timings}  total={result.total_s}s")

    assert result.clean.claims
    assert not any("too well" in c.lower() and "noise" not in c.lower() for c in result.clean.claims)
    assert result.decision.action in {"re_explain", "ask_followup", "revisit_prerequisite", "advance", "review_due"}
