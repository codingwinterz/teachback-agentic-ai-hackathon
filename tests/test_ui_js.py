"""UI guard: an empty or placeholder follow-up question must render nothing (runs app.js code in Node)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = (Path(__file__).resolve().parent.parent / "app" / "static" / "app.js").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")


def _present_source() -> str:
    m = re.search(r"function present\(value\) \{.*?\n\}", APP_JS, re.S)
    assert m, "present() not found in app.js"
    return m.group(0)


def test_present_drops_null_like_values_and_keeps_real_text():
    cases = [None, "null", "NULL", " null ", "", "   ", "none", "n/a", "undefined", 42,
             "  Can more data fail to fix overfitting?  "]
    script = f"{_present_source()}\nconsole.log(JSON.stringify({json.dumps(cases)}.map(present)));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == [None] * 10 + ["Can more data fail to fix overfitting?"]


def test_render_result_uses_the_guard_for_the_question_and_target():
    render = APP_JS[APP_JS.index("function renderResult"):APP_JS.index("// ---------- lesson card")]
    assert "present(d.followup_question)" in render and "present(d.target_concept_id)" in render
    # the raw field must not decide what is shown
    assert not re.search(r"d\.followup_question\s*\?", render)
