"""Prerequisite graph data: nodes laid out by prerequisite depth (columns) and order (rows)."""

import sqlite3

from app.session import _concepts, _states, concept_depths


def graph_data(conn: sqlite3.Connection) -> dict:
    concepts = _concepts(conn)
    states = _states(conn)
    depths = concept_depths(concepts)
    rows: dict[int, int] = {}
    nodes = []
    for c in concepts:  # JSON order within each column keeps related concepts near each other
        d = depths[c["id"]]
        s = states.get(c["id"], {})
        nodes.append({"id": c["id"], "name": c["name"], "depth": d, "row": rows.get(d, 0),
                      "mastery": s.get("mastery", 0.0), "attempts": s.get("attempts_count", 0)})
        rows[d] = rows.get(d, 0) + 1
    edges = [{"from": p, "to": c["id"]} for c in concepts for p in c["prerequisites"]]
    return {"nodes": nodes, "edges": edges, "columns": max(depths.values()) + 1,
            "max_rows": max(rows.values())}
