import os

from dotenv import load_dotenv

load_dotenv()

# "fake" (default): offline FakeLLM, no API calls. "real": call LLM_PROVIDER.
LLM_MODE = os.getenv("LLM_MODE", "fake").strip().lower()
FAKE_LLM_DELAY_S = float(os.getenv("FAKE_LLM_DELAY_S", "0.6"))  # per fake call, for a visible trace

DEFAULT_MODELS ={"gemini": "gemini-3.5-flash-lite", "anthropic": "claude-opus-5-5"}

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini")
LLM_MODEL = os.getenv("LLM_MODEL") or DEFAULT_MODELS.get(LLM_PROVIDER, "")
# Comma-separated models tried in order when the primary stays overloaded/rate-limited.
# Lite models first; 3.6/3.8 Flash last since their free tier is ~20 requests/day.
DEFAULT_FALLBACK_MODELS = "gemini-3.1-flash-lite,gemini-3.6-flash,gemini-3.8-flash"
LLM_FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv("LLM_FALLBACK_MODELS", DEFAULT_FALLBACK_MODELS).split(",")
    if m.strip()
]
LLM_EFFORT = os.getenv("LLM_EFFORT", "medium")  # anthropic only
LLM_CALL_TIMEOUT_S = float(os.getenv("LLM_CALL_TIMEOUT_S", "12"))  # per request; timeout is retryable
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DB_PATH = os.getenv("DB_PATH", "teachback.db")
CONCEPTS_PATH = os.getenv("CONCEPTS_PATH", "data/concepts.json")
