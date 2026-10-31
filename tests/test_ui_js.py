"""UI guard: an empty or placeholder follow-up question must render nothing (runs app.js code in Node)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = (Path(__file__).resolve().parent.parent / "app" / "static" / "app.js").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")


def _fn_source(name: str) -> str:
    m = re.search(rf"function {name}\(.*?\n\}}", APP_JS, re.S)
    assert m, f"{name}() not found in app.js"
    return m.group(0)


def _present_source() -> str:
    return _fn_source("present")


def _run(functions: list[str], expr: str):
    script = "\n".join(_fn_source(f) for f in functions) + f"\nconsole.log(JSON.stringify({expr}));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_mastery_band_thresholds():
    cases = [[0, 0], [0.9, 0], [0.0, 1], [0.39, 2], [0.4, 1], [0.7, 1], [0.71, 1], [1.0, 3]]
    assert _run(["masteryBand"], f"{json.dumps(cases)}.map(([m, a]) => masteryBand(m, a))") == [
        "untouched", "untouched", "low", "low", "mid", "mid", "high", "high"]


def test_split_label_wraps_long_names_to_two_lines():
    out = _run(["splitLabel"], json.dumps(
        ["Overfitting", "Train / Validation / Test Split", "Bagging and Random Forests", "Supercalifragilisticexpialidocious"]
    ) + ".map((n) => splitLabel(n))")
    assert out[0] == ["Overfitting"]
    assert out[1] == ["Train / Validation /", "Test Split"] or all(len(l) <= 18 for l in out[1])
    assert all(len(lines) <= 2 and all(len(l) <= 20 for l in lines) for lines in out[1:3])
    assert len(out[3]) == 1  # a single unbreakable word stays on one line


def test_tool_label_formats_args():
    assert _run(["toolLabel"], 'toolLabel("tool.pick_next_concept", {strategy: "due"})') == \
        "pick_next_concept(strategy=due)"
    assert _run(["toolLabel"], 'toolLabel("tool.list_due_reviews", {})') == "list_due_reviews()"


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
