# Project: Adaptive Exam-Prep Tutor (working title: TeachBack)

Agentic AI Hackathon (Product Space x Wispr Flow). Solo build, one day.
Submission deadline: 31 Oct 2026, 11:59 PM IST. Aim to submit by 10:30 PM.

## What we're building
A tutor where the student EXPLAINS concepts out loud (dictated via Wispr Flow
into a big text box). The agent turns messy speech into structured claims,
finds misconceptions, updates a persistent learner model, and decides the
next action. It is an agent: it plans and calls tools, it does not just chat.

## Hard requirements (from the organizers)
- Built from scratch today. Fresh repo, commit often. No reuse of old projects.
- Main product input is natural speech captured through Wispr Flow.
  Do NOT build custom speech-to-text. The input is a large textarea the user
  dictates into. Input will contain filler words, digressions, self-corrections.
- Deliverables: working demo, GitHub repo, 3-minute video, short write-up
  (problem -> solution -> architecture), honest log of my work vs AI output.

## Stack
- Backend: Python 3.11+, FastAPI, SQLite (sqlite3 or SQLModel)
- LLM: Claude API with tool calling. Provider and model come from env vars
  (LLM_PROVIDER, LLM_MODEL, API key) so they can be swapped easily.
- Frontend: single-page vanilla HTML/JS (or minimal React). Keep it simple.
- Deploy: Render, Railway, or Fly. Decide by 5 PM.

## Agent loop (core)
1. INPUT: raw dictated transcript for a concept.
2. CLEAN: LLM extracts structured claims, strips fillers, resolves
   self-corrections. Also outputs a hedging score (0-1) from the raw text
   (hesitation, "I think", "um", reversals) as a confidence signal.
3. GRADE: compare claims to reference notes for that concept. Output:
   correct_claims, missing_points, misconceptions (named, e.g.
   "confuses bias with variance").
4. UPDATE learner model via tool call (mastery, misconceptions, next review).
5. DECIDE next action via tool call, one of:
   re_explain | ask_followup | revisit_prerequisite | advance | review_due
6. Return structured JSON the UI renders. Log every tool call for the demo.

## Data model (SQLite)
- concepts(id, name, subject, reference_notes, prerequisites_json)
- attempts(id, concept_id, raw_transcript, clean_claims_json,
  hedging_score, correct_json, missing_json, misconceptions_json, created_at)
- learner_state(concept_id PK, mastery REAL 0-1, confidence REAL,
  interval_days REAL, ease REAL, next_review_at, attempts_count)
- misconceptions(id, concept_id, label, count, last_seen_at, resolved BOOL)
- agent_log(id, step, tool_name, args_json, result_json, created_at)

## Mastery and scheduling (keep simple)
- mastery = moving average of (correct / (correct + missing + misconceptions)),
  discounted when hedging_score is high.
- Spaced repetition: simple SM-2 style. On good attempt interval *= ease,
  on bad attempt interval resets to 1 day. About 30 lines, no libraries.

## Tools the agent may call
- get_concept(concept_id)
- get_learner_state(concept_id)
- update_learner_state(concept_id, mastery, confidence, misconceptions)
- list_due_reviews()
- pick_next_concept(strategy)
- generate_followup_question(concept_id, misconception)

## UI (minimum)
- Big dictation textarea (focus on load, so Wispr Flow works immediately)
- Result panel: cleaned claims, correct/missing/misconceptions, next action
- Learner model dashboard: mastery bars per concept + live misconception list
- Show the agent_log trace so judges SEE the decisions

## Content
- One subject only, 15-20 concepts with short reference notes and prerequisite
  links. Seed from a JSON file. Depth over breadth.

## Rules for Claude Code
- Build in small steps. Get the text-in to learner-model-updated loop working
  end to end BEFORE any UI polish or extra features.
- Prefer boring, readable code. No auth, no multi-user, no extra subjects.
- All LLM outputs that feed code must be strict JSON validated with Pydantic.
- Commit after each working step with a clear message.
- Keep a running BUILD_LOG.md: what I decided vs what AI generated.
- Never commit API keys. Use .env and .env.example.

## Build order
1. Core loop with text input (clean -> grade -> update -> decide)
2. Seed real content (15-20 concepts)
3. Dashboard + hedging/confidence signal
4. Spaced repetition
5. Deploy, record 3-min video, README, submit
