"""Offline stand-in for the real LLM (dev mode). Zero API calls, zero quota.

Parses the same prompts the real model receives and returns plausible, deterministic
outputs, so the whole loop and UI can be developed without spending requests.
"""

import json
import re
import time

from pydantic import BaseModel

from app import hedging
from app.llm import LLMResult
from app.schemas import (
    AgentStep, CheckResult, CleanResult, Decision, FinalChoice, GradeResult, Lesson, Misconception,
    ToolCall,
)

ABSOLUTES = re.compile(r"\b(always|never|only|guarantee[sd]?|every time|completely)\b", re.I)
REVERSAL = re.compile(r"\b(no,? wait|wait,? no|actually,? no|i mean|scratch that|or rather)\b[,\s]*", re.I)
FILLER = re.compile(
    r"\b(um+|uh+|er+m?|hmm|like|you know|basically|okay so|so|i think|i guess|"
    r"i believe|maybe|probably|kind of|sort of)\b[,\s]*",
    re.I,
)
WORD = re.compile(r"[a-z]{4,}")


def _between(text: str, start: str, end: str | None = None) -> str:
    pattern = re.escape(start) + (r"(.*?)" + re.escape(end) if end else r"(.*)")
    m = re.search(pattern, text, re.S)
    return m.group(1).strip() if m else ""


def _tidy(s: str) -> str:
    s = re.sub(r"\s+", " ", FILLER.sub("", s)).strip(" ,.")
    s = re.sub(r"^(and|but|then|also)\b[,\s]*", "", s, flags=re.I)
    return s[:1].upper() + s[1:] + "." if s else ""


def _clean(user: str) -> CleanResult:
    transcript = _between(user, "<transcript>", "</transcript>")
    claims, corrections = [], []
    for sentence in re.split(r"(?<=[.!?])\s+", transcript):
        parts = REVERSAL.split(sentence)
        if len(parts) > 1:  # "X, no wait, Y" -> keep Y
            before, after = parts[0], parts[-1]
            corrections.append(f"{_tidy(before).rstrip('.')} -> {_tidy(after).rstrip('.')}")
            sentence = after
        if (claim := _tidy(sentence)) and len(claim.split()) >= 3:
            claims.append(claim)
    return CleanResult(
        claims=claims,
        self_corrections=corrections,
        hedging_score=hedging.code_hedging(transcript).score,
    )


def _grade(user: str) -> GradeResult:
    claims = [c[2:] for c in _between(user, "Student claims:").splitlines() if c.startswith("- ")]
    notes = [n[2:] for n in _between(user, "Reference notes:", "Known misconception").splitlines()
             if n.startswith("- ")]
    known = json.loads(_between(user, "student and concept:", "\n") or "[]")

    correct, misconceptions = [], []
    for c in claims:
        if ABSOLUTES.search(c):
            label = known[0] if known else f"overgeneralizes: '{c[:50].rstrip('.')}'"
            misconceptions.append(Misconception(label=label, evidence=c))
        else:
            correct.append(c)

    said = set(WORD.findall(" ".join(claims).lower()))
    missing = []
    for n in notes:
        words = set(WORD.findall(n.lower()))
        if words and len(words & said) / len(words) < 0.3:
            missing.append(" ".join(n.split()[:10]))
    return GradeResult(correct_claims=correct, missing_points=missing[:3], misconceptions=misconceptions)


def _decide(user: str) -> Decision:
    score = float(_between(user, "Attempt score (hedge-discounted):", "\n") or 0)
    grade = GradeResult.model_validate_json(_between(user, "Grade:", "\n"))
    next_ids = json.loads(_between(user, "Concepts that build on this one:").replace("'", '"') or "[]")

    if grade.misconceptions:
        m = grade.misconceptions[0]
        return Decision(
            action="ask_followup",
            reason=f"Mostly there, but one claim needs a second look: \"{m.evidence}\"",
            followup_question=f"Is it really true that {m.evidence[0].lower()}{m.evidence[1:].rstrip('.')}? When might it fail?",
        )
    if score >= 0.7:
        return Decision(action="advance", reason="Solid, confident explanation.",
                        target_concept_id=next_ids[0] if next_ids else None)
    if score < 0.3:
        return Decision(action="re_explain", reason="Let's go over the core idea again.")
    gap = grade.missing_points[0] if grade.missing_points else "the key idea"
    return Decision(action="ask_followup", reason="Good start; one important piece is missing.",
                    followup_question=f"Can you add something about: {gap}?")


def _overlap(a: str, b: str) -> float:
    """Share of b's content words that also appear in a."""
    words_b = set(WORD.findall(b.lower()))
    return len(words_b & set(WORD.findall(a.lower()))) / len(words_b) if words_b else 0.0


def _teach(user: str) -> Lesson:
    concept = _between(user, "Concept:", "\n")
    notes = [n[2:] for n in _between(user, "Reference notes:", "Misconception to correct:").splitlines()
             if n.startswith("- ")]
    label = _between(user, "Misconception to correct:", "\n")
    evidence = _between(user, "Student's claim:")
    has_misconception = not label.startswith("(none")

    # the note that best addresses the claim, preferring ones that say what is NOT true
    best = max(notes, key=lambda n: _overlap(n, evidence) + (0.2 if re.search(r"\bnot\b", n, re.I) else 0))
    core = notes[0]
    example = next((n for n in notes if "e.g." in n or "example" in n.lower()), notes[min(1, len(notes) - 1)])
    if has_misconception:
        lesson = (f"Careful: \"{evidence.rstrip('.')}\" is not quite right. {best} "
                  f"Remember the core idea: {core}")
        question = f"In a sentence or two, why is \"{evidence.rstrip('.')}\" not always true?"
    else:
        lesson = f"Let's go over {concept} again. {core} {best}"
        question = f"In a sentence or two, what is {concept} and why does it matter?"
    return Lesson(lesson=lesson, example=f"For example: {example}", check_question=question,
                  expected_answer=best)


def _check(user: str) -> CheckResult:
    expected = _between(user, "Expected answer (key points):", "\n")
    answer = _between(user, "<answer>", "</answer>")
    repeats_absolute = bool(ABSOLUTES.search(answer)) and not re.search(r"\bnot\b|n't", answer, re.I)
    correct = _overlap(answer, expected) >= 0.25 and not repeats_absolute
    feedback = ("Yes, that's the key idea." if correct
                else f"Not quite. The key idea: {expected}")
    return CheckResult(correct=correct, feedback=feedback)


CALL_LINE = re.compile(r"^CALL \d+: (\w+)\((\{.*?\})\) -> (.*)$", re.M)


def _agent_step(user: str) -> AgentStep:
    """Session planner: round 1 gathers with tools, round 2 gives the final choice."""
    task = _between(user, "Task:", "\n")
    current = _between(user, "Current concept:", "\n")
    current = None if current in ("", "(none)") else current
    calls = {m[1]: json.loads(m[3]) for m in CALL_LINE.finditer(user)}  # tool -> latest result
    starting = task.startswith("Start")
    misconceptions = json.loads((re.search(r"Misconceptions: (\[.*?\])\.", task) or [None, "[]"])[1])
    mastery = float((re.search(r"mastery now (\d+(?:\.\d+)?)", task) or [None, "0"])[1])
    attempts = int((re.search(r"this session: (\d+)", task) or [None, "0"])[1])
    struggling = attempts >= 2 and mastery < 0.4

    if not calls:  # round 1: choose tools
        if starting:
            plan = [ToolCall(tool="list_due_reviews"), ToolCall(tool="list_weak_concepts"),
                    ToolCall(tool="pick_next_concept", strategy="due")]
        elif struggling:
            plan = [ToolCall(tool="get_prerequisites", concept_id=current),
                    ToolCall(tool="get_learner_state", concept_id=current),
                    ToolCall(tool="pick_next_concept", strategy="prerequisite_gap", concept_id=current)]
        elif misconceptions:
            plan = [ToolCall(tool="get_learner_state", concept_id=current),
                    ToolCall(tool="generate_followup_question", concept_id=current,
                             misconception=misconceptions[0])]
        else:
            plan = [ToolCall(tool="get_learner_state", concept_id=current),
                    ToolCall(tool="list_weak_concepts"),
                    ToolCall(tool="pick_next_concept", strategy="weakest")]
        return AgentStep(calls=plan)

    # round 2: final choice from the tool results
    if "generate_followup_question" in calls and "pick_next_concept" not in calls:
        q = calls["generate_followup_question"]
        return AgentStep(final=FinalChoice(
            action="ask_followup", next_concept_id=q["concept_id"], question=q["question"],
            why=f"You still have a misconception about this concept, so one more question on it."))
    pick = calls["pick_next_concept"]
    action = ("revisit_prerequisite" if pick["strategy_used"] == "prerequisite_gap"
              else "review_due" if pick["strategy_used"] == "due" else "advance")
    why = pick["why"]
    if action == "revisit_prerequisite":
        name = calls.get("get_learner_state", {}).get("name", current)
        why = (f"You've scored below 40% on {name} twice this session, so let's strengthen its "
               f"prerequisite {pick['name']} first.")
    return AgentStep(final=FinalChoice(
        action=action, next_concept_id=pick["concept_id"],
        question=f"Explain {pick['name']} out loud as if teaching a friend: what it is and why it matters.",
        why=why))


class FakeLLM:
    model = "fake-dev"

    def __init__(self, delay_s: float = 0.0):
        self.delay_s = delay_s  # simulated latency so the UI trace visibly steps through

    def structured(self, system: str, user: str, schema: type[BaseModel], thinking: str | None = None) -> LLMResult:
        handlers = {CleanResult: _clean, GradeResult: _grade, Decision: _decide,
                    Lesson: _teach, CheckResult: _check, AgentStep: _agent_step}
        if schema not in handlers:
            raise ValueError(f"FakeLLM has no handler for {schema.__name__}")
        start = time.perf_counter()
        time.sleep(self.delay_s)
        out = handlers[schema](user)
        elapsed = round(time.perf_counter() - start, 2)
        return LLMResult(parsed=out, raw_text=out.model_dump_json(), model=self.model,
                         elapsed_s=elapsed, last_call_s=elapsed)
