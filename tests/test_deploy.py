"""Deploy config sanity checks (no network, no real calls)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RENDER = (ROOT / "render.yaml").read_text(encoding="utf-8")


def _env_block(key: str) -> str:
    m = re.search(rf"- key: {key}\n((?:\s+\w+: .*\n?)*)", RENDER)
    assert m, f"{key} missing from render.yaml"
    return m.group(1)


def test_render_reads_key_and_model_chain_from_env():
    for key in ["LLM_MODE", "LLM_PROVIDER", "LLM_MODEL", "LLM_FALLBACK_MODELS", "HOST", "DB_PATH"]:
        assert "value:" in _env_block(key), key
    assert "gemini-3.5-flash-lite" in _env_block("LLM_MODEL")
    assert _env_block("LLM_FALLBACK_MODELS").strip().endswith(
        "gemini-3.1-flash-lite,gemini-3.6-flash,gemini-3.8-flash"
    )


def test_gemini_key_is_a_dashboard_secret_not_a_committed_value():
    block = _env_block("GEMINI_API_KEY")
    assert "sync: false" in block and "value:" not in block
    assert not re.search(r"AIza[0-9A-Za-z_\-]{20,}", RENDER)


def test_fresh_deploy_starts_in_fake_mode_and_uses_the_start_command():
    assert "value: fake" in _env_block("LLM_MODE")
    assert "startCommand: python -m app" in RENDER
    assert "healthCheckPath: /health" in RENDER
    assert (ROOT / "app" / "__main__.py").exists()
