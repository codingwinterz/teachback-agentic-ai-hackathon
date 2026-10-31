import json
from types import SimpleNamespace

import pytest
from google.genai import errors

from app import config, db, hedging
from app.agent import run_attempt
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


class FakeLLM:
    """Returns canned outputs keyed by schema, so the loop runs without an API key."""

    def __init__(self):
        self.calls = []
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

    def structured(self, system, user, schema):
        self.calls.append((schema.__name__, user))
        out = self.outputs[schema]
        return LLMResult(parsed=out, raw_text=out.model_dump_json())


def test_code_hedging_counts_messy_transcript():
    counts = hedging.code_hedging(MESSY)
    assert counts.fillers >= 4  # um, like, uh, um, you know
    assert counts.hedges == 2  # I think, I guess
    assert counts.reversals == 1  # no wait
    assert 0 < counts.score <= 1


def test_code_hedging_clean_text_is_zero():
    assert hedging.code_hedging("Overfitting means fitting noise instead of signal.").score == 0


def test_core_loop_with_fake_llm(conn):
    llm = FakeLLM()
    result = run_attempt(conn, llm, "overfitting", MESSY)

    assert [c[0] for c in llm.calls] == ["CleanResult", "GradeResult", "Decision"]
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
        self.models = self

    def generate_content(self, model, contents, config):
        self.prompts.append(contents)
        self.called_models.append(model)
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

    client = FakeGeminiClient([overloaded() for _ in range(6)])
    llm = GeminiLLM(model="primary", client=client, sleep=lambda s: None, fallback_models=["backup"])
    with pytest.raises(LLMError, match="All models failed"):
        llm.structured("sys", "user", Decision)


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


@pytest.mark.skipif(not config.GEMINI_API_KEY, reason="needs GEMINI_API_KEY")
def test_core_loop_real_model(conn):
    from app.llm import get_llm

    result = run_attempt(conn, get_llm(), "overfitting", MESSY)
    print(f"\n===== RAW MODEL OUTPUT PER STEP (primary: {config.LLM_MODEL}) =====")
    for e in result.agent_log:
        if e["tool_name"].startswith("llm."):
            print(f"\n--- {e['tool_name']} ---\n{e['result']['raw_text']}")
    print("\n===== HEDGING =====\n" + json.dumps(result.hedging, indent=2))
    print(f"\nattempt_score={result.attempt_score}  mastery={result.state.mastery}")

    assert result.clean.claims
    assert not any("too well" in c.lower() and "noise" not in c.lower() for c in result.clean.claims)
    assert result.decision.action in {"re_explain", "ask_followup", "revisit_prerequisite", "advance", "review_due"}
