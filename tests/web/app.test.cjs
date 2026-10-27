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
function harness(fetchResponse, { reducedMotion = false } = {}) {
  const nodes = new Map();
  const documentListeners = new Map();
  let focused = null;
  function node(id = "") {
    return {
      id, hidden: true, _disabled: false, value: "", files: [], dataset: {}, children: [], attributes: {}, listeners: {},
      get disabled() { return this._disabled; },
      set disabled(value) { this._disabled = value; if (value && focused === this) focused = document.body; },
      classList: { add() {}, remove() {}, toggle() {} },
      setAttribute(name, value) { this.attributes[name] = value; }, removeAttribute(name) { delete this.attributes[name]; },
      append(...items) { this.children.push(...items); },
      replaceChildren(...items) { this.children = items; },
      addEventListener(name, listener) { this.listeners[name] = listener; }, querySelectorAll() { return []; }, reportValidity() { return true; },
      focus() { focused = this; for (const listener of documentListeners.get("focusin") || []) listener({ target: this }); }, scrollIntoView(options) { this.lastScroll = options; },
    };
  }
  const document = {
    body: node("body"),
    getElementById(id) { if (!nodes.has(id)) nodes.set(id, node(id)); return nodes.get(id); },
    createElement() { return node(); }, createTextNode(text) { return { textContent: text }; },
    querySelectorAll(selector) { return selector.includes("[data-workflow]") ? workflow : []; },
    get activeElement() { return focused; }, hasFocus() { return true; },
    addEventListener(name, listener) { if (!documentListeners.has(name)) documentListeners.set(name, new Set()); documentListeners.get(name).add(listener); },
    removeEventListener(name, listener) { documentListeners.get(name)?.delete(listener); },
  };
  const workflow = ["upload", "card", "planning", "chat"].map((step) => {
    const button = node(`workflow-${step}`);
    button.dataset.workflow = step;
    return button;
  });
  const calls = [];
  const context = vm.createContext({
    document, window: { location: { origin: "http://localhost" }, matchMedia: () => ({ matches: reducedMotion }) }, URL, AbortController, FormData, TextEncoder, setTimeout, clearTimeout,
    fetch: async (url, options = {}) => {
      calls.push({ url, options });
      const result = await fetchResponse(url, options);
      if (options.signal?.aborted) throw new Error("Mock request aborted");
      return { ok: result.status < 400, status: result.status, json: async () => result.body };
    },
  });
  vm.runInContext(appSource.replace(/initialize\(\);\s*$/, "") + "\nglobalThis.app = { state, selectAssignment, submitUpload, deleteAssignment, confirmCard, submitPlan, updateControls, analyzeCard, showMode, scrollConversation };", context);
  const app = context.app;
  app.state.status = { mode: "offline" };
  app.state.assignments = [uploaded, seed];
  app.state.selectedId = uploaded.assignment_id;
  app.state.detail = uploaded;
  return { ...app, get: document.getElementById, calls, workflow, focused: () => focused?.id, active: () => focused };
}

function response(assignment) { return { status: 200, body: { assignment } }; }
function unavailable() { return { status: 503, body: { error: { message: "Сервис временно недоступен." } } }; }
function event() { return { preventDefault() {} }; }

test("failed analysis clears its busy status and offers a readable budget failure", async () => {
  const app = harness(() => ({ status: 503, body: { error: { code: "eval_budget_exceeded", message: "Technical budget guard exceeded." } } }));
  app.state.status = { mode: "live", live_configured: true };
  await app.analyzeCard();
  assert.equal(app.state.analysis, null);
  assert.doesNotMatch(app.get("card-status").textContent, /Разбираем|проверяем структуру/);
  assert.match(app.get("card-error").textContent, /тестовый лимит/i);
  assert.equal(app.get("analyze-button").disabled, false);
  assert.equal(app.get("analyze-cancel").hidden, true);
  assert.equal(app.get("card-form").attributes["aria-busy"], "false");
});

test("model notice uses a human name without exposing provider URIs", () => {
  const app = harness(unavailable);
  for (const model of ["gpt://private-folder/yandexgpt-5.1/latest", "gpt://private-folder/aliceai-llm-flash/latest", "unknown-private-model"]) {
    app.showMode({ mode: "live", live_configured: true, model });
    assert.doesNotMatch(app.get("mode-notice").textContent, /gpt:\/\/|private-folder|unknown-private-model|Живая модель/);
    assert.equal(app.get("mode-badge").hidden, true);
  }
  app.showMode({ mode: "live", live_configured: true, model: "gpt://private-folder/yandexgpt-5.1/latest" });
  assert.match(app.get("mode-notice").textContent, /YandexGPT 5\.1/);
});

test("manual entry before analysis confirms revision zero with user provenance and no model request", async () => {
  const confirmed = { ...uploaded, status: "ready", card, card_revision: 1 };
  const app = harness(() => response(confirmed));
  app.state.detail = { ...uploaded, card: null, card_revision: 0 };
  app.state.status = { mode: "live", live_configured: true };
  app.updateControls();
  assert.equal(app.get("manual-card-button").hidden, false);
  app.get("manual-card-button").listeners.click();
  assert.equal(app.calls.length, 0);
  assert.equal(app.get("card-form").hidden, false);
  assert.equal(app.get("card-goal").value, "");
  assert.equal(app.state.cardDraft.deadline.date, null);
  app.get("card-goal").value = "Моя цель";
  app.get("card-deliverable").value = "Мой отчёт";
  app.get("card-requirements").value = "Включить график";
  app.get("card-form").listeners.input();
  await app.confirmCard(event());
  assert.equal(app.calls.length, 1);
  assert.match(app.calls[0].url, /\/confirm$/);
  const sent = JSON.parse(app.calls[0].options.body);
  assert.equal(sent.revision, 0);
  for (const field of [sent.card.goal, sent.card.deliverable, sent.card.requirements[0]]) {
    assert.equal(field.origin, "user");
    assert.deepEqual(field.source_ids, []);
  }
  assert.deepEqual(sent.card.deadline, { date: null, origin: "unknown", source_ids: [] });
  assert.equal(app.state.detail.status, "ready");
});

test("budget failure allows explicit manual recovery without retrying analysis or changing provider mode", async () => {
  const app = harness(() => ({ status: 503, body: { error: { code: "eval_budget_exceeded", message: "guard" } } }));
  app.state.status = { mode: "live", live_configured: true };
  await app.analyzeCard();
  app.get("manual-card-button").listeners.click();
  assert.equal(app.calls.length, 1);
  assert.match(app.calls[0].url, /\/analyze$/);
  assert.equal(app.state.selectedId, uploaded.assignment_id);
  assert.equal(app.state.detail, uploaded);
  assert.equal(app.state.status.mode, "live");
  assert.equal(app.get("card-form").hidden, false);
  assert.equal(app.get("card-fields").disabled, false);
  assert.match(app.get("card-notice").textContent, /Модель не вызывается/);
});

test("manual action preserves an existing dirty draft and its evidence", () => {
  const app = harness(unavailable);
  app.state.cardDraft = card;
  app.state.cardDirty = true;
  app.get("card-goal").value = "Несохранённая цель";
  app.get("manual-card-button").listeners.click();
  assert.equal(app.state.cardDraft, card);
  assert.equal(app.state.cardDirty, true);
  assert.equal(app.get("card-goal").value, "Несохранённая цель");
  assert.equal(app.calls.length, 0);
});

test("late cancelled analysis cannot open a manual draft or show an error for another assignment", async () => {
  let finish;
  const app = harness((url) => url.endsWith("/analyze") ? new Promise((resolve) => { finish = resolve; }) : response(seed));
  app.state.status = { mode: "live", live_configured: true };
  const pending = app.analyzeCard();
  await app.selectAssignment(seed.assignment_id);
  finish({ status: 503, body: { error: { code: "eval_budget_exceeded", message: "guard" } } });
  await pending;
  assert.equal(app.state.detail, seed);
  assert.equal(app.state.analysis, null);
  assert.equal(app.state.cardDraft, null);
  assert.equal(app.get("card-status").textContent, "");
  assert.equal(app.get("card-error").hidden, true);
  assert.equal(app.get("manual-card-button").hidden, true);
});

test("workflow disables unavailable steps and navigation preserves current drafts, history and sources", () => {
  const app = harness(unavailable);
  app.state.cardDraft = card;
  app.state.cardDirty = true;
  const history = [{ role: "user", content: "Мой вопрос" }];
  app.state.history = history;
  app.get("card-goal").value = "Правка";
  app.get("question").value = "Неотправленный вопрос";
  const source = { source_id: "source-private", excerpt: "Мой материал" };
  app.get("material-results").append(source);
  app.updateControls();
  assert.deepEqual(app.workflow.map((button) => button.disabled), [false, false, true, true]);
  app.workflow[2].listeners.click();
  assert.equal(app.focused(), undefined);
  app.workflow[0].listeners.click();
  assert.equal(app.focused(), "materials-title");
  assert.equal(app.get("materials-body").hidden, false);
  app.workflow[1].listeners.click();
  assert.equal(app.focused(), "card-heading");
  assert.equal(app.workflow[1].attributes["aria-current"], "step");
  assert.equal(app.workflow[0].attributes["aria-current"], undefined);
  assert.equal(app.get("card-goal").value, "Правка");
  assert.equal(app.get("question").value, "Неотправленный вопрос");
  assert.equal(app.state.history, history);
  assert.equal(app.get("material-results").children[0], source);
  assert.equal(app.calls.length, 0);
});

test("workflow keyboard skips disabled steps and only scrolls after activation, respecting reduced motion", () => {
  for (const reducedMotion of [false, true]) {
    const app = harness(unavailable, { reducedMotion });
    app.updateControls();
    const press = (index, key) => app.workflow[index].listeners.keydown({ key, preventDefault() {} });
    press(0, "End");
    assert.equal(app.focused(), "workflow-card");
    assert.equal(app.get("card-heading").lastScroll, undefined);
    press(1, "ArrowRight");
    assert.equal(app.focused(), "workflow-upload");
    app.workflow[1].listeners.click();
    assert.equal(app.get("card-heading").lastScroll.behavior, reducedMotion ? "auto" : "smooth");
    app.state.detail = { ...uploaded, status: "ready" };
    app.updateControls();
    assert.equal(app.workflow.every((button) => !button.disabled), true);
    app.get("chat-panel").listeners.focusin();
    assert.equal(app.workflow[3].attributes["aria-current"], "step");
    press(3, "Home");
    assert.equal(app.focused(), "workflow-upload");
  }
});

test("upload can collapse without clearing selected files or an unfinished assignment name", () => {
  const app = harness(unavailable);
  const file = { name: "private.pdf", size: 10 };
  app.get("upload-files").files = [file];
  app.get("upload-name").value = "Новое задание";
  app.get("upload-body").hidden = false;
  app.get("upload-toggle").listeners.click();
  assert.equal(app.get("upload-body").hidden, true);
  assert.equal(app.get("upload-toggle").attributes["aria-expanded"], "false");
  app.get("upload-toggle").listeners.click();
  assert.equal(app.get("upload-body").hidden, false);
  app.workflow[1].listeners.click();
  assert.equal(app.get("upload-body").hidden, false);
  assert.equal(app.get("upload-name").value, "Новое задание");
  assert.equal(app.get("upload-files").files[0], file);
});

test("new chat content follows the bottom but does not move a reader reviewing earlier messages", () => {
  const app = harness(unavailable);
  const log = app.get("conversation");
  log.scrollHeight = 1000;
  log.clientHeight = 200;
  log.scrollTop = 100;
  log.listeners.scroll();
  log.scrollHeight = 1200;
  app.scrollConversation();
  assert.equal(log.scrollTop, 100);
  log.scrollTop = 1000;
  log.listeners.scroll();
  log.scrollHeight = 1300;
  app.scrollConversation();
  assert.equal(log.scrollTop, 1300);
});

test("confirmed card stays compact until explicitly opened and dirty fields cannot be collapsed", async () => {
  const saved = { ...uploaded, status: "ready", card, card_revision: 1 };
  const app = harness(() => response(saved));
  await app.selectAssignment(uploaded.assignment_id, true);
  assert.equal(app.get("card-body").hidden, true);
  assert.equal(app.get("card-summary").hidden, false);
  assert.match(app.get("card-summary").textContent, /Отчёт/);
  assert.equal(app.get("card-toggle").textContent, "Изменить карточку");
  app.get("card-toggle").listeners.click();
  assert.equal(app.get("card-body").hidden, false);
  app.workflow[3].listeners.click();
  assert.equal(app.get("card-body").hidden, true);
  app.workflow[1].listeners.click();
  assert.equal(app.get("card-body").hidden, false);
  app.get("card-goal").value = "Не потерять эту цель";
  app.get("card-form").listeners.input();
  assert.equal(app.get("card-toggle").hidden, true);
  app.get("card-toggle").listeners.click();
  assert.equal(app.get("card-body").hidden, false);
  assert.equal(app.get("card-goal").value, "Не потерять эту цель");
  app.get("card-discard").listeners.click();
  assert.equal(app.get("card-body").hidden, false);
  assert.equal(app.focused(), "card-goal");
});

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
  assert.equal(app.workflow[2].attributes["aria-current"], "step");
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
  assert.equal(app.workflow[2].attributes["aria-current"], "step");
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
