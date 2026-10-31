"use strict";

const STEPS = [
  { id: "clean", label: "Clean", tools: "claims, self-corrections, hesitation" },
  { id: "grade", label: "Grade", tools: "compare with reference notes" },
  { id: "update", label: "Update", tools: "mastery, misconceptions" },
  { id: "decide", label: "Decide", tools: "pick the next action" },
  { id: "teach", label: "Teach", tools: "mini-lesson if needed" },
];

const ACTIONS = {
  re_explain: "Explain it again",
  ask_followup: "Answer a follow-up question",
  revisit_prerequisite: "Revisit a prerequisite first",
  advance: "Move on to the next concept",
  review_due: "Review a concept that's due",
};

const $ = (id) => document.getElementById(id);
let concepts = [];
let byId = {};
let state = { learner_state: [], misconceptions: [] };
let lastUpdated = null;

// ---------- helpers ----------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c != null) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function fillList(listEl, items, render, emptyText) {
  listEl.replaceChildren(
    ...(items.length ? items.map(render) : [el("li", { class: "empty" }, emptyText)])
  );
}

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} returned ${r.status}`);
  return r.json();
}

// ---------- concept picker ----------

function currentConcept() {
  return byId[$("concept").value];
}

function showConcept() {
  const c = currentConcept();
  if (!c) return;
  $("concept-name").textContent = c.name;
  const prereqs = c.prerequisites.map((p) => byId[p]?.name ?? p);
  $("prereqs").textContent = prereqs.length ? `Builds on: ${prereqs.join(", ")}` : "No prerequisites.";
  renderMastery();
}

function selectConcept(id, { focus = true } = {}) {
  $("concept").value = id;
  showConcept();
  if (focus) $("transcript").focus();
}

// ---------- learner dashboard ----------

function renderMastery() {
  const current = $("concept").value;
  const rows = Object.fromEntries(state.learner_state.map((s) => [s.concept_id, s]));
  $("mastery").replaceChildren(
    ...concepts.map((c) => {
      const s = rows[c.id] || { mastery: 0, attempts_count: 0 };
      const pct = Math.round(s.mastery * 100);
      const cls = [c.id === current && "current", c.id === lastUpdated && "updated"]
        .filter(Boolean).join(" ");
      return el("li", { class: cls },
        el("button", {
          type: "button",
          title: `${s.attempts_count} attempt${s.attempts_count === 1 ? "" : "s"}`,
          onclick: () => selectConcept(c.id),
        },
          el("span", {}, c.name),
          el("span", { class: "pct" }, s.attempts_count ? `${pct}%` : "–"),
          el("span", {
            class: "bar", role: "progressbar", "aria-label": `${c.name} mastery`,
            "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(pct),
          }, el("span", { style: `width:${pct}%` }))
        )
      );
    })
  );

  const open = state.misconceptions.filter((m) => !m.resolved);
  fillList($("open-misconceptions"), open, (m) =>
    el("li", {},
      m.label,
      el("span", { class: "count" }, `×${m.count}`),
      el("span", { class: "where" }, byId[m.concept_id]?.name ?? m.concept_id)
    ), "No open misconceptions. Ones the tutor spots collect here until a lesson check clears them.");
}

async function refreshState() {
  state = await getJSON("/state");
  renderMastery();
}

// ---------- live trace ----------

function resetTrace() {
  $("trace-error").hidden = true;
  $("steps").replaceChildren(
    ...STEPS.map((s) =>
      el("li", { class: "step", id: `step-${s.id}` },
        el("div", { class: "step-head" }, s.label),
        el("div", { class: "step-meta" }),
        el("div", { class: "step-tools" }, s.tools)
      )
    )
  );
}

function onStepStart(step) {
  $(`step-${step}`)?.classList.add("running");
}

function onStepDone({ step, model, elapsed_s }) {
  const row = $(`step-${step}`);
  if (!row) return;
  row.classList.remove("running");
  row.classList.add("done");
  row.querySelector(".step-meta").textContent = `${model} · ${Number(elapsed_s).toFixed(2)}s`;
}

function onStepSkipped(step) {
  const row = $(`step-${step}`);
  if (!row) return;
  row.classList.add("skipped");
  row.querySelector(".step-meta").textContent = "not needed";
}

function onTool({ step, tool_name }) {
  const row = $(`step-${step}`);
  if (!row) return;
  const tools = row.querySelector(".step-tools");
  if (!row.dataset.calls) tools.textContent = "";
  row.dataset.calls = `${row.dataset.calls || ""}${tool_name} `;
  tools.textContent = row.dataset.calls.trim().split(" ").join(", ");
}

function showError(message) {
  for (const row of document.querySelectorAll(".step.running")) {
    row.classList.replace("running", "failed");
    row.querySelector(".step-meta").textContent = "failed";
  }
  $("trace-error").textContent = message;
  $("trace-error").hidden = false;
}

// ---------- result panel ----------

function renderResult(r) {
  const counts = Object.fromEntries(
    state.misconceptions.filter((m) => m.concept_id === r.concept_id).map((m) => [m.label, m.count])
  );

  fillList($("claims"), r.clean.claims, (c) => el("li", {}, c), "No claims found in the transcript.");
  $("corrections").textContent = r.clean.self_corrections.length
    ? `You corrected yourself: ${r.clean.self_corrections.join("; ")}`
    : "";
  const h = r.hedging;
  $("hedging").textContent =
    `Hesitation ${Math.round(h.blended * 100)}% (counted ${h.code.fillers} fillers, ` +
    `${h.code.hedges} hedges, ${h.code.reversals} reversals; model heard ${Math.round(h.llm * 100)}%). ` +
    `Mastery now ${Math.round(r.state.mastery * 100)}%.`;

  fillList($("correct"), r.grade.correct_claims, (c) => el("li", {}, c), "Nothing correct yet.");
  fillList($("missing"), r.grade.missing_points, (c) => el("li", {}, c), "Nothing important missing.");
  fillList($("misconceptions"), r.grade.misconceptions, (m) =>
    el("li", {},
      el("mark", {}, m.label),
      counts[m.label] > 1 ? el("span", { class: "count" }, `seen ${counts[m.label]}×`) : null,
      el("span", { class: "evidence" }, `“${m.evidence}”`)
    ), "No misconceptions.");

  const d = r.decision;
  const buttons = [];
  if (d.followup_question || d.action === "re_explain") {
    buttons.push(el("button", {
      type: "button", class: "quiet",
      onclick: () => { $("transcript").value = ""; updateWordCount(); $("transcript").focus(); },
    }, d.followup_question ? "Answer it" : "Try again"));
  }
  if (d.target_concept_id && byId[d.target_concept_id]) {
    buttons.push(el("button", {
      type: "button", class: "quiet",
      onclick: () => { $("transcript").value = ""; updateWordCount(); selectConcept(d.target_concept_id); },
    }, `Go to ${byId[d.target_concept_id].name}`));
  }
  $("next").replaceChildren(
    el("span", { class: "action" }, ACTIONS[d.action] ?? d.action),
    el("span", {}, d.reason),
    d.followup_question ? el("p", { class: "question" }, d.followup_question) : null,
    buttons.length ? el("div", { class: "buttons" }, buttons) : null
  );
  $("result").hidden = false;
}

// ---------- lesson card ----------

let lessonId = null;

function renderLesson(r) {
  lessonId = r.lesson_id;
  $("lesson").hidden = !r.lesson;
  if (!r.lesson) return;
  $("lesson-text").textContent = r.lesson.lesson;
  $("lesson-example").textContent = r.lesson.example;
  $("check-question").textContent = r.lesson.check_question;
  $("check-answer").value = "";
  $("check-answer").disabled = false;
  $("check-submit").disabled = false;
  $("check-feedback").hidden = true;
}

async function submitCheck(event) {
  event.preventDefault();
  const answer = $("check-answer").value.trim();
  if (!answer || lessonId == null || busy) return;
  busy = true;
  const button = $("check-submit");
  button.disabled = true;
  button.textContent = "Checking…";
  const feedback = $("check-feedback");
  try {
    const resp = await fetch(`/lessons/${lessonId}/answer`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answer }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail ?? `status ${resp.status}`);
    feedback.textContent = data.correct
      ? `${data.feedback} Mastery now ${Math.round(data.state.mastery * 100)}%.`
      : `${data.feedback} Try again.`;
    feedback.className = `check-feedback ${data.correct ? "right" : "wrong"}`;
    $("check-answer").disabled = data.correct;
    lastUpdated = data.state.concept_id;
    await refreshState();
    button.disabled = data.correct;
  } catch (err) {
    feedback.textContent = `Couldn't check the answer: ${err.message}`;
    feedback.className = "check-feedback wrong";
    button.disabled = false;
  } finally {
    feedback.hidden = false;
    button.textContent = "Check answer";
    busy = false;
  }
}

// ---------- submit (server-sent events over fetch) ----------

let busy = false; // one attempt at a time: a repeated Ctrl+Enter must not spend a second LLM run

async function submitAttempt(event) {
  event?.preventDefault();
  if (busy) return;
  const transcript = $("transcript").value.trim();
  if (!transcript) {
    $("transcript").focus();
    return;
  }
  busy = true;
  const button = $("submit");
  button.disabled = true;
  button.textContent = "Checking…";
  resetTrace();
  $("lesson").hidden = true;

  try {
    const resp = await fetch("/attempt/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ concept_id: $("concept").value, transcript }),
    });
    if (!resp.ok) throw new Error(`The server returned ${resp.status}. Check the uvicorn log.`);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf("\n\n")) >= 0) {
        const chunk = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        if (chunk.startsWith("data: ")) await handleEvent(JSON.parse(chunk.slice(6)));
      }
    }
  } catch (err) {
    showError(`Couldn't reach the tutor: ${err.message}`);
  } finally {
    busy = false;
    button.disabled = false;
    button.textContent = "Check my explanation";
  }
}

async function handleEvent(e) {
  if (e.type === "step_start") onStepStart(e.step);
  else if (e.type === "step_done") onStepDone(e);
  else if (e.type === "step_skipped") onStepSkipped(e.step);
  else if (e.type === "tool") onTool(e);
  else if (e.type === "error") showError(`${e.detail} (status ${e.status})`);
  else if (e.type === "result") {
    lastUpdated = e.data.concept_id;
    await refreshState();
    renderResult(e.data);
    renderLesson(e.data);
  }
}

// ---------- boot ----------

function updateWordCount() {
  const n = $("transcript").value.trim().split(/\s+/).filter(Boolean).length;
  $("wordcount").textContent = `${n} word${n === 1 ? "" : "s"}`;
}

async function boot() {
  $("transcript").focus();
  resetTrace();
  $("attempt-form").addEventListener("submit", submitAttempt);
  $("check-form").addEventListener("submit", submitCheck);
  $("transcript").addEventListener("input", updateWordCount);
  $("transcript").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !e.repeat) submitAttempt(e);
  });
  $("concept").addEventListener("change", () => selectConcept($("concept").value));

  try {
    const [health, conceptList] = await Promise.all([getJSON("/health"), getJSON("/concepts")]);
    const badge = $("badge");
    if (health.llm_mode === "real") {
      badge.textContent = `Real · ${health.model_chain[0]}`;
      badge.className = "badge real";
      badge.title = `Real model calls (fallbacks: ${health.model_chain.slice(1).join(", ")})`;
    } else {
      badge.textContent = "Fake LLM";
      badge.className = "badge fake";
      badge.title = "Dev mode: offline fake model, no API calls. Set LLM_MODE=real for the real model.";
    }

    concepts = conceptList;
    byId = Object.fromEntries(concepts.map((c) => [c.id, c]));
    $("concept").replaceChildren(...concepts.map((c) => el("option", { value: c.id }, c.name)));
    selectConcept("overfitting", { focus: false });
    await refreshState();
  } catch (err) {
    showError(`Couldn't load the app data: ${err.message}. Is uvicorn running?`);
  }
  $("transcript").focus();
}

boot();
