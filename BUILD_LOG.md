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
