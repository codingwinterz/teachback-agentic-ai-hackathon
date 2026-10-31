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


def test_due_text():
    now = 'new Date("2026-10-31T12:00:00Z")'
    assert _run(["dueText"], f'["2026-10-31 08:00:00", "2026-10-30 11:00:00", "2026-10-28 12:00:00"].map((d) => dueText(d, {now}))') == \
        ["due today", "overdue by 1 day", "overdue by 3 days"]


def test_plan_text_for_the_clipboard():
    plan = {"summary": "Start with Overfitting.", "steps": [
        {"concept_name": "Overfitting", "what_to_do": "Re-explain it.", "misconception": "thinks more data always fixes it",
         "next_review": "2026-11-01"},
        {"concept_name": "K-Means", "what_to_do": "Quick review.", "misconception": None, "next_review": "2026-11-03"}]}
    text = _run(["planText"], f"planText({json.dumps(plan)})")
    assert text.splitlines() == [
        "TeachBack study plan", "Start with Overfitting.", "",
        "1. Overfitting: Re-explain it.", "   Fix: thinks more data always fixes it", "   Review by 2026-11-01",
        "2. K-Means: Quick review.", "   Review by 2026-11-03"]


def test_short_date():
    assert _run(["shortDate"], '["2026-10-29 23:59:00", "2026-11-01 00:00:00"].map(shortDate)') == ["Oct 29", "Nov 1"]


def test_friendly_error_messages():
    cases = [
        [502, "Gemini API error: All models failed (a, b). Last: 429 quota"],
        [502, "Gemini API error 504 on m: Deadline expired"],
        [502, "Anthropic API error: overloaded"],
        [404, "Unknown concept: x"],
        [409, "Session 3 is finished"],
        [500, "TypeError: boom"],
        [0, "Failed to fetch"],
        [502, "This session used its 30 AI requests."],
    ]
    out = _run(["friendlyError"], f"{json.dumps(cases)}.map(([s, d]) => friendlyError(s, d))")
    assert "out of requests" in out[0]["message"] and out[0]["retry"]
    assert "slowly" in out[1]["message"] and out[1]["retry"]
    assert "problem" in out[2]["message"] and out[2]["retry"]
    assert out[3]["retry"] is False and "Reload" in out[3]["message"]
    assert out[4]["retry"] is False and "Start a new one" in out[4]["message"]
    assert out[5]["retry"] and out[6]["retry"] and "connection" in out[6]["message"]
    assert out[7]["retry"] is False and "budget" in out[7]["message"]
    assert all("Error" not in o["message"] and "429" not in o["message"] for o in out)  # no raw errors


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
