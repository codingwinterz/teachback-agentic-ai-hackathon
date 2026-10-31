from datetime import datetime, timedelta

import pytest

from app import db, scoring
from app.agent import run_attempt
from app.fake_llm import FakeLLM
from app.session import list_due_reviews

NOW = datetime(2026, 10, 31, 12, 0, 0)


def test_good_attempt_multiplies_interval_by_ease():
    interval, ease, next_at = scoring.schedule_review(2.0, 2.5, score=0.8, now=NOW)
    assert interval == 5.0 and ease == 2.55
    assert next_at == (NOW + timedelta(days=5)).strftime(scoring.SQLITE_TIME)


def test_bad_attempt_resets_interval_to_one_day_and_lowers_ease():
    interval, ease, next_at = scoring.schedule_review(12.0, 2.5, score=0.3, now=NOW)
    assert (interval, ease) == (1.0, 2.3)
    assert next_at == "2026-11-01 12:00:00"


def test_ease_stays_within_bounds():
    assert scoring.schedule_review(1, 1.35, score=0.0, now=NOW)[1] == scoring.MIN_EASE
    assert scoring.schedule_review(1, 2.79, score=1.0, now=NOW)[1] == scoring.MAX_EASE


def test_threshold_is_inclusive_and_is_due():
    assert scoring.schedule_review(1.0, 2.5, score=scoring.GOOD_SCORE, now=NOW)[0] == 2.5
    assert scoring.is_due("2026-10-31 11:59:59", now=NOW) and scoring.is_due("2026-10-31 12:00:00", now=NOW)
    assert not scoring.is_due("2026-10-31 12:00:01", now=NOW) and not scoring.is_due(None, now=NOW)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "sched.db"))
    db.init_db(c)
    db.seed_concepts(c)
    return c


def _review(conn, cid):
    return dict(conn.execute("SELECT interval_days, ease, next_review_at FROM learner_state WHERE concept_id = ?",
                             (cid,)).fetchone())


def test_attempts_update_the_schedule(conn):
    strong = ("K-means is an unsupervised algorithm that groups data into k clusters without labels. It assigns "
              "each point to the nearest centroid and moves centroids to the mean. k is chosen in advance.")
    result = run_attempt(conn, FakeLLM(), "k_means", strong)
    r = _review(conn, "k_means")
    if result.attempt_score >= scoring.GOOD_SCORE:
        assert r["interval_days"] == 2.5
    else:
        assert r["interval_days"] == 1.0
    assert r["next_review_at"] and not scoring.is_due(r["next_review_at"])

    run_attempt(conn, FakeLLM(), "overfitting", "Um, more data always fixes it.")
    assert _review(conn, "overfitting")["interval_days"] == 1.0  # weak attempt -> tomorrow


def test_due_list_shows_only_passed_reviews(conn):
    conn.execute("UPDATE learner_state SET next_review_at = datetime('now', '-2 days') WHERE concept_id = 'overfitting'")
    conn.execute("UPDATE learner_state SET next_review_at = datetime('now', '+2 days') WHERE concept_id = 'k_means'")
    due = list_due_reviews(conn)
    assert [d["concept_id"] for d in due] == ["overfitting"] and due[0]["name"] == "Overfitting"


def test_due_endpoint_reads_the_same_data(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app import main

    monkeypatch.setattr(main, "conn", db.connect(str(tmp_path / "api.db")))
    with TestClient(main.app) as client:
        assert client.get("/reviews/due").json() == []
        client.post("/demo/sample")
        due = client.get("/reviews/due").json()
        assert {d["concept_id"] for d in due} == {d["concept_id"] for d in list_due_reviews(main.conn)}
        assert len(due) >= 3
