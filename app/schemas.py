"""Pydantic models. Every LLM output that feeds code is one of these."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

EMPTY_STRINGS = {"", "null", "none", "n/a", "na", "undefined"}

# ---------- LLM outputs ----------


class CleanResult(BaseModel):
    """Step 1: messy dictation -> atomic claims."""

    claims: list[str] = Field(
        description="Atomic factual claims the student made, in their final corrected form."
    )
    self_corrections: list[str] = Field(
        description="Each place the student corrected themselves, as 'X -> Y'."
    )
    hedging_score: float = Field(
        ge=0.0,
        le=1.0,
        description="0 = fully confident delivery, 1 = extremely hesitant/unsure.",
    )


class Misconception(BaseModel):
    label: str = Field(
        description="Short reusable name, e.g. 'confuses bias with variance'."
    )
    evidence: str = Field(description="The student's claim that shows it.")


class GradeResult(BaseModel):
    """Step 2: claims vs reference notes."""

    correct_claims: list[str]
    missing_points: list[str] = Field(
        max_length=3,
        description="At most 3 CORE reference points the student did not cover.",
    )
    misconceptions: list[Misconception]


Action = Literal[
    "re_explain", "ask_followup", "revisit_prerequisite", "advance", "review_due"
]


class Decision(BaseModel):
    """Step 4: what the tutor does next."""

    action: Action
    reason: str = Field(description="One or two sentences, addressed to the student.")
    followup_question: str | None = Field(
        default=None, description="Required when action is ask_followup."
    )
    target_concept_id: str | None = Field(
        default=None,
        description="Concept to go to for revisit_prerequisite or advance.",
    )

    @field_validator("followup_question", "target_concept_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        """Models sometimes write the string "null" (or "", "none") for an absent optional field."""
        if isinstance(v, str) and v.strip().lower() in EMPTY_STRINGS:
            return None
        return v.strip() if isinstance(v, str) else v


ToolName = Literal[
    "get_learner_state", "list_due_reviews", "list_weak_concepts",
    "pick_next_concept", "get_prerequisites", "generate_followup_question",
]


class ToolCall(BaseModel):
    """One tool the session agent wants to run. Only the arguments that tool needs are set."""

    tool: ToolName
    concept_id: str | None = None
    strategy: Literal["weakest", "due", "prerequisite_gap"] | None = None
    misconception: str | None = None


class FinalChoice(BaseModel):
    """The session agent's decision: which concept to ask about next, and the question."""

    action: Action
    next_concept_id: str
    question: str = Field(description="The question to ask the student next.")
    why: str = Field(description='One sentence: "Why this next?", addressed to the student.')


class AgentStep(BaseModel):
    """One round of the session tool loop: request tools OR give the final choice."""

    calls: list[ToolCall] = Field(default_factory=list, max_length=3)
    final: FinalChoice | None = None

    @model_validator(mode="after")
    def _calls_or_final(self):
        if not self.calls and self.final is None:
            raise ValueError("an agent step must request at least one tool or give a final choice")
        return self


class Lesson(BaseModel):
    """Step 5 (teach): a mini-lesson that corrects one misconception, plus a check question."""

    lesson: str = Field(description="3-4 sentences correcting the specific misconception.")
    example: str = Field(description="One concrete example, one or two sentences.")
    check_question: str = Field(description="One short question that tests the corrected idea.")
    expected_answer: str = Field(description="Key points a correct answer must contain.")


class CheckResult(BaseModel):
    """Judging the student's answer to a lesson's check question."""

    correct: bool
    feedback: str = Field(description="One or two sentences addressed to the student.")


# ---------- Domain / tool results ----------


class Concept(BaseModel):
    id: str
    name: str
    subject: str
    reference_notes: str
    prerequisites: list[str]


class LearnerState(BaseModel):
    concept_id: str
    mastery: float = 0.0
    confidence: float = 0.0
    interval_days: float = 1.0
    ease: float = 2.5
    next_review_at: str | None = None
    attempts_count: int = 0


class HedgeCounts(BaseModel):
    words: int
    fillers: int
    hedges: int
    reversals: int
    score: float


class DecisionContext(BaseModel):
    """Everything a decider sees. The seam for a future tool-use loop."""

    concept: Concept
    grade: GradeResult
    hedging_score: float
    attempt_score: float
    state: LearnerState
    prerequisite_states: list[LearnerState]
    next_concept_ids: list[str]


# ---------- API ----------


class AttemptRequest(BaseModel):
    concept_id: str
    transcript: str = Field(min_length=1)


class SessionAnswerRequest(BaseModel):
    transcript: str = Field(min_length=1)


class SessionStepView(BaseModel):
    step_no: int
    concept_id: str
    concept_name: str
    question: str
    action: str
    reason: str
    attempt_id: int | None = None
    mastery_after: float | None = None


class SessionView(BaseModel):
    id: int
    status: str  # active | finished | stopped
    started_at: str
    ended_at: str | None = None
    call_count: int
    max_calls: int
    max_questions: int
    message: str | None = None
    steps: list[SessionStepView]
    current: SessionStepView | None = None  # the open question, if any


class LessonAnswerRequest(BaseModel):
    answer: str = Field(min_length=1)


class LessonAnswerResult(BaseModel):
    lesson_id: int
    correct: bool
    feedback: str
    state: LearnerState
    resolved_misconception: str | None


class AttemptResult(BaseModel):
    run_id: str
    attempt_id: int
    concept_id: str
    clean: CleanResult
    hedging: dict
    grade: GradeResult
    attempt_score: float
    state: LearnerState
    decision: Decision
    lesson: Lesson | None = None  # set when re_explain or a misconception triggers the teach step
    lesson_id: int | None = None
    step_timings: dict[str, float]  # seconds per step: context, clean, grade, update, decide, teach
    total_s: float
    agent_log: list[dict]  # each entry includes model and elapsed_s
