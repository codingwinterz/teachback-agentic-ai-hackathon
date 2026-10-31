"""LLM access. One method: structured(system, user, schema) -> validated model."""

import functools
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from app import config

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger(__name__)

# 504 = our per-call timeout: the SDK sends it to Gemini as a server deadline, so a slow
# call usually comes back as 504 DEADLINE_EXCEEDED rather than a client-side httpx timeout.
RETRYABLE_STATUS = {429, 500, 503, 504}
MAX_RETRIES_PER_MODEL = 2  # then move to the next model in the fallback chain
BASE_DELAY_S = 2.0
MAX_DELAY_S = 60.0


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    parsed: BaseModel
    raw_text: str
    model: str | None = None  # the model that produced `parsed`
    elapsed_s: float = 0.0  # whole structured() call, including retries and backoff sleeps
    last_call_s: float = 0.0  # just the successful API request
    failed_attempts: list[dict] = field(default_factory=list)  # [{"model", "status"}]
    validation_retries: int = 0

    def timing(self) -> dict:
        return {
            "model": self.model,
            "elapsed_s": self.elapsed_s,
            "last_call_s": self.last_call_s,
            "api_retries": len(self.failed_attempts),
            "failed_attempts": self.failed_attempts,
            "validation_retries": self.validation_retries,
        }


class LLM(Protocol):
    def structured(
        self, system: str, user: str, schema: type[T], thinking: str | None = None
    ) -> LLMResult:
        """thinking: optional "minimal" | "low" | "medium" | "high"; None = model default."""
        ...


def _retry_hint_seconds(err) -> float | None:
    """Gemini 429s carry a RetryInfo detail like {"retryDelay": "17s"}."""
    details = (getattr(err, "details", None) or {}).get("error", {}).get("details", [])
    for d in details:
        m = re.match(r"([\d.]+)s", str(d.get("retryDelay", "")))
        if m:
            return float(m.group(1))
    return None


class GeminiLLM:
    def __init__(
        self,
        model: str = config.LLM_MODEL,
        api_key: str | None = config.GEMINI_API_KEY,
        client=None,
        sleep: Callable[[float], None] = time.sleep,
        fallback_models: list[str] | None = None,
        call_timeout_s: float = config.LLM_CALL_TIMEOUT_S,
    ):
        if client is None:
            from google import genai
            from google.genai import types

            if not api_key:
                raise LLMError("GEMINI_API_KEY is not set")
            # Disable the SDK's own retries so backoff + fallback live in one place (here).
            client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1)),
            )
        self.client = client
        fallbacks = config.LLM_FALLBACK_MODELS if fallback_models is None else fallback_models
        self.models = [model] + [m for m in fallbacks if m != model]
        self.sleep = sleep
        self.call_timeout_s = call_timeout_s
        self.exhausted_until: dict[str, float] = {}  # model -> epoch seconds

    def _generate(
        self, system: str, user: str, schema: type[T], failures: list[dict], thinking: str | None
    ):
        """generate_content with backoff on 429/5xx/timeout, then fall through the model chain.
        Appends each failed attempt to `failures`; returns (response, model, call_seconds)."""
        import httpx
        from google.genai import errors, types

        cfg = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=schema.model_json_schema(),
            http_options=types.HttpOptions(timeout=int(self.call_timeout_s * 1000)),
            thinking_config=(
                types.ThinkingConfig(thinking_level=thinking.upper()) if thinking else None
            ),
        )
        last_error = "none"
        for model in self.models:
            if self.exhausted_until.get(model, 0) > time.time():
                failures.append({"model": model, "status": "skipped_exhausted", "call_s": 0.0})
                continue
            for attempt in range(MAX_RETRIES_PER_MODEL + 1):
                start = time.perf_counter()
                hint = None
                try:
                    resp = self.client.models.generate_content(model=model, contents=user, config=cfg)
                    call_s = round(time.perf_counter() - start, 2)
                    logger.info("%s %s ok in %.2fs (thinking=%s)", schema.__name__, model, call_s, thinking)
                    return resp, model, call_s
                except errors.APIError as e:
                    if e.code not in RETRYABLE_STATUS:
                        raise LLMError(f"Gemini API error {e.code} on {model}: {e.message}") from e
                    status, hint, last_error = e.code, _retry_hint_seconds(e), f"{e.code} {e.message}"
                except httpx.TimeoutException:
                    status, last_error = "timeout", f"timeout after {self.call_timeout_s}s"
                failures.append({
                    "model": model, "status": status,
                    "call_s": round(time.perf_counter() - start, 2),
                })
                logger.warning("%s %s failed: %s after %.2fs",
                               schema.__name__, model, status, failures[-1]["call_s"])
                if hint and hint > MAX_DELAY_S:
                    # e.g. daily free-tier quota gone (retryDelay ~20h): skip this model until then.
                    self.exhausted_until[model] = time.time() + hint
                    logger.warning("%s exhausted for %.0fs; moving to next model", model, hint)
                    break
                if attempt < MAX_RETRIES_PER_MODEL:
                    delay = hint or min(MAX_DELAY_S, BASE_DELAY_S * 2**attempt)
                    self.sleep(delay + random.uniform(0, 1))
        raise LLMError(f"All models failed ({', '.join(self.models)}). Last: {last_error}")

    def structured(
        self, system: str, user: str, schema: type[T], thinking: str | None = None
    ) -> LLMResult:
        prompt = user
        last_error: Exception | None = None
        failures: list[dict] = []
        start = time.perf_counter()
        for validation_retry in range(2):  # one retry with the validation error fed back
            resp, model, call_s = self._generate(system, prompt, schema, failures, thinking)
            finish = resp.candidates[0].finish_reason if resp.candidates else None
            finish_name = getattr(finish, "name", str(finish))
            raw_text = resp.text or ""
            if finish_name == "MAX_TOKENS":
                raise LLMError("Model output hit max tokens before finishing JSON")
            if not raw_text:
                raise LLMError(f"Empty response (finish_reason={finish_name})")
            try:
                return LLMResult(
                    parsed=schema.model_validate_json(raw_text),
                    raw_text=raw_text,
                    model=model,
                    elapsed_s=round(time.perf_counter() - start, 2),
                    last_call_s=call_s,
                    failed_attempts=failures,
                    validation_retries=validation_retry,
                )
            except ValidationError as e:
                last_error = e
                prompt = f"{user}\n\nYour previous output failed validation:\n{e}\nReturn corrected JSON."
        raise LLMError(f"Validation failed after retry: {last_error}")


class AnthropicLLM:
    def __init__(self, model: str = config.LLM_MODEL, effort: str = config.LLM_EFFORT):
        import anthropic

        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = model
        self.effort = effort

    def structured(
        self, system: str, user: str, schema: type[T], thinking: str | None = None
    ) -> LLMResult:
        # `thinking` is ignored here: Opus 5.5 thinking is adaptive, depth set via LLM_EFFORT.
        prompt = user
        last_error: Exception | None = None
        for _ in range(2):  # one retry with the validation error fed back
            start = time.perf_counter()
            try:
                resp = self.client.messages.parse(
                    model=self.model,
                    max_tokens=16000,
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                    output_format=schema,
                    output_config={"effort": self.effort},
                )
            except ValidationError as e:
                last_error = e
                prompt = f"{user}\n\nYour previous output failed validation:\n{e}\nReturn corrected JSON."
                continue
            except self.anthropic.APIError as e:
                raise LLMError(f"Anthropic API error: {e}") from e

            if resp.stop_reason == "refusal":
                raise LLMError(f"Model refused: {resp.stop_details}")
            if resp.stop_reason == "max_tokens":
                raise LLMError("Model output hit max_tokens before finishing JSON")
            raw_text = "".join(b.text for b in resp.content if b.type == "text")
            if resp.parsed_output is None:
                raise LLMError(f"No parsed output. Raw: {raw_text[:500]}")
            elapsed = round(time.perf_counter() - start, 2)
            return LLMResult(
                parsed=schema.model_validate(resp.parsed_output.model_dump()),
                raw_text=raw_text, model=self.model, elapsed_s=elapsed, last_call_s=elapsed,
            )
        raise LLMError(f"Validation failed after retry: {last_error}")


@functools.cache
def get_llm() -> LLM:
    """One shared instance, so quota-exhaustion memory persists across requests."""
    if config.LLM_PROVIDER == "gemini":
        return GeminiLLM()
    if config.LLM_PROVIDER == "anthropic":
        return AnthropicLLM()
    raise LLMError(f"Unsupported LLM_PROVIDER: {config.LLM_PROVIDER}")
