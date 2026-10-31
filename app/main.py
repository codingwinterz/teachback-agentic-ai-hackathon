import json
import queue
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import config, db
from app.agent import run_attempt
from app.llm import LLMError, get_llm
from app.schemas import AttemptRequest, AttemptResult

conn = db.connect()
attempt_lock = threading.Lock()  # one attempt at a time on the shared SQLite connection


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db(conn)
    db.seed_concepts(conn)
    yield


app = FastAPI(title="TeachBack", lifespan=lifespan)


@app.post("/attempt", response_model=AttemptResult)
def post_attempt(req: AttemptRequest):
    try:
        with attempt_lock:
            return run_attempt(conn, get_llm(), req.concept_id, req.transcript)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except LLMError as e:
        raise HTTPException(502, str(e))


@app.post("/attempt/stream")
def post_attempt_stream(req: AttemptRequest):
    """Server-sent events: step_start / tool / step_done as the loop runs, then result or error."""
    events: queue.Queue = queue.Queue()

    def work():
        try:
            with attempt_lock:
                result = run_attempt(conn, get_llm(), req.concept_id, req.transcript,
                                     on_event=events.put)
            events.put({"type": "result", "data": result.model_dump()})
        except KeyError as e:
            events.put({"type": "error", "status": 404, "detail": str(e)})
        except LLMError as e:
            events.put({"type": "error", "status": 502, "detail": str(e)})
        except Exception as e:  # surface anything else to the UI instead of a silent hang
            events.put({"type": "error", "status": 500, "detail": f"{type(e).__name__}: {e}"})
        finally:
            events.put(None)

    threading.Thread(target=work, daemon=True).start()

    def stream():
        while (event := events.get()) is not None:
            yield f"data: {json.dumps(event, default=str)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/health")
def health():
    """Which LLM is live, so the UI can show a FAKE badge in dev mode."""
    real = config.LLM_MODE == "real"
    return {
        "llm_mode": "real" if real else "fake",
        "provider": config.LLM_PROVIDER if real else None,
        "model_chain": ([config.LLM_MODEL] + config.LLM_FALLBACK_MODELS) if real else ["fake-dev"],
    }


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


# Mounted last so the API routes above take precedence.
app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui")
