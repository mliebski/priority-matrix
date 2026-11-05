"use strict";

const QUADRANTS = [
  { key: "do", name: "Do", axes: "Important, urgent", hint: "Do it yourself, soon." },
  { key: "schedule", name: "Schedule", axes: "Important, not urgent", hint: "Where the real progress is. Block time for it." },
  { key: "delegate", name: "Delegate", axes: "Urgent, not important", hint: "Hand it off, or do it quickly with little care." },
  { key: "drop", name: "Drop", axes: "Neither", hint: "Give yourself permission to let it go." },
];
const quadrantName = key => QUADRANTS.find(q => q.key === key)?.name ?? key;

const $ = id => document.getElementById(id);
const state = { items: [], goals: "", model: "", today: "", ai: null };
const coach = { id: null, sorting: false, busy: null, error: "", opening: null };  // busy: the item being waited on

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof data.detail === "string" ? data.detail : "That didn't work.";
    throw Object.assign(new Error(detail), { status: res.status });
  }
  return data;
}

function toast(message) {
  const box = $("toast");
  box.textContent = message;
  box.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { box.hidden = true; }, 5000);
}

// Runs a change the user asked for and reports failure, so no click fails silently.
function attempt(work) {
  return work().catch(e => toast(e.message));
}

const item = id => state.items.find(i => i.id === id);
const inbox = () => state.items.filter(i => !i.quadrant && !i.done_at);

function replaceItem(updated) {
  state.items = state.items.map(i => (i.id === updated.id ? updated : i));
}

// The list comes without conversations (chat is null); keep the ones already fetched if unchanged.
async function load() {
  const fresh = await api("GET", "/api/state");
  const before = new Map(state.items.map(i => [i.id, i]));
  fresh.items = fresh.items.map(i => {
    const old = before.get(i.id);
    return old?.chat && old.version === i.version ? { ...i, chat: old.chat } : i;
  });
  Object.assign(state, fresh);
  if (coach.id) await ensureChat(coach.id);
  render();
}

async function ensureChat(id) {
  if (item(id)?.chat) return;
  replaceItem(await api("GET", `/api/items/${id}`));
}

async function checkAi() {
  try {
    state.ai = await api("GET", "/api/ai");
  } catch {
    state.ai = { ok: false, detail: "Can't check the AI right now.", models: [], model: "" };
  }
  const label = $("ai-status");
  label.textContent = state.ai.ok ? state.ai.detail : `AI off: ${state.ai.detail}`;
  label.classList.toggle("bad", !state.ai.ok);
  if (coach.id) renderCoach();
}

/* ---------- Cards and the matrix ---------- */

function dueLabel(i) {
  if (!i.due) return null;
  const today = state.today;  // the server's date, the one the coach uses too
  const date = new Date(`${i.due}T00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  if (i.due < today) return { text: `Overdue, ${date}`, late: true };
  return { text: i.due === today ? "Due today" : `Due ${date}`, late: i.due === today };
}

function card(i) {
  const node = el("div", "card");
  node.dataset.id = i.id;
  node.classList.toggle("selected", coach.id === i.id);
  node.classList.toggle("finished", Boolean(i.done_at));
  node.draggable = !i.done_at;
  node.title = i.done_at ? "" : "Open it to place it in a square, or drag it there";
  node.addEventListener("dragstart", e => {
    e.dataTransfer.setData("text/plain", i.id);
    node.classList.add("dragging");
  });
  node.addEventListener("dragend", () => node.classList.remove("dragging"));

  if (!i.done_at) {
    const check = el("button", "card-check");
    check.title = "Mark done";
    check.setAttribute("aria-label", `Mark done: ${i.title}`);
    check.addEventListener("click", () => attempt(() => setDone(i.id, true)));
    node.append(check);
  }
  const main = el("button", "card-main", i.title);
  const due = dueLabel(i);
  if (due && !i.done_at) main.append(el("span", `card-meta${due.late ? " late" : ""}`, due.text));
  if (i.reason && !i.done_at) main.append(el("span", "card-why", i.reason));
  main.addEventListener("click", () => openCoach(i.id, false));
  node.append(main);
  if (i.done_at) {
    const undo = el("button", "quiet", "Undo");
    undo.addEventListener("click", () => attempt(() => setDone(i.id, false)));
    node.append(undo);
  }
  return node;
}

function dropTarget(node, quadrant) {
  node.addEventListener("dragover", e => { e.preventDefault(); node.classList.add("over"); });
  node.addEventListener("dragleave", e => {
    if (!node.contains(e.relatedTarget)) node.classList.remove("over");
  });
  node.addEventListener("drop", e => {
    e.preventDefault();
    node.classList.remove("over");
    const id = Number(e.dataTransfer.getData("text/plain"));
    if (id && item(id)?.quadrant !== quadrant) attempt(() => place(id, quadrant));
  });
}

function buildMatrix() {
  const matrix = $("matrix");
  for (const q of QUADRANTS) {
    const box = el("div", `quadrant q-${q.key}`);
    box.append(el("h3", "", q.name));
    const hint = el("p", "", q.hint);
    hint.dataset.axes = q.axes;
    box.append(hint);
    const cards = el("div", "cards");
    cards.id = `cards-${q.key}`;
    box.append(cards);
    dropTarget(box, q.key);
    matrix.append(box);
  }
  dropTarget($("inbox"), null);
}

function render() {
  const waiting = inbox();
  $("inbox-count").textContent = waiting.length ? `(${waiting.length})` : "";
  $("inbox-list").replaceChildren(...waiting.map(card));
  $("sort-next").hidden = waiting.length === 0;
  $("goals-nudge").hidden = Boolean(state.goals);

  for (const q of QUADRANTS) {
    const here = state.items.filter(i => i.quadrant === q.key && !i.done_at);
    $(`cards-${q.key}`).replaceChildren(...here.map(card));
  }
  const finished = state.items.filter(i => i.done_at);
  $("finished").hidden = finished.length === 0;
  $("finished-count").textContent = `(${finished.length})`;
  $("finished-list").replaceChildren(...finished.map(card));
  if (coach.id) renderCoach();
}

/* ---------- Changes ---------- */

async function setDone(id, done) {
  replaceItem(await api("PATCH", `/api/items/${id}`, { done }));
  if (done && coach.id === id) nextOrClose();
  render();
}

async function place(id, quadrant) {
  replaceItem(await api("PATCH", `/api/items/${id}`, { quadrant }));
  if (coach.id === id && quadrant) nextOrClose();
  render();
}

function nextOrClose() {
  const next = coach.sorting ? inbox().find(i => i.id !== coach.id) : null;
  if (next) openCoach(next.id, true);
  else closeCoach();
}

/* ---------- Coach ---------- */

async function openCoach(id, sorting) {
  coach.opening = id;
  try {
    await ensureChat(id);
  } catch (e) {
    toast(e.message);
    return;
  }
  if (coach.opening !== id || !item(id)) return;  // another item was opened meanwhile, or this one is gone
  coach.id = id;
  coach.sorting = sorting;
  coach.error = "";
  $("coach").hidden = false;
  $("answer").value = "";
  const i = item(id);
  $("coach-title").value = i.title;
  $("coach-due").value = i.due ?? "";
  render();
  // Opening an unsorted item starts the conversation; a placed one waits to be asked.
  if (!i.chat?.length && !i.quadrant) startCoach(id);
}

function closeCoach() {
  coach.id = null;
  coach.opening = null;
  coach.sorting = false;
  $("coach").hidden = true;
  render();
}

async function askCoach(id, text) {
  if (!state.ai?.ok) await checkAi();
  if (!state.ai.ok) return false;
  if (coach.busy) {
    toast("The coach is still working on another item. Wait for its reply first.");
    return false;
  }
  coach.busy = id;
  coach.error = "";
  renderCoach();
  const started = Date.now();
  const timer = setInterval(() => {
    const status = document.querySelector("#chat .status");
    if (status) status.textContent = `Thinking… ${Math.round((Date.now() - started) / 1000)}s`;
  }, 1000);
  let answered = false;
  try {
    replaceItem(await api("POST", `/api/items/${id}/coach`, { text }));
    answered = true;
  } catch (e) {
    coach.error = e.message;
    checkAi();
    if (e.status === 409) load().catch(() => {});  // show the newer conversation
  }
  clearInterval(timer);
  coach.busy = null;
  render();
  if (coach.id === id) $("answer").focus();
  return answered;
}

function startCoach(id) {
  return askCoach(id, "");
}

function pendingAnswer(i) {
  return coach.busy === i.id ? $("answer").value.trim() : "";
}

function renderCoach() {
  const i = item(coach.id);
  if (!i) return closeCoach();
  const busyHere = coach.busy === i.id;
  const turns = i.chat ?? [];
  const nodes = turns.map(m => el("div", `msg from-${m.role}`, m.text));

  const pending = pendingAnswer(i);
  if (pending) nodes.push(el("div", "msg from-you", pending));
  if (busyHere) nodes.push(el("div", "msg status", "Thinking…"));

  if (i.suggestion && !busyHere) {
    const box = el("div", "suggest");
    box.append(el("strong", "", `Looks like: ${quadrantName(i.suggestion.quadrant)}`));
    if (i.quadrant !== i.suggestion.quadrant) {
      const accept = el("button", "primary", `Put it in ${quadrantName(i.suggestion.quadrant)}`);
      accept.addEventListener("click", () => attempt(() => place(i.id, i.suggestion.quadrant)));
      box.append(accept);
    }
    box.append(el("span", "hint", "Not right? Tell me why below, or pick a square yourself."));
    nodes.push(box);
  }
  if (coach.error) nodes.push(el("div", "msg error", coach.error));
  if (!state.ai?.ok && state.ai && !busyHere) {
    nodes.push(el("div", "msg from-coach",
      "The AI isn't available, but you can still decide. Ask yourself: would it matter in a year? " +
      "Does a real deadline hit this week? Then pick a square below."));
  } else if (!turns.length && !busyHere && !coach.error) {
    const start = el("button", "primary", "Talk it through");
    start.addEventListener("click", () => startCoach(i.id));
    nodes.push(start);
  } else if (!turns.length && coach.error && !busyHere) {
    const retry = el("button", "primary-outline", "Try again");
    retry.addEventListener("click", () => startCoach(i.id));
    nodes.push(retry);
  }

  const chat = $("chat");
  chat.replaceChildren(...nodes);
  chat.scrollTop = chat.scrollHeight;

  const canAnswer = state.ai?.ok && turns.length > 0 && !coach.busy;
  $("answer").disabled = !canAnswer && !busyHere;
  $("answer-send").disabled = !canAnswer;
  $("answer-form").hidden = !turns.length;
  $("coach-restart").hidden = !turns.length;
  $("coach-done").textContent = i.done_at ? "Not done" : "Mark done";

  $("picker").replaceChildren(...QUADRANTS.map(q => {
    const button = el("button", `pick q-${q.key}`, q.name);
    button.append(el("small", "", q.axes));
    button.classList.toggle("current", i.quadrant === q.key);
    button.classList.toggle("suggested", i.suggestion?.quadrant === q.key);
    button.addEventListener("click", () => attempt(() => place(i.id, q.key)));
    return button;
  }));
}

async function sendAnswer() {
  const text = $("answer").value.trim();
  if (!text || coach.busy || !coach.id) return;
  const id = coach.id;
  if (await askCoach(id, text) && coach.id === id) $("answer").value = "";
}

/* ---------- Wiring ---------- */

function submitOnEnter(textarea, submit) {
  textarea.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      submit();
    }
  });
}

async function addItems() {
  const box = $("add-text");
  if (!box.value.trim()) return;
  const res = await api("POST", "/api/items", { text: box.value });
  box.value = "";
  if (res.shortened) toast(`${res.shortened} item(s) were over 500 characters and were shortened.`);
  await load();
}

async function openSettings() {
  await checkAi();
  $("goals").value = state.goals;
  const select = $("model");
  const names = state.ai.models;
  const current = state.model || state.ai.model;
  select.replaceChildren(el("option", "", names.length ? "Choose a model" : "No models found"));
  select.firstChild.value = "";
  for (const name of names) select.append(el("option", "", name));
  select.value = names.includes(current) ? current : "";
  $("settings-ai").textContent = state.ai.detail;
  $("settings").showModal();
}

async function saveSettings() {
  await api("PUT", "/api/settings", { goals: $("goals").value, model: $("model").value });
  $("settings").close();
  await load();
  await checkAi();
}

buildMatrix();
submitOnEnter($("add-text"), () => attempt(addItems));
submitOnEnter($("answer"), sendAnswer);
$("add-form").addEventListener("submit", e => { e.preventDefault(); attempt(addItems); });
$("answer-form").addEventListener("submit", e => { e.preventDefault(); sendAnswer(); });
$("sort-next").addEventListener("click", () => openCoach(inbox()[0].id, true));
$("goals-nudge").addEventListener("click", () => attempt(openSettings));
$("open-settings").addEventListener("click", () => attempt(openSettings));
$("settings-cancel").addEventListener("click", () => $("settings").close());
$("settings-form").addEventListener("submit", e => { e.preventDefault(); attempt(saveSettings); });
$("coach-close").addEventListener("click", closeCoach);
$("coach-title").addEventListener("change", e => attempt(async () => {
  const id = coach.id;
  if (!id) return;
  try {
    replaceItem(await api("PATCH", `/api/items/${id}`, { title: e.target.value }));
  } finally {
    if (coach.id === id) e.target.value = item(id).title;  // show what was really saved
  }
  render();
}));
$("coach-due").addEventListener("change", e => attempt(async () => {
  const id = coach.id;
  if (!id) return;
  try {
    replaceItem(await api("PATCH", `/api/items/${id}`, { due: e.target.value || null }));
  } finally {
    if (coach.id === id) e.target.value = item(id).due ?? "";
  }
  render();
}));
$("coach-done").addEventListener("click", () => attempt(() => setDone(coach.id, !item(coach.id).done_at)));
$("coach-restart").addEventListener("click", () => attempt(async () => {
  const id = coach.id;
  replaceItem(await api("DELETE", `/api/items/${id}/coach`));
  coach.error = "";
  render();
  startCoach(id);
}));
$("coach-delete").addEventListener("click", () => attempt(async () => {
  const i = item(coach.id);
  if (!confirm(`Delete "${i.title}"?`)) return;
  await api("DELETE", `/api/items/${i.id}`);
  const wasSorting = coach.sorting;
  const next = wasSorting ? inbox().find(x => x.id !== i.id) : null;
  state.items = state.items.filter(x => x.id !== i.id);
  if (next) openCoach(next.id, true);
  else closeCoach();
}));
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && coach.id && !$("settings").open) closeCoach();
});

attempt(load).then(checkAi);
