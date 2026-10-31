"""Code-based hedging signal, blended 50/50 with the LLM's hedging score."""

import re

from app.schemas import HedgeCounts

FILLERS = ["um", "umm", "uh", "uhh", "er", "erm", "hmm", "you know", "basically", "like"]
HEDGES = [
    "i think", "i guess", "i believe", "i suppose", "maybe", "probably", "perhaps",
    "kind of", "sort of", "not sure", "i m not sure", "might be", "something like",
]
REVERSALS = [
    "no wait", "wait no", "actually no", "no actually", "i mean", "scratch that",
    "or rather", "sorry i mean", "let me rephrase",
]

# Weighted markers per word, scaled so ~1 hedge-ish marker every 8 words reads as ~0.5.
HEDGE_WEIGHT = 2
REVERSAL_WEIGHT = 2
SCALE = 4.0


def _normalize(text: str) -> str:
    text = text.lower().replace("'", " ").replace("’", " ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z\s]", " ", text)).strip()


def _count(phrases: list[str], text: str) -> int:
    return sum(len(re.findall(rf"\b{re.escape(p)}\b", text)) for p in phrases)


def code_hedging(transcript: str) -> HedgeCounts:
    text = _normalize(transcript)
    words = len(text.split())
    fillers = _count(FILLERS, text)
    hedges = _count(HEDGES, text)
    reversals = _count(REVERSALS, text)
    weighted = fillers + HEDGE_WEIGHT * hedges + REVERSAL_WEIGHT * reversals
    score = min(1.0, SCALE * weighted / words) if words else 0.0
    return HedgeCounts(
        words=words, fillers=fillers, hedges=hedges, reversals=reversals,
        score=round(score, 3),
    )


def blend(llm_score: float, code_score: float) -> float:
    return round((llm_score + code_score) / 2, 3)
