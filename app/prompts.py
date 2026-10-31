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

DECIDE_USER = """Concept: {concept_id} ({concept_name})
Attempt score (hedge-discounted): {score}
Hedging score: {hedging}
Learner state after update: {state}
Grade: {grade}
Prerequisite states: {prereqs}
Concepts that build on this one: {next_ids}"""
