import pytest

from app import db, graph


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "graph.db"))
    db.init_db(c)
    db.seed_concepts(c)
    return c


def test_graph_layout_by_prerequisite_depth(conn):
    data = graph.graph_data(conn)
    nodes = {n["id"]: n for n in data["nodes"]}
    assert len(nodes) == 18
    assert nodes["supervised_learning"]["depth"] == 0
    assert nodes["overfitting"]["depth"] == 2  # supervised -> train_test_split -> overfitting
    assert nodes["regularization"]["depth"] == 4  # longest chain through linear regression
    assert nodes["backpropagation"]["depth"] == 6 and data["columns"] == 7
    # every prerequisite sits in an earlier column, and no two nodes share a slot
    for e in data["edges"]:
        assert nodes[e["from"]]["depth"] < nodes[e["to"]]["depth"]
    assert len({(n["depth"], n["row"]) for n in data["nodes"]}) == 18
    assert data["max_rows"] == max(n["row"] for n in data["nodes"]) + 1


def test_graph_edges_and_mastery(conn):
    conn.execute("UPDATE learner_state SET mastery = 0.55, attempts_count = 2 WHERE concept_id = 'overfitting'")
    data = graph.graph_data(conn)
    assert {"from": "overfitting", "to": "bias_variance"} in data["edges"]
    assert len(data["edges"]) == sum(len(c["prerequisites"]) for c in __import__("json").load(
        open("data/concepts.json", encoding="utf-8")))
    node = next(n for n in data["nodes"] if n["id"] == "overfitting")
    assert node["mastery"] == 0.55 and node["attempts"] == 2
