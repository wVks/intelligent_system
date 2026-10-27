const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

const appSource = fs.readFileSync(process.env.STUDYNAVIGATOR_WEB_APP || path.join(__dirname, "../../web/app.js"), "utf8");
const uploaded = { assignment_id: "upload-a", title: "Работа A", is_uploaded: true, status: "awaiting_analysis", sources: [], documents: [] };
const seed = { assignment_id: "personal-project", title: "Личный проект", status: "ready", sources: [] };
const card = {
  title: "Работа A", goal: { text: "Изучить тему", origin: "user", source_ids: [] },
  deliverable: { text: "Отчёт", origin: "user", source_ids: [] },
  requirements: [], criteria: [], deadline: { date: null, origin: "unknown", source_ids: [] }, tasks: [], questions: [],
};

// A small DOM substitute for state-transition tests; browser checks are separate.
function harness(fetchResponse) {
  const nodes = new Map();
  const documentListeners = new Map();
  let focused = null;
  function node(id = "") {
    return {
      id, hidden: true, _disabled: false, value: "", files: [], dataset: {}, children: [], attributes: {}, listeners: {},
      get disabled() { return this._disabled; },
      set disabled(value) { this._disabled = value; if (value && focused === this) focused = document.body; },
      classList: { add() {}, remove() {}, toggle() {} },
      setAttribute(name, value) { this.attributes[name] = value; },
      append(...items) { this.children.push(...items); },
      replaceChildren(...items) { this.children = items; },
      addEventListener(name, listener) { this.listeners[name] = listener; }, querySelectorAll() { return []; }, reportValidity() { return true; },
      focus() { focused = this; for (const listener of documentListeners.get("focusin") || []) listener({ target: this }); }, scrollIntoView() {},
    };
  }
  const document = {
    body: node("body"),
    getElementById(id) { if (!nodes.has(id)) nodes.set(id, node(id)); return nodes.get(id); },
    createElement() { return node(); }, createTextNode(text) { return { textContent: text }; }, querySelectorAll() { return []; },
    get activeElement() { return focused; }, hasFocus() { return true; },
    addEventListener(name, listener) { if (!documentListeners.has(name)) documentListeners.set(name, new Set()); documentListeners.get(name).add(listener); },
    removeEventListener(name, listener) { documentListeners.get(name)?.delete(listener); },
  };
  const calls = [];
  const context = vm.createContext({
    document, window: { location: { origin: "http://localhost" } }, URL, AbortController, FormData, TextEncoder, setTimeout, clearTimeout,
    fetch: async (url, options = {}) => {
      calls.push({ url, options });
      const result = await fetchResponse(url, options);
      if (options.signal?.aborted) throw new Error("Mock request aborted");
      return { ok: result.status < 400, status: result.status, json: async () => result.body };
    },
  });
  vm.runInContext(appSource.replace(/initialize\(\);\s*$/, "") + "\nglobalThis.app = { state, selectAssignment, submitUpload, deleteAssignment, confirmCard, submitPlan, updateControls };", context);
  const app = context.app;
  app.state.status = { mode: "offline" };
  app.state.assignments = [uploaded, seed];
  app.state.selectedId = uploaded.assignment_id;
  app.state.detail = uploaded;
  return { ...app, get: document.getElementById, calls, focused: () => focused?.id, active: () => focused };
}

function response(assignment) { return { status: 200, body: { assignment } }; }
function unavailable() { return { status: 503, body: { error: { message: "Сервис временно недоступен." } } }; }
function event() { return { preventDefault() {} }; }

test("switching assignments preserves an unsaved card and explains the next action", async () => {
  const app = harness(() => response(seed));
  app.state.cardDraft = card;
  app.state.cardDirty = true;
  app.get("card-goal").value = "Несохранённая цель";
  const opened = await app.selectAssignment(seed.assignment_id);
  assert.equal(app.state.selectedId, uploaded.assignment_id);
  assert.equal(app.state.cardDirty, true);
  assert.equal(app.get("card-goal").value, "Несохранённая цель");
  assert.equal(app.calls.length, 0);
  assert.match(app.get("card-status").textContent, /Отменить правки/);
  assert.equal(app.focused(), "card-confirm");
  assert.equal(opened, false);
});

test("explicit card reload can replace a dirty draft", async () => {
  const saved = { ...uploaded, status: "ready", card, card_revision: 2 };
  const app = harness(() => response(saved));
  app.state.cardDirty = true;
  await app.selectAssignment(uploaded.assignment_id, true);
  assert.equal(app.state.cardDirty, false);
  assert.equal(app.get("card-goal").value, card.goal.text);
});

test("failed assignment loading returns a retryable failure", async () => {
  const app = harness(unavailable);
  await app.selectAssignment(seed.assignment_id);
  assert.equal(app.state.loading, false);
  assert.equal(app.state.detail, null);
  assert.equal(app.get("sources").children.at(-1).textContent, "Загрузить задание ещё раз");
});

test("upload does not announce an unopened card as the next active screen", async () => {
  const app = harness((url, options) => options.method === "POST" ? response(uploaded) : unavailable());
  app.get("upload-files").files = [{ name: "task.pdf", size: 100 }];
  app.state.selectedId = seed.assignment_id;
  app.state.detail = seed;
  await app.submitUpload(event());
  assert.equal(app.state.assignments[0].assignment_id, uploaded.assignment_id);
  assert.match(app.get("upload-status").textContent, /повторите его открытие/);
  assert.notEqual(app.focused(), "card-heading");
  assert.equal(app.state.upload, null);
});

test("successful upload moves keyboard focus to the card heading", async () => {
  const app = harness(() => response(uploaded));
  app.get("upload-files").files = [{ name: "task.pdf", size: 100 }];
  app.state.selectedId = seed.assignment_id;
  app.state.detail = seed;
  await app.submitUpload(event());
  assert.equal(app.focused(), "card-heading");
  assert.equal(app.get("card-heading").attributes.tabindex, "-1");
});

test("successful card confirmation moves keyboard focus to planning", async () => {
  const confirmed = { ...uploaded, status: "ready", card, card_revision: 2 };
  const app = harness(() => response(confirmed));
  app.state.detail = { ...uploaded, card_revision: 1 };
  app.state.cardDraft = card;
  app.get("card-title").value = card.title;
  app.get("card-goal").value = card.goal.text;
  app.get("card-deliverable").value = card.deliverable.text;
  await app.confirmCard(event());
  assert.equal(app.focused(), "plan-title");
  assert.equal(app.state.confirmation, null);
});

test("successful deletion removes the item even when the next detail request fails", async () => {
  const app = harness((url, options) => options.method === "DELETE" ? { status: 204 } : unavailable());
  await app.deleteAssignment();
  assert.deepEqual(Array.from(app.state.assignments, (item) => item.assignment_id), [seed.assignment_id]);
  assert.equal(app.state.selectedId, seed.assignment_id);
  assert.equal(app.state.detail, null);
  assert.equal(app.get("sources").children.at(-1).textContent, "Загрузить задание ещё раз");
  assert.match(app.get("upload-status").textContent, /удалены/);
  assert.equal(app.calls.some((call) => call.url === "/api/assignments"), false);
  assert.equal(app.state.deleting, false);
});

test("ending cancelled operations restores keyboard focus from the hidden cancel button", () => {
  for (const [cancelId, nextId] of [["cancel-button", "question"], ["upload-cancel", "upload-button"], ["analyze-cancel", "analyze-button"]]) {
    const app = harness(() => response(seed));
    app.state.detail = { ...uploaded, status: "ready" };
    app.get("upload-files").files = [{ name: "task.pdf", size: 100 }];
    app.get(nextId).hidden = false;
    app.get(cancelId).focus();
    app.updateControls();
    assert.equal(app.get(cancelId).hidden, true);
    assert.equal(app.focused(), nextId);
  }
});

test("discarding card edits restores the saved fields and keyboard focus", () => {
  const app = harness(() => response(seed));
  app.state.detail = { ...uploaded, status: "ready", card };
  app.state.cardDraft = card;
  app.state.cardDirty = true;
  app.get("card-goal").value = "Несохранённая цель";
  app.get("card-discard").focus();
  app.get("card-discard").listeners.click();
  assert.equal(app.state.cardDirty, false);
  assert.equal(app.get("card-goal").value, card.goal.text);
  assert.equal(app.get("card-discard").hidden, true);
  assert.equal(app.focused(), "card-goal");
});

test("async plan restores keyboard focus without stealing it after mouse, user movement or cancellation", async () => {
  const result = { status: "insufficient_time", available_minutes: 120, required_minutes: 570, deficit_minutes: 450, schedule: [] };
  for (const scenario of ["keyboard-success", "keyboard-error", "mouse", "moved", "moved-back-to-body", "cancelled"]) {
    let finish;
    const app = harness(() => new Promise((resolve) => { finish = resolve; }));
    app.state.detail = seed;
    app.state.selectedId = seed.assignment_id;
    app.get("plan-days").value = "1";
    app.get("plan-minutes").value = "120";
    app.get("plan-button").focus();
    app.get("plan-button").listeners.click?.({ detail: scenario === "mouse" ? 1 : 0 });
    const pending = app.submitPlan(event());
    assert.equal(app.focused(), "body", scenario + ": disabling the active button loses focus");
    if (scenario.startsWith("moved")) app.get("question").focus();
    if (scenario === "moved-back-to-body") app.get("body").focus();
    if (scenario === "cancelled") app.state.plan.abort();
    finish(scenario === "keyboard-error" ? unavailable() : { status: 200, body: result });
    await pending;
    if (scenario === "keyboard-success") {
      const heading = app.get("plan-result").children[0].children[0];
      assert.equal(app.active(), heading);
      assert.equal(heading.attributes.role, "heading");
      assert.equal(heading.attributes.tabindex, "-1");
    } else {
      assert.equal(app.focused(), scenario === "keyboard-error" ? "plan-button" : scenario === "moved" ? "question" : "body", scenario);
    }
    assert.equal(app.get("plan-button").disabled, false, scenario);
  }
});
