import pytest

from app import config, db, demo, scoring
from app.agent import run_attempt
from app.fake_llm import FakeLLM
from app.session import list_due_reviews


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "demo.db"))
    db.init_db(c)
    db.seed_concepts(c)
    return c


def test_load_sample_fills_marked_realistic_data(conn):
    out = demo.load_sample(conn)
    assert out["concepts"] == 8 and out["attempts"] >= 8
    states = {r["concept_id"]: dict(r) for r in conn.execute("SELECT * FROM learner_state WHERE is_sample = 1")}
    assert len(states) == 8
    assert all(0 < s["mastery"] < 1 and s["attempts_count"] > 0 and s["next_review_at"] for s in states.values())
    assert states["supervised_learning"]["mastery"] > states["bias_variance"]["mastery"]  # a realistic spread
    misc = {r["label"]: r["count"] for r in conn.execute("SELECT * FROM misconceptions WHERE is_sample = 1")}
    assert misc["thinks more data always fixes overfitting"] == 3 and misc["confuses bias with variance"] == 2
    assert len(list_due_reviews(conn)) >= 3  # some sample reviews are overdue
    assert demo.sample_loaded(conn)
    assert conn.execute("SELECT COUNT(*) FROM attempts WHERE is_sample = 0").fetchone()[0] == 0


def test_sample_mastery_matches_a_replay_of_its_attempts(conn):
    demo.load_sample(conn)
    attempts, _ = demo.SAMPLE["overfitting"]
    m = 0.0
    for i, (correct, missing, labels, hedge) in enumerate(attempts):
        m = scoring.next_mastery(m, i, scoring.attempt_score(demo._attempt_payload(correct, missing, labels), hedge))
    stored = conn.execute("SELECT mastery FROM learner_state WHERE concept_id = 'overfitting'").fetchone()[0]
    assert stored == m


def test_load_sample_twice_replaces_it_but_refuses_over_real_data(conn):
    demo.load_sample(conn)
    demo.load_sample(conn)
    labels = {(cid, l) for cid, (atts, _) in demo.SAMPLE.items() for (_, _, ls, _) in atts for l in ls}
    assert conn.execute("SELECT COUNT(*) FROM misconceptions").fetchone()[0] == len(labels)  # no duplicates
    n_attempts = sum(len(atts) for atts, _ in demo.SAMPLE.values())
    assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == n_attempts
    run_attempt(conn, FakeLLM(), "k_means", "K-means groups points into k clusters around centroids.")
    with pytest.raises(demo.RealDataPresent):
        demo.load_sample(conn)


def test_reset_deletes_learner_and_sample_data_but_keeps_concepts(conn):
    demo.load_sample(conn)
    run_attempt(conn, FakeLLM(), "overfitting", "Overfitting is fitting noise. More data always fixes it.")
    conn.execute("INSERT INTO sessions DEFAULT VALUES")
    demo.reset_progress(conn)
    for table in ("attempts", "misconceptions", "lessons", "sessions", "session_steps", "agent_log"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    assert conn.execute("SELECT COUNT(*) FROM concepts").fetchone()[0] == 18
    row = conn.execute("SELECT MAX(mastery), MAX(attempts_count), MAX(is_sample), COUNT(*) FROM learner_state").fetchone()
    assert tuple(row) == (0, 0, 0, 18)
    assert not demo.sample_loaded(conn)


def test_demo_endpoints_never_call_the_llm(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app import main

    def no_llm():
        raise AssertionError("demo endpoints must not use the LLM")

    monkeypatch.setattr(main, "get_llm", no_llm)
    monkeypatch.setattr(main, "conn", db.connect(str(tmp_path / "api.db")))
    with TestClient(main.app) as client:
        assert client.get("/state").json()["sample_loaded"] is False
        assert client.post("/demo/sample").json()["concepts"] == 8
        assert client.get("/state").json()["sample_loaded"] is True
        assert client.post("/demo/reset").json() == {"reset": True}
        assert client.get("/state").json()["sample_loaded"] is False


def test_migration_adds_is_sample_to_an_old_database(tmp_path):
    c = db.connect(str(tmp_path / "old.db"))
    c.executescript("""CREATE TABLE learner_state (concept_id TEXT PRIMARY KEY, mastery REAL DEFAULT 0);
                       INSERT INTO learner_state (concept_id) VALUES ('x');""")
    db.init_db(c)
    assert c.execute("SELECT is_sample FROM learner_state WHERE concept_id = 'x'").fetchone()[0] == 0
