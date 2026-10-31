# TeachBack

An exam-prep tutor where **you explain a concept out loud** and an AI agent works out what you
actually understand. It turns messy dictated speech into claims, grades them against reference
notes, names your misconceptions, updates a learner model, and decides what to teach or ask next.

Built in one day for the Agentic AI Hackathon (Product Space x Wispr Flow). One subject: 18 machine
learning fundamentals concepts, from supervised learning to backpropagation.

## The problem

Re-reading notes and recognising the right multiple-choice answer feel like learning but test very
little. Explaining a concept in your own words ("teach-back") shows what you really know, but
nobody is around to listen, notice the gap, or catch the specific wrong idea, such as "more data
always fixes overfitting".

TeachBack is that listener. It accepts the way people actually talk ("um, it's when the model, no
wait, when it fits the noise"), tracks *which* misconceptions keep coming back, and plans the
next question from your learner model instead of a fixed script.

## What it does

- **Single attempt:** pick a concept, dictate an explanation, and watch a live trace of the five
  pipeline steps with the model and seconds for each.
- **Results:** cleaned claims, your self-corrections, a hesitation score, correct points, missing
  points, and named misconceptions with repeat counts, then the next action and a follow-up question.
- **Mini-lessons:** when you get something wrong, a 3–4 sentence lesson built from the reference
  notes, one example, and a check question. A correct answer counts toward mastery and clears the
  misconception.
- **Study sessions:** an agent chooses its own tools to pick 5 questions in a row. It explains each
  choice ("Why this next?") and walks back to a prerequisite when you fail a concept twice. It ends
  with a 3–5 step study plan you can copy.
- **Learner dashboard:** mastery per concept, open misconceptions, a "Review today" list (spaced
  repetition), an attempt history timeline, and a prerequisite graph coloured by mastery that shows
  the session's walked path.
- **Demo tools:** load a clearly marked sample learner, or reset all progress.

## How Wispr Flow is used

Speech is the main input, and TeachBack does **no speech-to-text of its own**. The page opens with
a very large text box already focused, so you can start dictating with
[Wispr Flow](https://wisprflow.ai) straight away. The check-question box for lessons works the same way.

The pipeline is built for dictated text rather than typed text:

- **Clean** keeps your final position when you correct yourself ("X, no wait, Y" becomes Y, and
  the correction is recorded) and drops filler and digressions.
- **Hesitation is a signal, not noise.** Fillers ("um", "you know"), hedges ("I think", "I guess")
  and reversals ("no wait") are counted in code and averaged 50/50 with the model's own hesitation
  score. A hesitant correct answer counts for less than a confident one.

## Architecture

```
 dictated text ──► POST /attempt/stream (server-sent events) ──► live trace in the browser
                         │
   1 CLEAN ─► 2 GRADE ─► 3 UPDATE ─► 4 DECIDE ─► 5 TEACH (only if needed)
    LLM        LLM        code        Decider       LLM
                          │              │
                    learner model    SingleShotDecider (one call)
                    (SQLite)         or ToolLoopDecider (session agent, tool loop)
```

FastAPI and SQLite on the backend; a single-page vanilla HTML/CSS/JS frontend with no build step.
Every tool call, its arguments and result, the model that answered and the seconds taken are
written to `agent_log` and streamed to the UI.

### The five-step pipeline (`app/agent.py`)

| Step | Done by | Output |
|---|---|---|
| 1. Clean | LLM (`CleanResult`) | Atomic claims, self-corrections, model hesitation score; blended with the code-counted hedge score |
| 2. Grade | LLM (`GradeResult`) | Correct claims, at most 3 core missing points, named misconceptions (existing labels reused so counts accumulate) |
| 3. Update | **Code** | Mastery, confidence, misconception counts, next review date |
| 4. Decide | A `Decider` | `re_explain`, `ask_followup`, `revisit_prerequisite`, `advance` or `review_due`, with a reason and a question |
| 5. Teach | LLM (`Lesson`) | Mini-lesson, example and check question, only after `re_explain` or a misconception |

**The LLM judges; code does the arithmetic.** Every LLM output that feeds code is a Pydantic
model, requested through one interface, `llm.structured(system, user, Schema)`, and validated, with
one retry on validation failure.

### The session agent (`app/session.py`)

`ToolLoopDecider` plugs into the same `Decider` seam as the single-call decider. Each round the
model returns an `AgentStep`: either up to 3 tool calls, or its final choice (next concept,
question, and a one-sentence "why").

| Tool | What it returns |
|---|---|
| `get_learner_state(concept_id)` | Mastery, confidence, attempts, next review, open misconceptions |
| `list_due_reviews()` | Concepts whose review date has passed |
| `list_weak_concepts()` | Attempted concepts with low mastery |
| `pick_next_concept(strategy)` | `weakest`, `due` or `prerequisite_gap` |
| `get_prerequisites(concept_id)` | Direct prerequisites and their mastery |
| `generate_followup_question(concept_id, misconception)` | A question built from the reference notes |

The tools are database reads, so they cost no model requests; a decision usually costs 2. Guard rails:

- **At most 6 tool calls per decision.** If the loop exceeds that, or returns invalid output, it
  falls back to `SingleShotDecider`, so a session never breaks.
- **The walk-back rule is enforced in code.** After two attempts in a session with mastery below
  0.4, the next question goes to the weakest prerequisite, with a one-sentence reason, even if the
  model chose otherwise. When that happens it is logged as `policy.walk_back`.
- **Quota safety:** 5 questions per session, and a per-session budget (default 30 requests) that
  also counts provider retries and lesson checks. Requests are spaced 5 seconds apart in real mode
  (at most 12 per minute). If the budget or quota runs out, the session ends with a message and a
  rule-based study plan, without crashing.

### The learner model

- **Mastery** per concept: an attempt scores `correct / (correct + 0.5 × missing + misconceptions)`,
  discounted by up to 50% for hesitation, then folded into a moving average (alpha 0.5).
- **Confidence:** a moving average of 1 − hesitation.
- **Misconceptions:** named labels with counts per concept. They stay open until a lesson check
  answer is correct.
- **Spaced repetition (simple SM-2 style):** an attempt scoring 0.6 or more multiplies the review
  interval by the ease factor; a weaker attempt resets it to 1 day. Ease stays between 1.3 and 2.8.
- **Prerequisite graph:** 18 concepts form a DAG (`data/concepts.json`). It drives walk-backs and
  the graph view.

### Models and the LLM layer (`app/llm.py`)

- **Provider:** Gemini free tier through the Google GenAI SDK, with JSON-schema structured output.
  An Anthropic Claude provider is also implemented behind `LLM_PROVIDER=anthropic`.
- **Fallback chain:** `gemini-3.5-flash-lite` → `gemini-3.1-flash-lite` → `gemini-3.6-flash` →
  `gemini-3.8-flash`.
- **Retries:** backoff on 429/5xx, and a 12-second per-call timeout that counts as retryable.
- **Daily quota:** a model whose daily quota is gone is skipped until its quota resets, instead of
  waiting on Gemini's suggested retry delay (we saw 20 hours).
- **Fake mode** (`LLM_MODE=fake`, the default for local development) uses an offline fake LLM that
  reads the same prompts and returns deterministic answers, so the whole app and test suite run
  with **zero API calls**.

### Data (SQLite)

`concepts`, `attempts`, `learner_state`, `misconceptions`, `lessons`, `sessions`, `session_steps`
and `agent_log`. New columns are added by small startup migrations, so an existing database and a
fresh deploy both work. Sample data is marked with `is_sample = 1`.

## Setup

Requires Python 3.11 or newer (developed on 3.15; Render uses 3.14).

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then put your own key in .env, which is gitignored
python -m app               # http://127.0.0.1:8000
```

The app starts in **fake mode**, with no API calls. To use the real model, set `LLM_MODE=real` and
a `GEMINI_API_KEY` (free from Google AI Studio) in `.env`.

```bash
pytest                                   # full suite, fake mode, no network
LLM_MODE=real pytest -s -k real          # one real end-to-end attempt (spends ~3-5 requests)
```

Some UI tests run the browser code in Node.js and are skipped if Node isn't installed.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `LLM_MODE` | `fake` | `fake` = offline fake LLM; `real` = call the provider |
| `LLM_PROVIDER` | `gemini` | `gemini` or `anthropic` |
| `LLM_MODEL` | `gemini-3.5-flash-lite` | Primary model |
| `LLM_FALLBACK_MODELS` | `gemini-3.1-flash-lite,gemini-3.6-flash,gemini-3.8-flash` | Tried in order when the primary keeps failing or is out of quota |
| `GEMINI_API_KEY` | none | Your Gemini key (never commit it) |
| `LLM_CALL_TIMEOUT_S` | `12` | Per-request timeout; a timeout is retried |
| `SESSION_MAX_CALLS` | `30` | Request budget per study session |
| `SESSION_MIN_CALL_INTERVAL_S` | `5` | Seconds between session requests (real mode only) |
| `FAKE_LLM_DELAY_S` | `0.6` | Simulated latency per fake call, so the trace visibly steps through |
| `DB_PATH` | `teachback.db` | SQLite file |
| `CONCEPTS_PATH` | `data/concepts.json` | Seed content |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Server address (`render.yaml` sets `HOST=0.0.0.0`) |
| `ANTHROPIC_API_KEY`, `LLM_EFFORT` | none, `medium` | Only for `LLM_PROVIDER=anthropic` |

### Deploy on Render

`render.yaml` is a Render Blueprint (free web service, start command `python -m app`, health check
`/health`). In the Render dashboard choose **New → Blueprint**, pick this repo, and enter
`GEMINI_API_KEY` when asked. It runs with `LLM_MODE=real`.

Render's free disk is temporary, so the database starts empty after every deploy or restart. Use
**Load sample learner** before a demo.

### Cost per use (real mode)

Measured with the fake LLM, where one fake call equals one real request:

| What | Requests |
|---|---|
| One attempt | 3–5 (the lesson adds one) |
| A full 5-question session | about 22–27 (more if the provider retries; capped by the budget) |
| Lesson check answer | +1 each |

The Gemini free tier gives a small daily request allowance per model (about 20 a day for
`gemini-3.6-flash` when we checked). The fallback chain spreads usage across models.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/attempt` | One attempt; returns the full result as JSON |
| POST | `/attempt/stream` | Same, as server-sent step events (used by the UI) |
| POST | `/lessons/{id}/answer` | Answer a lesson's check question |
| POST | `/session/start` | Start a study session (streams the agent's tool calls) |
| POST | `/session/{id}/answer` | Answer the current session question (streams) |
| POST | `/session/{id}/end` | End early and get the study plan |
| GET | `/session/{id}` | Session state, steps and plan |
| GET | `/state`, `/concepts`, `/graph`, `/reviews/due`, `/concepts/{id}/history`, `/log`, `/health` | Dashboard data |
| POST | `/demo/sample`, `/demo/reset` | Demo tools (no LLM calls) |

## Known limitations

- **The reference notes were written by AI** and are what the grader treats as correct. They should
  be reviewed by someone who knows the subject.
- **One learner, no accounts.** Anyone with the deployed link shares one database and one quota.
- **The free-tier quota is the real bottleneck.** On a busy day a session can stop early (gracefully,
  with a plan).
- **The fake LLM's wording is rougher than the real model's** (for example its misconception
  labels). It exists so the app can be developed and tested without spending quota.
- **"Mastery after" in the history is replayed from attempts,** so it leaves out lesson-check bonuses.

## What I built vs what Claude Code generated

This project was built with [Claude Code](https://claude.com/claude-code). In honesty: **Claude
Code wrote nearly all of the code, tests, prompts and seed content; my work was the product, the
spec, the decisions, and checking the results.** [`BUILD_LOG.md`](BUILD_LOG.md) records, step by
step, what I decided and what was AI-generated.

**What I did**

- Wrote the product idea and spec (`CLAUDE.md`): teach-back from dictated speech, the
  clean → grade → update → decide loop, the data model, the action set and the build order.
- Made the product and engineering calls along the way, for example:
  - one subject (ML fundamentals) and its prerequisite chain;
  - hesitation as a code + model blend;
  - mastery computed in code;
  - switching to Gemini's free tier;
  - the model fallback order;
  - capping missing points and weighting them at half;
  - low thinking on clean and decide only;
  - a 12-second timeout;
  - fake mode by default to protect quota;
  - the teaching step;
  - the tool-using session agent with the walk-back rule, a 5-question cap, a 30-request budget
    and 5-second pacing;
  - pinning real mode on Render.
- Set the quality bar: raw model output reviewed before commits, a stop if any call took over 10
  seconds without retries, and phone-width and console checks.
- Ran the deliberate real-model checks, found and reported issues (for example, the literal "null"
  follow-up question), reviewed each step, and handled the repo, keys and deployment.

**What Claude Code generated** (and I reviewed)

- The application code: FastAPI app, pipeline, session agent and tools, LLM layer with fallback and
  quota handling, fake LLM, scoring, migrations, and the frontend (HTML/CSS/JS, the SVG graph).
- The prompts, Pydantic schemas, the 18 concepts' reference notes, and the 99 tests.
- Debugging found while testing, for example:
  - the SDK turning timeouts into HTTP 504;
  - an uncapped 20-hour retry sleep;
  - a double-submit that would have spent quota twice;
  - CSS that let the graph widen the phone page.
- The Render configuration and this README.

## License

MIT. See [LICENSE](LICENSE).
