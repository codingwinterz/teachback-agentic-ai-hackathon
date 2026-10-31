import pytest

from app import db, demo, history
from app.agent import run_attempt
from app.fake_llm import FakeLLM


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "history.db"))
    db.init_db(c)
    db.seed_concepts(c)
    return c


def test_history_replays_mastery_after_each_attempt(conn):
    first = run_attempt(conn, FakeLLM(), "overfitting", "Overfitting is fitting noise. More data always fixes it.")
    second = run_attempt(conn, FakeLLM(), "overfitting",
                         "Overfitting is when a model learns the noise in the training data, so it has low training "
                         "error but high validation error.")
    items = history.concept_history(conn, "overfitting")
    assert [h["attempt_id"] for h in items] == [first.attempt_id, second.attempt_id]
    assert items[0]["mastery_after"] == first.state.mastery
    assert items[1]["mastery_after"] == second.state.mastery  # matches the dashboard when no lesson bonus
    assert items[0]["misconceptions"] and not items[0]["is_sample"]


def test_history_for_sample_data_and_empty_concepts(conn):
    demo.load_sample(conn)
    items = history.concept_history(conn, "overfitting")
    assert len(items) == 3 and all(h["is_sample"] for h in items)
    assert items[-1]["mastery_after"] == conn.execute(
        "SELECT mastery FROM learner_state WHERE concept_id = 'overfitting'").fetchone()[0]
    assert all("thinks more data always fixes overfitting" in h["misconceptions"] for h in items)
    assert history.concept_history(conn, "boosting") == []
    with pytest.raises(KeyError):
        history.concept_history(conn, "nope")


def test_history_endpoint(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app import main

    monkeypatch.setattr(main, "conn", db.connect(str(tmp_path / "api.db")))
    with TestClient(main.app) as client:
        assert client.get("/concepts/overfitting/history").json() == []
        client.post("/demo/sample")
        body = client.get("/concepts/bias_variance/history").json()
        assert len(body) == 2 and body[0]["created_at"] < body[1]["created_at"]
        assert client.get("/concepts/nope/history").status_code == 404
