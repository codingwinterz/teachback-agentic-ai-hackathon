import os

from dotenv import load_dotenv

load_dotenv()

DEFAULT_MODELS = {"gemini": "gemini-3.6-flash", "anthropic": "claude-opus-5-5"}

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini")
LLM_MODEL = os.getenv("LLM_MODEL") or DEFAULT_MODELS.get(LLM_PROVIDER, "")
# Comma-separated models tried in order when the primary stays overloaded/rate-limited.
LLM_FALLBACK_MODELS = [
    m.strip() for m in os.getenv("LLM_FALLBACK_MODELS", "gemini-3.5-flash-lite").split(",") if m.strip()
]
LLM_EFFORT = os.getenv("LLM_EFFORT", "medium")  # anthropic only
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DB_PATH = os.getenv("DB_PATH", "teachback.db")
CONCEPTS_PATH = os.getenv("CONCEPTS_PATH", "data/concepts.json")
