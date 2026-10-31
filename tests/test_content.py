import json

import pytest

from app import config, db
from app.agent import run_attempt
from app.fake_llm import FakeLLM

with open(config.CONCEPTS_PATH, encoding="utf-8") as f:
    CONCEPTS = json.load(f)
BY_ID = {c["id"]: c for c in CONCEPTS}


def test_size_and_single_subject():
    assert 15 <= len(CONCEPTS) <= 20
    assert len(BY_ID) == len(CONCEPTS), "duplicate concept ids"
    assert {c["subject"] for c in CONCEPTS} == {"ML Fundamentals"}


def test_prerequisites_exist_and_form_a_dag():
    for c in CONCEPTS:
        for p in c["prerequisites"]:
            assert p in BY_ID, f"{c['id']} -> unknown prerequisite {p}"
            assert p != c["id"]

    visiting, done = set(), set()

    def visit(cid, path):
        assert cid not in visiting, f"cycle: {' -> '.join(path + [cid])}"
        if cid in done:
            return
        visiting.add(cid)
        for p in BY_ID[cid]["prerequisites"]:
            visit(p, path + [cid])
        visiting.discard(cid)
        done.add(cid)

    for c in CONCEPTS:
        visit(c["id"], [])


def test_core_chain_is_preserved():
    assert "overfitting" in BY_ID["bias_variance"]["prerequisites"]
    assert "bias_variance" in BY_ID["regularization"]["prerequisites"]


def test_reference_notes_are_bulleted_and_short():
    for c in CONCEPTS:
        lines = c["reference_notes"].splitlines()
        assert all(line.startswith("- ") for line in lines), c["id"]
        assert 5 <= len(lines) <= 9, f"{c['id']}: {len(lines)} bullets"
        assert len(c["reference_notes"]) <= 1200, c["id"]


def test_seed_is_idempotent_and_updates(tmp_path):
    conn = db.connect(str(tmp_path / "seed.db"))
    db.init_db(conn)
    assert db.seed_concepts(conn) == len(CONCEPTS)
    conn.execute("UPDATE learner_state SET mastery = 0.9 WHERE concept_id = 'overfitting'")
    db.seed_concepts(conn)  # re-seed must not reset learner progress
    assert conn.execute("SELECT COUNT(*) FROM concepts").fetchone()[0] == len(CONCEPTS)
    assert conn.execute("SELECT COUNT(*) FROM learner_state").fetchone()[0] == len(CONCEPTS)
    assert conn.execute(
        "SELECT mastery FROM learner_state WHERE concept_id = 'overfitting'"
    ).fetchone()[0] == 0.9


@pytest.mark.parametrize("concept_id", list(BY_ID))
def test_loop_runs_on_every_concept_with_fake_llm(tmp_path, concept_id):
    conn = db.connect(str(tmp_path / "loop.db"))
    db.init_db(conn)
    db.seed_concepts(conn)
    first_bullet = BY_ID[concept_id]["reference_notes"].splitlines()[0][2:]
    transcript = f"Um, so I think {first_bullet[0].lower()}{first_bullet[1:]}"

    result = run_attempt(conn, FakeLLM(), concept_id, transcript)

    assert result.clean.claims
    assert result.grade.correct_claims or result.grade.misconceptions
    assert len(result.state.concept_id) and result.state.attempts_count == 1
    assert result.decision.action in {
        "re_explain", "ask_followup", "revisit_prerequisite", "advance", "review_due"
    }
