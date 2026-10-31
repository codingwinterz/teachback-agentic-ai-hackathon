# Build Log

Honest record of what I decided vs what the AI (Claude Code) generated.

## Step 1: Core loop (clean -> grade -> update -> decide)

**I decided**
- Subject: ML fundamentals, 3 concepts chained overfitting -> bias-variance -> regularization.
- A messy-transcript test (fillers, "I think", mid-sentence self-correction), run against the real model, with raw output reviewed before committing.
- Hedging is a blend: code-counted fillers/hedges/reversals averaged 50/50 with the LLM's score.
- Step 4 (decide) keeps a clean seam for a future tool-use loop, not built yet.

**AI generated / proposed (I approved)**
- FastAPI + plain sqlite3 layout, Pydantic schemas, prompts, the 3 seed concepts' reference notes.
- Structured outputs via `messages.parse(output_format=PydanticModel)` instead of forced tool use (forced `tool_choice` is rejected by current Claude models).
- Mastery computed in code, not by the LLM: correct/(correct+missing+misconceptions), discounted up to 50% by hedging, moving average with alpha 0.5.
- Hedge weights (filler 1, hedge 2, reversal 2, scaled x4 per word, capped at 1). Tunable, picked by feel.
- `run_id` column added to agent_log to group one attempt's entries.
- `Decider` protocol with `SingleShotDecider`; a future `ToolLoopDecider` slots in.

### Provider switch: Anthropic -> Gemini free tier
**I decided**
- No Anthropic key, so use Gemini's free tier via the Google GenAI SDK; key from `GEMINI_API_KEY`, model from `LLM_MODEL`.
- Keep `structured(system, user, schema)` identical so `agent.py` is untouched; keep one validation retry; add backoff for rate limits in `llm.py`.

**AI generated / proposed (I approved)**
- Model `gemini-3.8-flash`: newest stable Flash on ai.google.dev models page, listed free-of-charge on the pricing page (checked 2026-10-31).
- `response_json_schema=Model.model_json_schema()` + `response_mime_type="application/json"`, validated client-side with `model_validate_json`.
- Backoff: retries 429/500/503 up to 5 times, honoring Gemini's `retryDelay` hint when present, else 2s * 2^n capped at 60s, plus jitter.
- Kept `AnthropicLLM` behind `LLM_PROVIDER=anthropic` so the provider stays swappable per CLAUDE.md.

### Real-model run + fixes
**Observed:** `gemini-3.8-flash` returned 503 "high demand" on every try (2 runs, ~72s each). Same test on `gemini-3.6-flash` passed: self-correction resolved, misconception "thinks more data always fixes overfitting" caught, ask_followup chosen. But mastery was only 0.258 because the grader listed 4 long missing points.

**I decided**
- Default model `gemini-3.6-flash`; fallback chain from `LLM_FALLBACK_MODELS` env var when the primary keeps failing.
- Grader lists at most 3 core missing points; missing points weigh 0.5 vs correct claims and misconceptions at 1.
- Leave the hedge scale (x4) alone for now even though code hedge (0.75) reads higher than the LLM's (0.5-0.6).

**AI generated / proposed (I approved)**
- Fallback: 3 tries per model (backoff between), then next model; non-retryable 4xx fail immediately, no fallback. Default fallback `gemini-3.5-flash-lite`.
- Disabled the Google SDK's built-in retries so backoff + fallback live only in `llm.py`.
- `max_length=3` on `missing_points` so the cap is enforced by the schema (Gemini enforces maxItems) and by Pydantic, not just the prompt.

**Result:** same messy transcript, mastery 0.258 -> 0.398 (3 correct, 2 missing, 1 misconception; blended hedge 0.673).

### Latency instrumentation
**I decided:** record model name and elapsed seconds per step in agent_log and the API response; flag any single call over 10s without retries before changing anything.

**AI generated (I approved):** `model`/`elapsed_s` columns (+ startup migration), `step_timings`/`total_s` in the response, and per-LLM-call `elapsed_s` vs `last_call_s` plus failed attempts, so slow calls and retries can be told apart.

**Observed (2 runs, gemini-3.6-flash):** run 1 = 21.3s (clean 8.2, grade 4.6, decide 8.6, no retries). Run 2 = 65.0s: clean 26.0s single call with no retries, decide 33.7s (two 503s, final call 15.8s). Fallback never fired.

### Latency + quota fixes
**I decided:** low thinking on clean and decide only (grade untouched); 12s per-call timeout that counts as retryable; target under ~15s per attempt, else consider merging clean + grade.

**AI generated (I approved):**
- `thinking_level=LOW` (Gemini 3.x uses levels, not budgets), `HttpOptions(timeout=12000)` per request.
- Found while testing: the SDK sends the timeout as a server deadline, so slow calls return HTTP 504; made 504 retryable.
- Found while testing: gemini-3.6-flash free tier = 20 requests/day; a daily-quota 429 says `retryDelay: 72629s` and the backoff slept on it (uncapped). Now a hint > 60s marks the model exhausted until then and falls through immediately; `get_llm()` is a shared instance so this persists across requests.

**Observed (3 runs, all served by gemini-3.5-flash-lite after 3.6 quota ran out):** 3.57s / 3.79s / 3.97s total; mastery 0.344 / 0.306 / 0.389. Under the 15s target, so no clean+grade merge.

### Model chain + dev mode
**I decided**
- Quota is now the scarce resource. Primary `gemini-3.5-flash-lite`, fallback `gemini-3.6-flash` then `gemini-3.8-flash`.
- Spend one real attempt to confirm Flash-Lite still catches the misconception: it did ("thinks more data always fixes overfitting", same label as 3.6), and the self-corrected claim counted as correct. Mastery 0.317 (it merged two claims and used all 3 missing slots).
- Develop against a fake LLM by default; real calls only for deliberate checks, recording, Demo Day.

**AI generated (I approved)**
- `LLM_MODE=fake|real` (default fake). `app/fake_llm.py` parses the real prompts and returns deterministic outputs (sentence claims, reversal handling, absolute words -> misconception, low-overlap reference bullets -> missing points, rule-based decision).
- Real-model test now opt-in via `LLM_MODE=real` (before, any `pytest` with a key spent quota). `GET /health` reports the mode for a UI badge.

**Still to do by me:** read per-model daily limits at aistudio.google.com/rate-limit (not in the public docs).

### Fallback chain reorder
**I decided:** gemini-3.5-flash-lite -> gemini-3.1-flash-lite -> gemini-3.6-flash -> gemini-3.8-flash (3.6/3.8 last: out of daily quota). Keep exhausted-model skipping.

## Step 2: Content (18 concepts)
**I decided:** one subject (ML fundamentals), 15-20 concepts, short reference notes, prerequisite links, seeded from JSON; no real model calls while building it.

**AI generated (needs my review for accuracy):**
- 18 concepts: supervised learning, train/val/test split, loss function, gradient descent, linear regression, logistic regression, overfitting, bias-variance, regularization, cross-validation, classification metrics, feature scaling, decision trees, bagging/random forests, boosting, k-means, neural networks, backpropagation.
- Prerequisite DAG; kept the overfitting -> bias_variance -> regularization chain, and added overfitting <- train_test_split and regularization <- linear_regression.
- 6-8 bullet reference notes each, most ending in a "Common confusion" bullet so the grader has explicit misconceptions to match.
- Content tests: size, unique ids, prerequisites exist, no cycles, bullet format, idempotent re-seed that keeps learner progress, and the fake-LLM loop on every concept.

## Step 3: UI + dashboard (fake mode only, no real calls)
**I decided:** single page, vanilla HTML/JS, no build step: concept picker, very large dictation textarea focused on load, live trace of clean/grade/update/decide with model + seconds, result panel (claims, correct, missing, misconceptions with counts, next action + follow-up), mastery bar per concept, open misconceptions, fake/real badge from /health.

**AI generated (I approved):**
- `POST /attempt/stream` (server-sent events): the agent emits step_start / tool / step_done events through an `on_event` hook on AgentLogger; a lock keeps one attempt at a time on the shared SQLite connection.
- `FAKE_LLM_DELAY_S` (0.6s per fake call, server only) so the trace visibly steps through in dev mode.
- Design: ruled-notebook dictation sheet (Literata) as the hero, Atkinson Hyperlegible for UI, highlighter yellow for misconceptions, sage for correct.
- Found in browser testing: a repeated Ctrl+Enter started a second attempt (3 recorded for 2 submits); in real mode that spends quota twice. Added a busy guard + ignore key repeat; verified 3 rapid submits -> 1 attempt.
