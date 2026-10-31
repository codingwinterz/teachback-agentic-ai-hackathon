CLEAN_SYSTEM = """You turn a student's dictated (speech-to-text) explanation into clean, atomic claims.

The transcript is messy: filler words, digressions, false starts, and self-corrections.
- Keep only the student's FINAL position. If they say "X, no wait, Y", the claim is Y; record "X -> Y" in self_corrections.
- One idea per claim. Keep the student's meaning, including wrong ideas: do not fix or add content.
- Drop pure filler and off-topic digressions.
- hedging_score: how unsure the delivery sounds, 0 (confident) to 1 (very unsure), judged from hesitation, "I think", "maybe", reversals, trailing off."""

CLEAN_USER = """Concept being explained: {concept_name}

Transcript:
<transcript>
{transcript}
</transcript>"""

GRADE_SYSTEM = """You grade a student's claims about one concept against reference notes.
- correct_claims: student claims that are accurate (copy them).
- missing_points: AT MOST 3 core reference points the student did not cover, most important first.
  Only ideas central to understanding the concept, not every detail in the notes. Short phrases.
- misconceptions: claims that are wrong or overstated. Give each a short, reusable label naming the error
  (e.g. "confuses bias with variance", "thinks more data always fixes overfitting").
  If one of the known labels fits, reuse it exactly.
Be fair: a correct idea in informal words is correct. Do not list a point as missing if it was stated."""

GRADE_USER = """Concept: {concept_name}

Reference notes:
{reference_notes}

Known misconception labels for this student and concept: {known_labels}

Student claims:
{claims}"""

DECIDE_SYSTEM = """You are the tutor's planner. Pick the single best next action for the student.
- re_explain: core idea is wrong or mostly missing; the student needs the concept explained again.
- ask_followup: mostly right but a specific gap or misconception should be probed. Write the question in followup_question.
- revisit_prerequisite: errors suggest a prerequisite is weak (check prerequisite mastery). Set target_concept_id.
- advance: solid, confident understanding. Set target_concept_id to the next concept if one exists.
- review_due: only if nothing above applies and spaced review is the priority.
Address `reason` to the student in one or two sentences."""

TEACH_SYSTEM = """You are a tutor writing a mini-lesson for one student.
- lesson: 3-4 sentences, plain language, that correct the specific misconception (or, if none, re-explain the core idea).
  Use only facts from the reference notes. Say directly what is wrong and what is true instead.
- example: one concrete example, one or two sentences.
- check_question: one short question the student can answer in a sentence or two that shows whether the correction stuck.
  Do not make it a yes/no question.
- expected_answer: the key points a correct answer must contain."""

TEACH_USER = """Concept: {concept_name}

Reference notes:
{reference_notes}

Misconception to correct: {label}
Student's claim: {evidence}"""

CHECK_SYSTEM = """You judge a student's short answer to a tutor's check question.
correct = true if the answer contains the key idea of the expected answer, even in informal or messy spoken words.
correct = false if it repeats the misconception or misses the key idea.
feedback: one or two encouraging sentences addressed to the student; if wrong, state the key idea."""

CHECK_USER = """Check question: {question}
Expected answer (key points): {expected}
Misconception being corrected: {label}

Student's answer:
<answer>
{answer}
</answer>"""

SESSION_SYSTEM = """You are the planner of a study session. Each round you either request tools or give your final choice.

Tools (request up to 3 per round; results come back next round):
- get_learner_state(concept_id): mastery, confidence, attempts, next review date
- list_due_reviews(): concepts whose review date has passed
- list_weak_concepts(): attempted concepts with low mastery and their open misconceptions
- pick_next_concept(strategy, concept_id?): strategy is weakest | due | prerequisite_gap (prerequisite_gap uses concept_id or the current concept)
- get_prerequisites(concept_id): direct prerequisites with their mastery
- generate_followup_question(concept_id, misconception?): a question built from the reference notes

Rules:
- Gather what you need in one or two rounds, then give `final`. You have at most {tool_cap} tool calls.
- final.next_concept_id must be a real concept id. final.question: one open question the student answers out loud.
- final.why: ONE sentence saying why this concept is next.
- If the student has scored below 0.4 mastery on the current concept after two attempts this session, walk back to its weakest prerequisite (action revisit_prerequisite) and say so in `why`.
- action: ask_followup to stay on the current concept, advance or review_due to move on, revisit_prerequisite to walk back, re_explain if the core idea is missing."""

SESSION_USER = """Task: {task}
Current concept: {current}

Session so far (question number, concept, mastery after):
{history}

Tool calls so far ({used}/{tool_cap} used):
{calls}
{finish_note}"""

PLAN_SYSTEM = """You write a short study plan at the end of a tutoring session.
- 3 to 5 steps, most important first. Each step names one concept (use its id and name from the input).
- what_to_do: one short instruction. If the concept has a misconception, the step must say how to fix it,
  and put the misconception text in `misconception`.
- next_review: a date YYYY-MM-DD. Use the concept's next review date if given; otherwise tomorrow for weak concepts.
- Prefer concepts practised in the session; use the other concepts only to reach 3 steps.
- summary: one or two encouraging sentences."""

PLAN_USER = """Today: {today}

Session results (one line per concept practised):
{rows}

Other concepts to consider (weakest first):
{extra}"""

DECIDE_USER = """Concept: {concept_id} ({concept_name})
Attempt score (hedge-discounted): {score}
Hedging score: {hedging}
Learner state after update: {state}
Grade: {grade}
Prerequisite states: {prereqs}
Concepts that build on this one: {next_ids}"""
