import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from app import db
from app.agent import run_attempt
from app.llm import LLMError, get_llm
from app.schemas import AttemptRequest, AttemptResult

conn = db.connect()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db(conn)
    db.seed_concepts(conn)
    yield


app = FastAPI(title="TeachBack", lifespan=lifespan)


@app.post("/attempt", response_model=AttemptResult)
def post_attempt(req: AttemptRequest):
    try:
        return run_attempt(conn, get_llm(), req.concept_id, req.transcript)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except LLMError as e:
        raise HTTPException(502, str(e))


@app.get("/concepts")
def get_concepts():
    rows = conn.execute("SELECT id, name, subject, prerequisites_json FROM concepts").fetchall()
    return [{**dict(r), "prerequisites": json.loads(r["prerequisites_json"])} for r in rows]


@app.get("/state")
def get_state():
    states = conn.execute("SELECT * FROM learner_state").fetchall()
    misc = conn.execute("SELECT * FROM misconceptions ORDER BY last_seen_at DESC").fetchall()
    return {"learner_state": [dict(r) for r in states], "misconceptions": [dict(r) for r in misc]}


@app.get("/log")
def get_log(limit: int = 50):
    rows = conn.execute("SELECT * FROM agent_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
