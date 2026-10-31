import json
import queue
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import config, db, demo, graph, history, session
from app.agent import AlreadyAnswered, answer_lesson, run_attempt
from app.llm import LLMError, get_llm
from app.schemas import (
    AttemptRequest, AttemptResult, LessonAnswerRequest, LessonAnswerResult, SessionAnswerRequest, SessionView,
)
from app.session import SessionClosed

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


def _sse(job: Callable[[Callable[[dict], None]], None]) -> StreamingResponse:
    """Run job(emit) in a thread under the attempt lock and stream its events as server-sent events.
    Errors become {"type": "error", "status", "detail"} events instead of a silent hang."""
    events: queue.Queue = queue.Queue()

    def work():
        try:
            with attempt_lock:
                job(events.put)
        except KeyError as e:
            events.put({"type": "error", "status": 404, "detail": str(e)})
        except SessionClosed as e:
            events.put({"type": "error", "status": 409, "detail": str(e)})
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


@app.post("/attempt/stream")
def post_attempt_stream(req: AttemptRequest):
    """Server-sent events: step_start / tool / step_done as the loop runs, then result or error."""
    def job(emit):
        result = run_attempt(conn, get_llm(), req.concept_id, req.transcript, on_event=emit)
        emit({"type": "result", "data": result.model_dump()})

    return _sse(job)


def _session_budget() -> dict:
    """Pacing only applies to real calls, so fake-mode development stays fast."""
    return {"max_calls": config.SESSION_MAX_CALLS,
            "min_interval_s": config.SESSION_MIN_CALL_INTERVAL_S if config.LLM_MODE == "real" else 0.0}


@app.post("/session/start")
def post_session_start():
    """Server-sent events for the agent's tool loop, then {"type": "session", "data": SessionView}."""
    def job(emit):
        view = session.start_session(conn, get_llm(), on_event=emit, **_session_budget())
        emit({"type": "session", "data": view.model_dump()})

    return _sse(job)


@app.post("/session/{session_id}/answer")
def post_session_answer(session_id: int, req: SessionAnswerRequest):
    """Runs the five-step pipeline on the answer (tool-loop decide), then the next question."""
    def job(emit):
        result, view = session.answer_session(conn, get_llm(), session_id, req.transcript,
                                              on_event=emit, **_session_budget())
        if result is not None:
            emit({"type": "result", "data": result.model_dump()})
        emit({"type": "session", "data": view.model_dump()})

    return _sse(job)


@app.post("/session/{session_id}/end", response_model=SessionView)
def post_session_end(session_id: int):
    """End early; generates the study plan (one LLM call, or rule-based if nothing was answered)."""
    with attempt_lock:
        try:
            return session.end_by_user(conn, get_llm(), session_id, **_session_budget())
        except KeyError as e:
            raise HTTPException(404, str(e))


@app.get("/session/{session_id}", response_model=SessionView)
def get_session(session_id: int):
    try:
        return session.session_view(conn, session_id, config.SESSION_MAX_CALLS)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.post("/lessons/{lesson_id}/answer", response_model=LessonAnswerResult)
def post_lesson_answer(lesson_id: int, req: LessonAnswerRequest):
    try:
        with attempt_lock:
            llm = session.llm_for_lesson(conn, get_llm(), lesson_id, **_session_budget())
            return answer_lesson(conn, llm, lesson_id, req.answer)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except AlreadyAnswered as e:
        raise HTTPException(409, str(e))
    except LLMError as e:
        raise HTTPException(502, str(e))


@app.get("/health")
def health():
    """Which LLM is live, so the UI can show a FAKE badge in dev mode."""
    real = config.LLM_MODE == "real"
    return {
        "llm_mode": "real" if real else "fake",
        "provider": config.LLM_PROVIDER if real else None,
        "model_chain": ([config.LLM_MODEL] + config.LLM_FALLBACK_MODELS) if real else ["fake-dev"],
    }


@app.get("/reviews/due")
def get_due_reviews():
    """Concepts whose next review time has passed (same data the session's list_due_reviews tool reads)."""
    return session.list_due_reviews(conn)


@app.get("/concepts/{concept_id}/history")
def get_concept_history(concept_id: str):
    """Timeline of attempts for one concept: date, mastery after, misconceptions seen."""
    try:
        return history.concept_history(conn, concept_id)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/graph")
def get_graph():
    """Nodes (depth/row layout + mastery) and prerequisite edges for the SVG graph view."""
    return graph.graph_data(conn)


@app.get("/concepts")
def get_concepts():
    rows = conn.execute("SELECT id, name, subject, prerequisites_json FROM concepts").fetchall()
    return [{**dict(r), "prerequisites": json.loads(r["prerequisites_json"])} for r in rows]


@app.get("/state")
def get_state():
    states = conn.execute("SELECT * FROM learner_state").fetchall()
    misc = conn.execute("SELECT * FROM misconceptions ORDER BY last_seen_at DESC").fetchall()
    return {"learner_state": [dict(r) for r in states], "misconceptions": [dict(r) for r in misc],
            "sample_loaded": demo.sample_loaded(conn)}


@app.post("/demo/sample")
def post_demo_sample():
    """Fill a realistic sample learner (marked is_sample). No LLM calls."""
    with attempt_lock:
        try:
            return demo.load_sample(conn)
        except demo.RealDataPresent as e:
            raise HTTPException(409, str(e))


@app.post("/demo/reset")
def post_demo_reset():
    """Delete all learner and sample data. Concepts are kept. No LLM calls."""
    with attempt_lock:
        demo.reset_progress(conn)
        return {"reset": True}


@app.get("/log")
def get_log(limit: int = 50):
    rows = conn.execute("SELECT * FROM agent_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# Mounted last so the API routes above take precedence.
app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui")
