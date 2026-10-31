"use strict";

const STEPS = [
  { id: "clean", label: "Clean", tools: "claims, self-corrections, hesitation" },
  { id: "grade", label: "Grade", tools: "compare with reference notes" },
  { id: "update", label: "Update", tools: "mastery, misconceptions" },
  { id: "decide", label: "Decide", tools: "pick the next action" },
  { id: "teach", label: "Teach", tools: "mini-lesson if needed" },
];
const START_STEPS = [{ id: "choose", label: "Choose", tools: "agent picks a concept with tools" }];
const EXTRA_STEPS = {
  choose: START_STEPS[0],
  plan: { id: "plan", label: "Plan", tools: "study plan for next time" },
};

// Turn an API/stream error into a plain message, and whether retrying makes sense.
function friendlyError(status, detail = "") {
  const d = String(detail).toLowerCase();
  if (status === 0) return { message: "Can't reach the server. Check your connection, then retry.", retry: true };
  if (status === 404) return { message: "That item no longer exists (the data may have been reset). Reload the page.", retry: false };
  if (status === 409) return { message: "This session has already ended. Start a new one.", retry: false };
  if (status === 502) {
    if (/used its \d+ ai requests/.test(d)) {
      return { message: "This session has used its AI request budget. End the session to see your study plan.", retry: false };
    }
    if (/all models failed|429|quota|exhausted|resource/.test(d)) {
      return { message: "The free AI models are out of requests right now. Wait a minute and retry, or try later.", retry: true };
    }
    if (/timeout|deadline|504|slow/.test(d)) {
      return { message: "The AI is responding slowly right now. Retry in a moment.", retry: true };
    }
    return { message: "The AI service had a problem with this request. Retry in a moment.", retry: true };
  }
  return { message: "Something went wrong on the server. Retry, and if it keeps happening, reload the page.", retry: true };
}

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

// An optional text field is absent if it's null or a placeholder string like "null".
function present(value) {
  if (typeof value !== "string") return null;
  const v = value.trim();
  return ["", "null", "none", "n/a", "na", "undefined"].includes(v.toLowerCase()) ? null : v;
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
  renderGraph();
  loadHistory(id);
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

// "2026-10-30 09:00:00" (UTC) -> "overdue by 2 days" / "due today"
function dueText(nextReviewAt, now = new Date()) {
  const due = new Date(`${nextReviewAt.replace(" ", "T")}Z`);
  const days = Math.floor((now - due) / 86400000);
  if (days <= 0) return "due today";
  return `overdue by ${days} day${days === 1 ? "" : "s"}`;
}

function renderDue(due) {
  fillList($("due-reviews"), due, (d) =>
    el("li", {},
      el("span", {}, d.name, el("span", { class: "when" }, `${dueText(d.next_review_at)} · mastery ${Math.round(d.mastery * 100)}%`)),
      el("button", {
        type: "button", class: "quiet", disabled: activeSession ? "" : null,
        onclick: () => { if (!activeSession) selectConcept(d.concept_id); },
      }, "Start")
    ), "Nothing due. Good attempts push the next review further out.");
  for (const b of $("due-reviews").querySelectorAll("button")) b.disabled = Boolean(activeSession);
}

async function refreshState() {
  const [s, due] = await Promise.all([getJSON("/state"), getJSON("/reviews/due")]);
  state = s;
  renderDue(due);
  renderMastery();
  $("sample-badge").hidden = !state.sample_loaded;
  if (!$("panel-graph").hidden) await loadGraph();
}

// ---------- demo tools (no LLM calls) ----------

function demoMessage(text, isError = false) {
  const m = $("demo-message");
  m.textContent = text;
  m.className = `small ${isError ? "error" : "muted"}`;
  m.hidden = false;
}

async function loadSample() {
  if (busy) return;
  const resp = await fetch("/demo/sample", { method: "POST" });
  const data = await resp.json();
  if (!resp.ok) return demoMessage(data.detail ?? `Couldn't load sample data (status ${resp.status}).`, true);
  demoMessage(`Loaded a sample learner: ${data.concepts} concepts, ${data.attempts} sample attempts.`);
  await refreshState();
  loadHistory($("concept").value);
}

let resetArmed = null;
async function resetProgress() {
  if (busy) return;
  const button = $("reset-progress");
  if (!resetArmed) { // first click arms it; a second click within 4s resets
    button.textContent = "Click again to reset";
    button.classList.add("confirm");
    resetArmed = setTimeout(() => {
      resetArmed = null;
      button.textContent = "Reset progress";
      button.classList.remove("confirm");
    }, 4000);
    return;
  }
  clearTimeout(resetArmed);
  resetArmed = null;
  button.textContent = "Reset progress";
  button.classList.remove("confirm");
  const resp = await fetch("/demo/reset", { method: "POST" });
  if (!resp.ok) return demoMessage(`Couldn't reset (status ${resp.status}).`, true);
  activeSession = lastSession = null;
  renderSession({ status: "finished", steps: [], call_count: 0, max_calls: 0, message: null });
  $("session-status").textContent = "";
  $("result").hidden = $("lesson").hidden = true;
  resetTrace();
  demoMessage("All progress deleted. Concepts are kept.");
  await refreshState();
  loadHistory($("concept").value);
}

// ---------- prerequisite graph (plain SVG) ----------

// grey = untouched, red < 0.4, yellow 0.4–0.7, green > 0.7
function masteryBand(mastery, attempts) {
  if (!attempts) return "untouched";
  if (mastery < 0.4) return "low";
  if (mastery <= 0.7) return "mid";
  return "high";
}

// Wrap a concept name onto at most two lines of ~maxChars.
function splitLabel(name, maxChars = 18) {
  if (name.length <= maxChars) return [name];
  const words = name.split(" ");
  let first = "";
  while (words.length && (first + " " + words[0]).trim().length <= maxChars) first = `${first} ${words.shift()}`.trim();
  if (!first) first = words.shift();
  let second = words.join(" ");
  if (second.length > maxChars) second = `${second.slice(0, maxChars - 1)}…`;
  return second ? [first, second] : [first];
}

const G = { col: 150, row: 54, w: 128, h: 40, pad: 12 };
let lastSession = null; // keep the walked path visible after a session ends
let graphData = null;

function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  node.append(...children.filter(Boolean));
  return node;
}

function curve(x1, y1, x2, y2) {
  const dx = Math.max(30, Math.abs(x2 - x1) / 2);
  return `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`;
}

async function loadGraph() {
  graphData = await getJSON("/graph");
  renderGraph();
}

function renderGraph() {
  if (!graphData) return;
  const pos = Object.fromEntries(graphData.nodes.map((n) => [n.id,
    { x: G.pad + n.depth * G.col, y: G.pad + n.row * G.row }]));
  const width = G.pad * 2 + (graphData.columns - 1) * G.col + G.w;
  const height = G.pad * 2 + (graphData.max_rows - 1) * G.row + G.h;
  const session = activeSession || lastSession;
  const current = activeSession?.current?.concept_id;
  const selected = $("concept").value;

  const defs = svg("defs", {},
    svg("marker", { id: "arrow", viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "6", markerHeight: "6", orient: "auto" },
      svg("path", { d: "M0,0 L10,5 L0,10 z", fill: "#a9b6c2" })),
    svg("marker", { id: "walk-arrow", viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "6", markerHeight: "6", orient: "auto" },
      svg("path", { d: "M0,0 L10,5 L0,10 z", fill: "#3a5bd9" })));

  const edges = graphData.edges.filter((e) => pos[e.from] && pos[e.to]).map((e) => {
    const a = pos[e.from], b = pos[e.to];
    return svg("path", { class: "edge", d: curve(a.x + G.w, a.y + G.h / 2, b.x, b.y + G.h / 2), "marker-end": "url(#arrow)" });
  });

  // the session's walked path, in question order (walk-backs go right-to-left)
  const ids = (session?.steps ?? []).map((s) => s.concept_id);
  const walk = [];
  for (let i = 1; i < ids.length; i++) {
    if (ids[i] === ids[i - 1] || !pos[ids[i]] || !pos[ids[i - 1]]) continue;
    const a = pos[ids[i - 1]], b = pos[ids[i]];
    const forward = b.x >= a.x;
    walk.push(svg("path", {
      class: "walk", "marker-end": "url(#walk-arrow)",
      d: curve(forward ? a.x + G.w : a.x, a.y + G.h / 2, forward ? b.x : b.x + G.w, b.y + G.h / 2),
    }));
  }

  const nodes = graphData.nodes.map((n) => {
    const p = pos[n.id];
    const lines = splitLabel(n.name);
    const cls = ["node", masteryBand(n.mastery, n.attempts), n.id === selected && "selected", n.id === current && "current"]
      .filter(Boolean).join(" ");
    const pct = n.attempts ? `${Math.round(n.mastery * 100)}%` : "not tried";
    const g = svg("g", { class: cls, tabindex: "0", role: "button", "aria-label": `${n.name}, ${pct}` },
      svg("title", {}, `${n.name}: ${pct}`),
      svg("rect", { x: p.x, y: p.y, width: G.w, height: G.h, rx: "6" }),
      ...lines.map((line, i) => svg("text", {
        x: p.x + G.w / 2, y: p.y + G.h / 2 + (i - (lines.length - 1) / 2) * 13 + 4, "text-anchor": "middle",
      }, line)));
    const choose = () => { if (!activeSession) selectConcept(n.id); };
    g.addEventListener("click", choose);
    g.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); choose(); } });
    return g;
  });

  const wrap = $("graph-wrap");
  wrap.replaceChildren(svg("svg", {
    width, height, viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "Prerequisite graph",
  }, defs, ...edges, ...walk, ...nodes));
  const focus = pos[current] || pos[selected];  // scroll the narrow panel to the concept in play
  if (focus) wrap.scrollLeft = Math.max(0, focus.x - G.col / 2);
}

function showTab(which) {
  const graph = which === "graph";
  $("tab-progress").setAttribute("aria-selected", String(!graph));
  $("tab-graph").setAttribute("aria-selected", String(graph));
  $("panel-progress").hidden = graph;
  $("panel-graph").hidden = !graph;
  if (graph) loadGraph();
}

// ---------- live trace ----------

function stepRow(s) {
  return el("li", { class: "step", id: `step-${s.id}` },
    el("div", { class: "step-head" }, s.label),
    el("div", { class: "step-meta" }),
    el("div", { class: "step-tools" }, s.tools)
  );
}

function resetTrace(steps = STEPS) {
  $("trace-error").hidden = true;
  $("trace-why").hidden = true;
  $("tool-calls").hidden = true;
  $("tool-list").replaceChildren();
  $("tool-count").textContent = "0";
  $("steps").replaceChildren(...steps.map(stepRow));
}

function onStepStart(step) {
  if (!$(`step-${step}`)) $("steps").append(stepRow(EXTRA_STEPS[step] ?? { id: step, label: step, tools: "" }));
  $(`step-${step}`).classList.add("running");
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

// "tool.pick_next_concept" + {strategy: "due"} -> "pick_next_concept(strategy=due)"
function toolLabel(toolName, args) {
  const name = toolName.replace(/^tool\./, "");
  const parts = Object.entries(args || {}).map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`);
  return `${name}(${parts.join(", ")})`;
}

function onTool({ step, tool_name, args, result, elapsed_s, model }) {
  const row = $(`step-${step}`);
  if (row) {
    const tools = row.querySelector(".step-tools");
    if (!row.dataset.calls) tools.textContent = "";
    row.dataset.calls = `${row.dataset.calls || ""}${tool_name.replace(/^tool\./, "")} `;
    tools.textContent = row.dataset.calls.trim().split(" ").join(", ");
  }
  const secs = elapsed_s == null ? "" : ` ${Number(elapsed_s).toFixed(2)}s`;
  $("tool-list").append(el("li", {},
    el("code", {}, toolLabel(tool_name, args)),
    `${model ? ` [${model}]` : ""}${secs} `,
    el("span", { class: "res" }, `→ ${result ?? ""}`)
  ));
  $("tool-count").textContent = String($("tool-list").children.length);
  $("tool-calls").hidden = false;
}

let lastAction = null; // what Retry re-runs

function showError(message, retry = null) {
  for (const row of document.querySelectorAll(".step.running")) {
    row.classList.replace("running", "failed");
    row.querySelector(".step-meta").textContent = "failed";
  }
  $("trace-error-text").textContent = message;
  $("retry").hidden = !retry;
  $("retry").onclick = retry ? () => { $("trace-error").hidden = true; retry(); } : null;
  $("trace-error").hidden = false;
}

function showFriendlyError(status, detail) {
  const f = friendlyError(status, detail);
  showError(f.message, f.retry ? lastAction : null);
}

// Show "still working" if a request takes longer than 8s; returns a function that clears it.
function slowNotice() {
  const timer = setTimeout(() => { $("trace-slow").hidden = false; }, 8000);
  return () => { clearTimeout(timer); $("trace-slow").hidden = true; };
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
  const question = present(d.followup_question);
  const target = present(d.target_concept_id);
  const buttons = [];
  if (question || d.action === "re_explain") {
    buttons.push(el("button", {
      type: "button", class: "quiet",
      onclick: () => { $("transcript").value = ""; updateWordCount(); $("transcript").focus(); },
    }, question ? "Answer it" : "Try again"));
  }
  if (target && byId[target]) {
    buttons.push(el("button", {
      type: "button", class: "quiet",
      onclick: () => { $("transcript").value = ""; updateWordCount(); selectConcept(target); },
    }, `Go to ${byId[target].name}`));
  }
  $("next").replaceChildren(
    el("span", { class: "action" }, ACTIONS[d.action] ?? d.action),
    el("span", {}, d.reason),
    question ? el("p", { class: "question" }, question) : null,
    buttons.length ? el("div", { class: "buttons" }, buttons) : null
  );
  $("result").hidden = false;
}

// ---------- attempt history ----------

// "2026-10-29 14:05:00" (UTC) -> "Oct 29"
function shortDate(utc) {
  const d = new Date(`${utc.replace(" ", "T")}Z`);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}

async function loadHistory(conceptId) {
  const items = await getJSON(`/concepts/${encodeURIComponent(conceptId)}/history`).catch(() => []);
  if (conceptId !== $("concept").value) return; // the user moved on while this loaded
  $("history").hidden = items.length === 0;
  $("history-concept").textContent = byId[conceptId]?.name ?? conceptId;
  $("history-list").replaceChildren(...items.slice().reverse().map((h) =>
    el("li", {},
      el("span", { class: "date" }, shortDate(h.created_at)),
      "mastery after ", el("span", { class: "m" }, `${Math.round(h.mastery_after * 100)}%`),
      h.is_sample ? el("span", { class: "sample-tag" }, "sample") : null,
      h.misconceptions.length ? el("span", { class: "misc" }, `Misconception: ${h.misconceptions.join("; ")}`) : null)));
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
    if (!resp.ok) throw new Error(friendlyError(resp.status, data.detail).message);
    feedback.textContent = data.correct
      ? `${data.feedback} Mastery now ${Math.round(data.state.mastery * 100)}%.`
      : `${data.feedback} Try again.`;
    feedback.className = `check-feedback ${data.correct ? "right" : "wrong"}`;
    $("check-answer").disabled = data.correct;
    lastUpdated = data.state.concept_id;
    await refreshState();
    button.disabled = data.correct;
  } catch (err) {
    feedback.textContent = err.message;
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

let activeSession = null; // SessionView while a study session is running

// POST json and feed each server-sent event to handleEvent.
async function streamPost(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!resp.ok) throw Object.assign(new Error(`The server returned ${resp.status}.`), { status: resp.status });
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
}

async function submitAttempt(event) {
  event?.preventDefault();
  if (busy) return;
  const transcript = $("transcript").value.trim();
  if (!transcript) {
    $("transcript").focus();
    return;
  }
  busy = true;
  lastAction = () => submitAttempt();
  const button = $("submit");
  button.disabled = true;
  button.textContent = "Checking…";
  resetTrace();
  $("lesson").hidden = true;
  const clearSlow = slowNotice();

  try {
    if (activeSession) {
      await streamPost(`/session/${activeSession.id}/answer`, { transcript });
    } else {
      await streamPost("/attempt/stream", { concept_id: $("concept").value, transcript });
    }
  } catch (err) {
    showFriendlyError(err.status ?? 0, err.message);
  } finally {
    clearSlow();
    busy = false;
    button.disabled = false;
    button.textContent = activeSession ? "Send answer" : "Check my explanation";
  }
}

async function startSession() {
  if (busy) return;
  busy = true;
  lastAction = () => startSession();
  $("start-session").disabled = true;
  $("session-message").hidden = true;
  $("plan").hidden = true;
  resetTrace(START_STEPS);
  const clearSlow = slowNotice();
  try {
    await streamPost("/session/start");
  } catch (err) {
    showFriendlyError(err.status ?? 0, err.message);
  } finally {
    clearSlow();
    busy = false;
    $("start-session").disabled = false;
  }
}

async function endSession() {
  if (!activeSession || busy) return;
  busy = true;
  lastAction = () => endSession();
  $("end-session").disabled = true;
  try {
    const resp = await fetch(`/session/${activeSession.id}/end`, { method: "POST" });
    const data = await resp.json();
    if (!resp.ok) throw Object.assign(new Error(data.detail ?? ""), { status: resp.status });
    renderSession(data);
    await refreshState();
  } catch (err) {
    showFriendlyError(err.status ?? 0, err.message);
  } finally {
    $("end-session").disabled = false;
    busy = false;
  }
}

// Plain-text version of a study plan for the clipboard.
function planText(plan) {
  const lines = ["TeachBack study plan", plan.summary, ""];
  plan.steps.forEach((s, i) => {
    lines.push(`${i + 1}. ${s.concept_name}: ${s.what_to_do}`);
    if (s.misconception) lines.push(`   Fix: ${s.misconception}`);
    lines.push(`   Review by ${s.next_review}`);
  });
  return lines.join("\n");
}

function renderPlan(plan) {
  $("plan").hidden = !plan;
  if (!plan) return;
  $("plan-summary").textContent = plan.summary;
  $("plan-steps").replaceChildren(...plan.steps.map((s) =>
    el("li", {},
      el("strong", {}, s.concept_name), `: ${s.what_to_do}`,
      el("span", { class: "review" }, `Review by ${s.next_review}`))));
  $("plan-source").textContent = plan.source === "offline"
    ? "Built from your results by rules, without an AI call (nothing answered yet, or the request budget or quota ran out)."
    : "";
  $("plan-source").hidden = plan.source !== "offline";
  $("copy-plan").onclick = async () => {
    const text = planText(plan);
    try {
      await navigator.clipboard.writeText(text);
    } catch { // clipboard API blocked (http, iframe): fall back to a temporary textarea
      const t = el("textarea", { style: "position:fixed;opacity:0" });
      t.value = text;
      document.body.append(t);
      t.select();
      document.execCommand("copy");
      t.remove();
    }
    $("copy-plan").textContent = "Copied";
    setTimeout(() => { $("copy-plan").textContent = "Copy"; }, 1500);
  };
}

const ACTION_SHORT = {
  re_explain: "re-explain", ask_followup: "follow-up", revisit_prerequisite: "walk back to a prerequisite",
  advance: "move on", review_due: "review",
};

function renderSession(view) {
  const active = view.status === "active" && view.current;
  activeSession = active ? view : null;
  lastSession = view;
  $("start-session").hidden = Boolean(active);
  $("end-session").hidden = !active;
  $("concept").disabled = Boolean(active);
  $("session-status").textContent = `AI requests used: ${view.call_count} of ${view.max_calls}`;
  $("session-card").hidden = !active;
  $("prompt-default").hidden = Boolean(active);
  $("prompt-session").hidden = !active;
  $("submit").textContent = active ? "Send answer" : "Check my explanation";

  if (active) {
    const c = view.current;
    $("session-progress").textContent = `Question ${c.step_no} of ${view.max_questions}: ${c.concept_name}`;
    $("session-question").textContent = c.question;
    $("session-why").textContent = c.reason;
    $("trace-why").textContent = c.reason;
    $("trace-why").hidden = false;
    selectConcept(c.concept_id);
    $("transcript").value = "";
    updateWordCount();
  }
  $("session-message").textContent = view.message ?? "";
  $("session-message").hidden = !view.message || Boolean(active);
  renderPlan(active ? null : view.plan);

  $("session-log").hidden = view.steps.length === 0;
  $("session-steps").replaceChildren(...view.steps.map((s) =>
    el("li", {},
      `${s.concept_name} · ${ACTION_SHORT[s.action] ?? s.action}`,
      s.mastery_after == null ? " · waiting for your answer" : ` · mastery after ${Math.round(s.mastery_after * 100)}%`,
      el("span", { class: "why" }, `Why: ${s.reason}`)
    )
  ));
}

async function handleEvent(e) {
  if (e.type === "step_start") onStepStart(e.step);
  else if (e.type === "step_done") onStepDone(e);
  else if (e.type === "step_skipped") onStepSkipped(e.step);
  else if (e.type === "tool") onTool(e);
  else if (e.type === "error") showFriendlyError(e.status, e.detail);
  else if (e.type === "result") {
    lastUpdated = e.data.concept_id;
    await refreshState();
    renderResult(e.data);
    renderLesson(e.data);
    loadHistory(e.data.concept_id);
  } else if (e.type === "session") {
    renderSession(e.data);
    await refreshState(); // after renderSession so the graph sees the new path
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
  $("start-session").addEventListener("click", startSession);
  $("load-sample").addEventListener("click", loadSample);
  $("reset-progress").addEventListener("click", resetProgress);
  $("tab-progress").addEventListener("click", () => showTab("progress"));
  $("tab-graph").addEventListener("click", () => showTab("graph"));
  $("end-session").addEventListener("click", endSession);
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
    showError("Couldn't load the app data. Check that the server is running, then retry.", () => location.reload());
  }
  $("transcript").focus();
}

boot();
