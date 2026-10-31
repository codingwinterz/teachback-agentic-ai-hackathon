"""LLM access. One method: structured(system, user, schema) -> validated model."""

import random
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from app import config

T = TypeVar("T", bound=BaseModel)

RETRYABLE_STATUS = {429, 500, 503}
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
    def structured(self, system: str, user: str, schema: type[T]) -> LLMResult: ...


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

    def _generate(self, system: str, user: str, schema: type[T], failures: list[dict]):
        """generate_content with backoff on 429/5xx, then fall through the model chain.
        Appends each failed attempt to `failures`; returns (response, model, call_seconds)."""
        from google.genai import errors, types

        cfg = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=schema.model_json_schema(),
        )
        last_error: errors.APIError | None = None
        for model in self.models:
            for attempt in range(MAX_RETRIES_PER_MODEL + 1):
                start = time.perf_counter()
                try:
                    resp = self.client.models.generate_content(model=model, contents=user, config=cfg)
                    return resp, model, round(time.perf_counter() - start, 2)
                except errors.APIError as e:
                    if e.code not in RETRYABLE_STATUS:
                        raise LLMError(f"Gemini API error {e.code} on {model}: {e.message}") from e
                    failures.append({
                        "model": model, "status": e.code,
                        "call_s": round(time.perf_counter() - start, 2),
                    })
                    last_error = e
                    if attempt < MAX_RETRIES_PER_MODEL:
                        delay = _retry_hint_seconds(e) or min(MAX_DELAY_S, BASE_DELAY_S * 2**attempt)
                        self.sleep(delay + random.uniform(0, 1))
        raise LLMError(
            f"All models failed ({', '.join(self.models)}). Last: {last_error.code} {last_error.message}"
        )

    def structured(self, system: str, user: str, schema: type[T]) -> LLMResult:
        prompt = user
        last_error: Exception | None = None
        failures: list[dict] = []
        start = time.perf_counter()
        for validation_retry in range(2):  # one retry with the validation error fed back
            resp, model, call_s = self._generate(system, prompt, schema, failures)
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

    def structured(self, system: str, user: str, schema: type[T]) -> LLMResult:
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


def get_llm() -> LLM:
    if config.LLM_PROVIDER == "gemini":
        return GeminiLLM()
    if config.LLM_PROVIDER == "anthropic":
        return AnthropicLLM()
    raise LLMError(f"Unsupported LLM_PROVIDER: {config.LLM_PROVIDER}")
