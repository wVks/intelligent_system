"use strict";

const $ = (id) => document.getElementById(id);
const state = { assignments: [], selectedId: null, detail: null, status: null, selection: 0, chat: null, plan: null, upload: null, search: null, analysis: null, confirmation: null, cardDraft: null, cardDirty: false, cardExpanded: true, planTasks: [], planVersion: 0, deleting: false, loading: false, history: [], workflow: "upload" };
let planKeyboardActivation = false;
let conversationAtBottom = true;

class ApiError extends Error {
  constructor(message, code = "request_failed", requestId = null, status = null) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.requestId = requestId;
    this.status = status;
  }
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function scrollBehavior() { return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth"; }

function focusSection(id) {
  const target = $(id);
  const step = { "upload-title": "upload", "materials-title": "upload", "card-heading": "card", "plan-title": "planning", "chat-title": "chat" }[id];
  if (step) { state.workflow = step; updateWorkflow(); }
  target.setAttribute("tabindex", "-1");
  target.focus({ preventScroll: true });
  target.scrollIntoView({ behavior: scrollBehavior(), block: "start" });
}

function setUploadExpanded(expanded) {
  $("upload-body").hidden = !expanded;
  $("upload-toggle").setAttribute("aria-expanded", String(expanded));
  $("upload-toggle").textContent = expanded ? "Свернуть" : "Добавить PDF";
}

function collapseUploadIfIdle() {
  if (!state.upload && !$("upload-files").files.length && !$("upload-name").value.trim()) setUploadExpanded(false);
}

function updateCardVisibility() {
  const collapsible = isUploaded() && state.detail?.status === "ready" && Boolean(state.cardDraft) && !state.cardDirty && !state.analysis && !state.confirmation;
  const expanded = !collapsible || state.cardExpanded;
  $("card-body").hidden = !expanded;
  $("card-toggle").hidden = !collapsible;
  $("card-toggle").setAttribute("aria-expanded", String(expanded));
  $("card-toggle").textContent = expanded ? "Свернуть карточку" : "Изменить карточку";
  $("card-summary").hidden = !collapsible || expanded;
  $("card-summary").textContent = `Результат: ${String(state.cardDraft?.deliverable?.text || "карточка подтверждена").slice(0, 180)}`;
}

function collapseCardIfReady() {
  if (state.detail?.status === "ready" && !state.cardDirty && !state.analysis && !state.confirmation) state.cardExpanded = false;
  updateCardVisibility();
}

function workflowAvailability() {
  const loaded = Boolean(state.detail) && !state.loading;
  const ready = loaded && !materialOnly() && !state.cardDirty && !state.analysis && !state.confirmation;
  return { upload: true, card: loaded && isUploaded(), planning: ready, chat: ready };
}

function updateWorkflow() {
  const available = workflowAvailability();
  if (!available[state.workflow]) state.workflow = available.card ? "card" : available.chat ? "chat" : "upload";
  document.querySelectorAll("#workflow-nav [data-workflow]").forEach((button) => {
    const step = button.dataset.workflow;
    if (step === "upload") button.setAttribute("aria-controls", isUploaded() && state.detail && !state.loading ? "materials-panel" : "upload-panel");
    button.disabled = !available[step];
    const current = state.workflow === step;
    button.classList.toggle("is-current", current);
    if (current) button.setAttribute("aria-current", "step");
    else button.removeAttribute("aria-current");
  });
}

function navigateWorkflow(step) {
  if (!workflowAvailability()[step]) return;
  if (step === "upload") {
    if (isUploaded() && state.detail && !state.loading) {
      $("materials-body").hidden = false;
      $("materials-toggle").setAttribute("aria-expanded", "true");
      $("materials-toggle").textContent = "Скрыть материалы";
      focusSection("materials-title");
    } else { setUploadExpanded(true); focusSection("upload-title"); }
  } else {
    collapseUploadIfIdle();
    if (step === "card") { state.cardExpanded = true; updateCardVisibility(); }
    else collapseCardIfReady();
    focusSection({ card: "card-heading", planning: "plan-title", chat: "chat-title" }[step]);
  }
}

function userErrorMessage(error) {
  const messages = {
    eval_budget_exceeded: "Достигнут тестовый лимит AI-запросов приложения. Владельцу сервиса нужно проверить лимит расходов AI. Материалы и план остаются доступны; карточку своих файлов можно заполнить вручную.",
    eval_budget_invalid: "AI временно недоступен: владельцу сервиса нужно проверить настройки лимита. Можно продолжить с ручной карточкой.",
    provider_auth: "Нет доступа к модели. Владельцу сервиса нужно проверить подключение. Можно продолжить с ручной карточкой.",
    provider_rate_limit: "Сервис модели временно ограничил запросы. Попробуйте позже или заполните карточку вручную.",
  };
  return messages[error.code] || error.message;
}

async function requestJSON(path, options = {}, timeout = 50000) {
  const controller = new AbortController();
  const upstream = options.signal;
  let timedOut = false;
  const cancel = () => controller.abort();
  if (upstream?.aborted) throw new ApiError("Запрос отменён.", "cancelled");
  upstream?.addEventListener("abort", cancel, { once: true });
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeout);
  try {
    const jsonBody = options.body && !(options.body instanceof FormData);
    const response = await fetch(path, { ...options, signal: controller.signal, headers: { "Accept": "application/json", ...(jsonBody ? { "Content-Type": "application/json" } : {}), ...options.headers } });
    if (response.status === 204 && response.ok) return {};
    let data;
    try { data = await response.json(); } catch (_) { throw new ApiError("Сервис вернул ответ в неподдерживаемом формате.", "invalid_response"); }
    if (!response.ok) throw new ApiError(data?.error?.message || `Запрос не выполнен (HTTP ${response.status}).`, data?.error?.code || "request_failed", data?.request_id || null, response.status);
    if (!data || typeof data !== "object") throw new ApiError("Получен некорректный ответ сервиса.", "invalid_response");
    return data;
  } catch (error) {
    if (controller.signal.aborted) throw new ApiError(timedOut ? "Время ожидания истекло. Попробуйте ещё раз." : "Запрос отменён.", timedOut ? "request_timeout" : "cancelled");
    if (error instanceof ApiError) throw error;
    throw new ApiError("Не удалось связаться с сервисом. Проверьте подключение и повторите запрос.", "connection_error");
  } finally {
    clearTimeout(timer);
    upstream?.removeEventListener("abort", cancel);
  }
}

function sourceURL(value) {
  if (typeof value !== "string" || !value) return null;
  try {
    const url = new URL(value, window.location.origin);
    return url.origin === window.location.origin && ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch (_) { return null; }
}

function sourceLocation(source) {
  if (Number.isInteger(source.page) && source.page > 0) return `Стр. ${source.page}`;
  return typeof source.section === "string" && source.section ? source.section : "Материал";
}

function dateLabel(assignment) {
  const value = assignment.due_date || assignment.due_at;
  if (!value) return "Дата сдачи не указана";
  const date = new Date(assignment.due_date ? `${assignment.due_date}T12:00:00` : value);
  return Number.isNaN(date.getTime()) ? "Дата требует уточнения" : `Сдать до ${date.toLocaleDateString("ru-RU", { day: "numeric", month: "long", year: "numeric" })}`;
}

function formatMinutes(value) {
  const minutes = Number.isFinite(value) ? Math.max(0, Math.round(value)) : 0;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return hours ? `${hours} ч${rest ? ` ${rest} мин` : ""}` : `${rest} мин`;
}

function showMode(status, assignment = state.detail) {
  state.status = status;
  const offline = status.mode === "offline";
  const unconfigured = status.mode === "live" && !status.live_configured;
  $("mode-badge").className = `mode-badge${offline ? " is-offline" : unconfigured ? " is-error" : ""}`;
  $("mode-badge").hidden = true;
  $("mode-label").textContent = offline ? "Демо · без LLM" : unconfigured ? "Модель не настроена" : "AI подключён";
  $("mode-notice").className = `mode-notice${offline ? "" : unconfigured ? " is-error" : " is-live"}`;
  $("mode-notice").textContent = offline
    ? isUploaded(assignment)
      ? "Модель не подключена. Карточку можно заполнить вручную, план рассчитывает код. В чате доступны демонстрационные ответы и выдержки из материалов."
      : "Демонстрационный режим: ответы по материалам формируются детерминированно. LLM не подключена."
    : unconfigured
      ? "Выбран режим живой модели, но доступ к ней не настроен. Запросы не будут подменены демонстрационными ответами."
      : `${String(status.model || "").includes("/yandexgpt-5.1/") ? "Ассистент работает на YandexGPT 5.1." : "AI-ассистент подключён."} Проверяйте важные факты по источникам.`;
}

function materialOnly(assignment = state.detail) {
  return Boolean(assignment?.status && assignment.status !== "ready");
}

function isUploaded(assignment = state.detail) {
  return Boolean(assignment?.is_uploaded || assignment?.assignment_id?.startsWith("upload-"));
}

function updateControls() {
  const focused = document.activeElement;
  const cardBusy = Boolean(state.analysis || state.confirmation);
  const ready = Boolean(state.detail) && !state.loading && !materialOnly() && !state.cardDirty && !cardBusy;
  $("question").disabled = !ready;
  $("send-button").disabled = !ready || Boolean(state.chat);
  $("send-button").hidden = Boolean(state.chat);
  $("cancel-button").hidden = !state.chat;
  document.querySelectorAll("[data-prompt]").forEach((button) => { button.disabled = !ready || Boolean(state.chat); });
  $("plan-button").disabled = !ready || Boolean(state.plan);
  $("plan-button").textContent = state.plan ? "Составляем план…" : "Составить план →";
  $("chat-form").setAttribute("aria-busy", String(Boolean(state.chat)));
  $("plan-form").setAttribute("aria-busy", String(Boolean(state.plan)));
  const uploading = Boolean(state.upload);
  $("upload-button").disabled = uploading || !$("upload-files").files.length;
  $("upload-button").textContent = uploading ? "Загружаем и извлекаем текст…" : "Загрузить материалы ↑";
  $("upload-cancel").hidden = !uploading;
  $("upload-name").disabled = uploading;
  $("upload-files").disabled = uploading;
  $("upload-reset").hidden = !$("upload-files").files.length || uploading;
  $("upload-form").setAttribute("aria-busy", String(uploading));
  $("material-search-button").disabled = state.loading || Boolean(state.search);
  $("material-search-button").textContent = state.search ? "Ищем…" : "Найти";
  $("material-search-form").setAttribute("aria-busy", String(Boolean(state.search)));
  $("delete-assignment").disabled = state.deleting;
  $("delete-confirm").disabled = state.deleting;
  $("delete-cancel").disabled = state.deleting;
  $("delete-confirm").textContent = state.deleting ? "Удаляем…" : "Да, удалить";
  $("card-fields").disabled = cardBusy || state.loading;
  $("analyze-button").disabled = !isUploaded() || state.loading || cardBusy || state.cardDirty;
  $("analyze-button").textContent = state.analysis ? "Получаем карточку…" : state.status?.mode === "live" ? state.cardDraft ? "Повторить AI-разбор" : "Разобрать материалы" : "Заполнить карточку";
  $("analyze-button").hidden = state.status?.mode !== "live" && Boolean(state.cardDraft);
  $("analyze-cancel").hidden = !state.analysis;
  $("manual-card-button").hidden = !isUploaded() || Boolean(state.cardDraft);
  $("manual-card-button").disabled = !state.detail || state.loading || cardBusy;
  $("card-confirm").disabled = !state.cardDraft || state.loading || cardBusy || (state.detail?.status === "ready" && !state.cardDirty);
  $("card-confirm").textContent = state.confirmation ? "Сохраняем карточку…" : state.detail?.status === "ready" ? state.cardDirty ? "Подтвердить изменения и пересчитать план" : "Карточка подтверждена" : "Подтвердить карточку и перейти к плану";
  $("card-form").setAttribute("aria-busy", String(cardBusy));
  $("card-dirty").hidden = !state.cardDirty;
  $("card-discard").hidden = !state.cardDirty;
  $("card-discard").disabled = cardBusy;
  if (state.cardDirty) $("card-evidence").hidden = true;
  $("card-state").textContent = state.cardDirty ? "Есть изменения" : state.detail?.status === "ready" ? "Подтверждена" : state.cardDraft ? "Проверьте сведения" : "Нужна карточка";
  for (const [cancelId, nextId] of [["cancel-button", "question"], ["upload-cancel", "upload-button"], ["analyze-cancel", "analyze-button"]]) {
    if (focused === $(cancelId) && $(cancelId).hidden && !$(nextId).hidden && !$(nextId).disabled) $(nextId).focus();
  }
  updateCardVisibility();
  updateWorkflow();
}

function renderAssignments() {
  const list = $("assignments");
  list.replaceChildren();
  $("assignment-count").textContent = String(state.assignments.length);
  if (!state.assignments.length) list.append(element("li", "sidebar-placeholder", "Задания пока не добавлены."));
  state.assignments.forEach((assignment) => {
    const item = element("li");
    const button = element("button", "assignment-button");
    button.type = "button";
    button.dataset.assignmentId = assignment.assignment_id;
    button.setAttribute("aria-pressed", "false");
    const subtitle = isUploaded(assignment) ? assignment.status === "ready" ? "Карточка подтверждена" : assignment.status === "needs_confirmation" ? "Карточка на проверке" : "Свои материалы · текст готов" : `${assignment.demo ? "Демо · " : ""}${dateLabel(assignment)}`;
    button.append(element("span", "assignment-name", assignment.title), element("span", "assignment-subtitle", subtitle));
    const selected = assignment.assignment_id === state.selectedId;
    button.classList.toggle("is-selected", selected);
    button.setAttribute("aria-pressed", String(selected));
    button.addEventListener("click", () => { selectAssignment(assignment.assignment_id); });
    item.append(button);
    list.append(item);
  });
}

function sourceCard(source) {
  const card = element("article", "source-card");
  const heading = element("div", "source-heading");
  heading.append(element("h3", "source-title", source.title || "Учебный материал"), element("span", "source-page", sourceLocation(source)));
  card.append(heading);
  if (source.excerpt) card.append(element("p", "source-excerpt", source.excerpt));
  const links = element("div", "source-links");
  for (const [value, label] of [[source.url, "Открыть фрагмент ↗"], [source.document_url ? `${source.document_url}#page=${source.page || 1}` : null, "Страница PDF ↗"]]) {
    const url = sourceURL(value);
    if (!url) continue;
    const link = element("a", "source-open", label);
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    links.append(link);
  }
  card.append(links);
  return card;
}

function renderSources(sources, fromAnswer = false) {
  const entries = Array.isArray(sources) ? sources : [];
  if (fromAnswer && isUploaded()) $("sources-panel").hidden = !entries.length;
  $("sources-title").textContent = fromAnswer ? "Источники ответа" : "Материалы задания";
  $("source-count").textContent = String(entries.length);
  const list = $("sources");
  list.replaceChildren();
  if (!entries.length) list.append(element("p", "empty-note", fromAnswer ? "В этом ответе нет подтверждающих фрагментов. Уточните вопрос или посмотрите материалы задания." : "Источники для этого задания пока отсутствуют."));
  entries.forEach((source) => list.append(sourceCard(source)));
}

function renderRequirements(assignment) {
  const target = $("requirements");
  target.replaceChildren();
  const requirements = Array.isArray(assignment.requirements) ? assignment.requirements : [];
  const criteria = Array.isArray(assignment.criteria) ? assignment.criteria : [];
  if (requirements.length) {
    target.append(element("h3", "", "Что подготовить"));
    const list = element("ul");
    requirements.forEach((item) => list.append(element("li", "", typeof item === "string" ? item : item.text)));
    target.append(list);
  }
  if (criteria.length) {
    target.append(element("h3", "", "Как оценивается работа"));
    const list = element("ul");
    criteria.forEach((item) => list.append(element("li", "", `${item.title || item.text || "Критерий"}${Number.isFinite(item.max_points) ? ` — до ${item.max_points} баллов` : ""}`)));
    target.append(list);
  }
  $("assignment-details").hidden = !requirements.length && !criteria.length;
}

function renderMaterialResults(sources, query = "") {
  const entries = Array.isArray(sources) ? sources : [];
  const target = $("material-results");
  target.replaceChildren();
  $("material-search-status").textContent = query
    ? entries.length ? `Фрагментов по запросу: ${entries.length}.` : "Совпадений нет. Попробуйте другое слово из задания."
    : `Текст по страницам · фрагментов: ${entries.length}.`;
  entries.forEach((source) => target.append(sourceCard(source)));
}

function renderDocuments(assignment) {
  const target = $("documents");
  target.replaceChildren();
  const documents = Array.isArray(assignment.documents) ? assignment.documents : [];
  documents.forEach((document) => {
    const card = element("article", "document-card");
    const name = document.filename || document.title || document.name || "Материал PDF";
    const pages = document.page_count ?? document.pages;
    const info = element("div", "document-info");
    info.append(element("strong", "document-name", name));
    const metadata = [];
    if (Number.isInteger(pages)) metadata.push(`Страниц: ${pages}`);
    if (Number.isInteger(document.character_count)) metadata.push(`Символов: ${document.character_count.toLocaleString("ru-RU")}`);
    info.append(element("span", "document-meta", metadata.join(" · ") || "Текст сохранён"));
    card.append(element("span", "document-icon", "PDF"), info);
    const url = sourceURL(document.document_url || document.url);
    if (url) {
      const link = element("a", "document-open", "Открыть PDF ↗");
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.setAttribute("aria-label", `Открыть PDF: ${name}`);
      card.append(link);
    }
    target.append(card);
  });
  renderMaterialResults(assignment.sources);
}

function cloneData(value) { return JSON.parse(JSON.stringify(value)); }

function invalidatePlan(showMessage = false) {
  const hadPlan = !$("plan-result").hidden || Boolean(state.plan);
  state.plan?.abort();
  state.plan = null;
  state.planVersion += 1;
  $("plan-result").hidden = true;
  $("plan-error").hidden = true;
  $("plan-stale").hidden = !(showMessage || hadPlan);
}

function resetConversation() {
  state.chat?.abort();
  state.chat = null;
  state.history = [];
  conversationAtBottom = true;
  $("conversation").querySelectorAll(".message").forEach((node) => node.remove());
  $("chat-welcome").hidden = false;
}

function cardField(value) {
  return value && typeof value === "object" ? value : { text: "", origin: "user", source_ids: [] };
}

function lineText(value) { return String(value || "").replace(/\s*\n\s*/g, " ").trim(); }

function renderCardEvidence(card) {
  const target = $("card-evidence");
  target.replaceChildren();
  const sources = new Map((state.detail?.sources || []).map((source) => [source.source_id, source]));
  const fields = [["Цель", card.goal], ["Результат сдачи", card.deliverable],
    ...(card.requirements || []).map((item, index) => [`Требование ${index + 1}`, item]),
    ...(card.criteria || []).map((item, index) => [`Критерий ${index + 1}`, item]),
    ["Срок", card.deadline]];
  let count = 0;
  fields.forEach(([name, item]) => {
    if (!item || !(item.text || item.date)) return;
    const row = element("div", "card-evidence-row");
    row.append(element("span", "", `${name} · ${item.origin === "document" ? "по документу" : "ваш ввод"}`));
    (item.origin === "document" ? item.source_ids || [] : []).forEach((id) => {
      const source = sources.get(id);
      const url = sourceURL(source?.url);
      if (!url) return;
      const link = element("a", "source-chip", `${source.title || "Источник"} · ${sourceLocation(source)} ↗`);
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      row.append(link);
    });
    target.append(row);
    count += 1;
  });
  target.hidden = !count;
}

function renderCard(assignment, expanded = assignment.status !== "ready") {
  state.cardDraft = assignment.card ? cloneData(assignment.card) : null;
  state.cardDirty = false;
  state.cardExpanded = expanded;
  $("card-form").hidden = !state.cardDraft;
  $("card-error").hidden = true;
  $("card-reload").hidden = true;
  $("card-status").textContent = "";
  $("card-dirty").hidden = true;
  const offline = state.status?.mode !== "live";
  $("card-notice").textContent = offline
    ? "Модель не подключена. Заполните карточку по материалам вручную. Ваш ввод будет отмечен отдельно от документа."
    : "Проверьте предложенное описание по источникам. Исправления сохранятся как ваш ввод, неизвестную дату можно оставить пустой.";
  if (!state.cardDraft) {
    $("card-evidence").hidden = true;
    $("card-questions").hidden = true;
    state.planTasks = [];
    renderTaskEditor();
    return;
  }
  const card = state.cardDraft;
  $("card-title").value = card.title || assignment.title || "";
  $("card-goal").value = cardField(card.goal).text || "";
  $("card-deliverable").value = cardField(card.deliverable).text || "";
  $("card-requirements").value = (card.requirements || []).map((item) => lineText(item.text)).join("\n");
  $("card-criteria").value = (card.criteria || []).map((item) => lineText(item.text)).join("\n");
  $("card-deadline").value = card.deadline?.date || "";
  const questions = $("card-questions");
  questions.replaceChildren();
  const pending = Array.isArray(card.questions) ? card.questions : [];
  questions.hidden = !pending.length;
  if (pending.length) {
    questions.append(element("strong", "", "Что ещё стоит уточнить"));
    const list = element("ul");
    pending.forEach((text) => list.append(element("li", "", text)));
    questions.append(list);
  }
  renderCardEvidence(card);
  state.planTasks = (card.tasks || []).map((task) => ({ ...cloneData(task), completed: false }));
  renderTaskEditor();
}

function editedField(text, original) {
  const old = cardField(original);
  return text === old.text ? cloneData(old) : { text, origin: "user", source_ids: [] };
}

function editedLines(value, originals, maximum, name) {
  const lines = value.split("\n").map((text) => text.trim()).filter(Boolean);
  if (lines.length > maximum) throw new ApiError(`${name}: оставьте не больше ${maximum} строк.`);
  if (lines.some((text) => text.length > 1000)) throw new ApiError(`${name}: одна строка должна быть не длиннее 1000 символов.`);
  return lines.map((text) => {
    const old = (originals || []).find((item) => lineText(item.text) === text);
    return old ? cloneData(old) : { text, origin: "user", source_ids: [] };
  });
}

function readCardForm() {
  const original = state.cardDraft;
  if (!original) throw new ApiError("Сначала откройте карточку задания.");
  const deadline = $("card-deadline").value || null;
  return {
    title: $("card-title").value.trim(),
    goal: editedField($("card-goal").value.trim(), original.goal),
    deliverable: editedField($("card-deliverable").value.trim(), original.deliverable),
    requirements: editedLines($("card-requirements").value, original.requirements, 12, "Требования"),
    criteria: editedLines($("card-criteria").value, original.criteria, 10, "Критерии"),
    deadline: deadline === (original.deadline?.date || null) ? cloneData(original.deadline || { date: null, origin: "unknown", source_ids: [] }) : { date: deadline, origin: deadline ? "user" : "unknown", source_ids: [] },
    tasks: cloneData(original.tasks || []),
    questions: cloneData(original.questions || []),
  };
}

function applyAssignment(assignment) {
  state.detail = assignment;
  state.assignments = state.assignments.map((item) => item.assignment_id === assignment.assignment_id ? assignment : item);
  $("assignment-title").textContent = assignment.title;
  $("assignment-description").textContent = isUploaded(assignment)
    ? assignment.status === "ready"
      ? "Карточка подтверждена. Рассчитайте план или задайте вопрос по материалам."
      : "Материалы сохранены. Проверьте и подтвердите карточку задания."
    : assignment.description || "Требования, материалы и план подготовки по этому заданию.";
  $("assignment-deadline").textContent = dateLabel(assignment);
  renderAssignments();
  renderSources(assignment.sources);
  renderRequirements(assignment);
  showAssignmentMode(assignment);
  renderCard(assignment);
  updateControls();
}

function cardError(error) {
  state.cardExpanded = true;
  $("card-status").textContent = "";
  $("card-error").textContent = userErrorMessage(error);
  $("card-error").hidden = false;
  if (error.status === 409) {
    $("card-reload").hidden = false;
    $("card-status").textContent = "Карточка изменилась. Обновите её перед сохранением; несохранённые правки будут заменены.";
  }
}

function openManualCard() {
  if (!isUploaded() || !state.detail || state.loading || state.analysis || state.confirmation) return;
  if (!state.cardDraft) {
    const field = () => ({ text: "", origin: "user", source_ids: [] });
    const card = { title: state.detail.title, goal: field(), deliverable: field(), requirements: [], criteria: [], deadline: { date: null, origin: "unknown", source_ids: [] }, tasks: [], questions: [] };
    renderCard({ ...state.detail, card });
    $("card-notice").textContent = "Заполните карточку по своим материалам. Модель не вызывается; введённые сведения сохранятся как ваш ввод. Неизвестную дату оставьте пустой.";
  }
  $("card-status").textContent = "Ручное заполнение. После подтверждения станут доступны план и чат.";
  updateControls();
  collapseUploadIfIdle();
  focusSection("card-heading");
  $("card-goal").focus({ preventScroll: true });
}

async function analyzeCard() {
  if (!isUploaded() || state.loading || state.analysis || state.confirmation || state.cardDirty) return;
  if (state.status?.mode !== "live") { openManualCard(); return; }
  const controller = new AbortController();
  const selection = ++state.selection;
  resetConversation();
  invalidatePlan();
  state.analysis = controller;
  state.cardExpanded = true;
  $("card-error").hidden = true;
  $("card-status").textContent = state.status?.mode === "live" ? "Разбираем материалы и проверяем структуру карточки…" : "Готовим пустую карточку для ручного заполнения. Модель не вызывается.";
  updateControls();
  try {
    const data = await requestJSON(`/api/assignments/${encodeURIComponent(state.selectedId)}/analyze`, { method: "POST", body: "{}", signal: controller.signal }, 50000);
    if (selection !== state.selection) return;
    if (!data.assignment?.card) throw new ApiError("Сервис не вернул карточку задания.", "invalid_response");
    applyAssignment(data.assignment);
    if (data.notice) $("card-notice").textContent = data.notice;
    $("card-status").textContent = data.mode === "live" ? "Черновик готов. Проверьте сведения и подтвердите карточку." : "Карточка открыта. Заполните цель и результат сдачи по своим материалам.";
    state.analysis = null;
    updateControls();
    $("card-goal").focus();
  } catch (error) {
    if (selection !== state.selection) return;
    if (error.code === "cancelled") $("card-status").textContent = "Разбор отменён. Сохранённые материалы доступны ниже.";
    else cardError(error);
  } finally {
    if (state.analysis === controller) { state.analysis = null; updateControls(); }
  }
}

async function confirmCard(event) {
  event.preventDefault();
  if (!isUploaded() || !state.cardDraft || state.loading || state.analysis || state.confirmation) return;
  if (!$("card-form").reportValidity()) return;
  let card;
  try { card = readCardForm(); } catch (error) { cardError(error); return; }
  const controller = new AbortController();
  const selection = ++state.selection;
  resetConversation();
  invalidatePlan();
  state.confirmation = controller;
  $("card-error").hidden = true;
  $("card-status").textContent = "Проверяем и сохраняем карточку…";
  updateControls();
  try {
    const data = await requestJSON(`/api/assignments/${encodeURIComponent(state.selectedId)}/confirm`, { method: "POST", body: JSON.stringify({ card, revision: state.detail.card_revision ?? 0 }), signal: controller.signal }, 15000);
    if (selection !== state.selection) return;
    if (!data.assignment?.card || data.assignment.status !== "ready") throw new ApiError("Не удалось подтвердить состояние карточки.", "invalid_response");
    applyAssignment(data.assignment);
    $("card-status").textContent = "Карточка подтверждена. Проверьте оценки задач и рассчитайте план ниже.";
    state.confirmation = null;
    updateControls();
    focusSection("plan-title");
  } catch (error) {
    if (selection === state.selection) {
      if (error.code === "cancelled") $("card-status").textContent = "Сохранение отменено. Правки остаются в форме.";
      else cardError(error);
    }
  } finally {
    if (state.confirmation === controller) { state.confirmation = null; updateControls(); }
  }
}

function renderTaskEditor() {
  const target = $("plan-tasks");
  target.replaceChildren();
  $("plan-tasks-editor").hidden = !isUploaded() || !state.planTasks.length;
  state.planTasks.forEach((task, index) => {
    const row = element("div", `plan-task${task.completed ? " is-complete" : ""}`);
    const done = element("input");
    done.type = "checkbox";
    done.checked = task.completed;
    done.setAttribute("aria-label", `Задача ${index + 1} выполнена`);
    const title = element("input", "task-title-input");
    title.type = "text";
    title.maxLength = 200;
    title.required = true;
    title.value = task.title;
    title.setAttribute("aria-label", `Название задачи ${index + 1}`);
    const minutes = element("input", "task-minutes-input");
    minutes.type = "number";
    minutes.min = "1";
    minutes.max = "4800";
    minutes.step = "1";
    minutes.required = true;
    minutes.value = task.minutes;
    minutes.setAttribute("aria-label", `Минут на задачу ${index + 1}`);
    const minuteLabel = element("label", "task-minutes-label", "Минуты");
    minuteLabel.append(minutes);
    const info = element("div", "task-edit-info");
    const origin = element("span", "task-origin", task.estimate_source === "document" ? "Оценка из документа" : task.estimate_source === "user" ? "Ваша оценка" : "Предварительная оценка");
    info.append(title, origin);
    const edit = () => {
      task.title = title.value;
      task.minutes = Number(minutes.value);
      task.estimate_source = "user";
      task.source_ids = [];
      origin.textContent = "Ваша оценка";
      invalidatePlan(true);
      updateControls();
    };
    title.addEventListener("input", edit);
    minutes.addEventListener("input", edit);
    done.addEventListener("change", () => {
      task.completed = done.checked;
      row.classList.toggle("is-complete", task.completed);
      invalidatePlan(true);
      updateControls();
    });
    row.append(done, info, minuteLabel);
    target.append(row);
  });
}

function showAssignmentMode(assignment) {
  const uploaded = isUploaded(assignment);
  const pending = materialOnly(assignment);
  state.workflow = uploaded ? pending ? "card" : "planning" : "chat";
  if (uploaded) collapseUploadIfIdle();
  $("materials-panel").hidden = !uploaded;
  $("card-panel").hidden = !uploaded;
  $("chat-panel").hidden = pending;
  $("context-column").hidden = pending;
  $("sources-panel").hidden = uploaded;
  $("workspace-grid").classList.toggle("is-materials", pending);
  $("workspace-grid").classList.toggle("is-uploaded", uploaded);
  $("delete-confirmation").hidden = true;
  $("delete-error").hidden = true;
  $("material-query").value = "";
  $("materials-toggle").hidden = !uploaded || pending;
  $("materials-body").hidden = uploaded && !pending;
  $("materials-toggle").setAttribute("aria-expanded", String(pending));
  $("materials-toggle").textContent = pending ? "Скрыть материалы" : "Показать материалы";
  if (uploaded) {
    $("assignment-deadline").textContent = pending ? "Сначала подтвердите карточку" : dateLabel(assignment);
    $("materials-next").textContent = pending ? "Откройте оригиналы, проверьте фрагменты и заполните карточку выше. После подтверждения станут доступны план и чат." : "Карточка подтверждена. Здесь остаются исходники и поиск для проверки требований.";
    renderDocuments(assignment);
  }
  $("plan-intro").textContent = uploaded ? "Задачи идут по порядку. Проверьте оценки минут и отметьте выполненное перед расчётом." : "Распределим этапы по доступному времени. Длительности — ориентировочные оценки.";
  const prompts = uploaded ? [["Что требуется сделать по этому заданию?", "Что требуется?"], ["Какие критерии оценки указаны?", "Критерии оценки"], ["Когда сдавать задание?", "Срок сдачи"]] : [["Что нужно отправить в LMS?", "Что отправить в LMS?"], ["За что дают баллы и как оценивается работа?", "Критерии оценки"], ["Когда сдавать личный проект?", "Когда сдавать?"]];
  document.querySelectorAll("[data-prompt]").forEach((button, index) => {
    button.dataset.prompt = prompts[index][0];
    button.replaceChildren(document.createTextNode(prompts[index][1] + " "), element("span", "", "↗"));
  });
  $("question").placeholder = uploaded ? "Например: объясни первое требование подробнее" : "Например: что обязательно должно быть в README?";
  if (state.status) showMode(state.status, assignment);
}

function selectedFilesChanged() {
  $("upload-error").hidden = true;
  const files = Array.from($("upload-files").files);
  const target = $("selected-files");
  target.replaceChildren();
  target.hidden = !files.length;
  files.forEach((file) => target.append(element("li", "", `${file.name} · ${(file.size / 1024 / 1024).toLocaleString("ru-RU", { maximumFractionDigits: 2 })} МиБ`)));
  $("upload-status").textContent = files.length ? `Выбрано файлов: ${files.length}. Загрузите их как одно задание.` : "Текст будет сохранён со ссылками на страницы.";
  updateControls();
}

async function refreshAssignments(preferredId = state.selectedId) {
  const data = await requestJSON("/api/assignments", {}, 15000);
  if (!Array.isArray(data.assignments)) throw new ApiError("Не удалось прочитать список заданий.", "invalid_response");
  state.assignments = data.assignments;
  renderAssignments();
  const next = state.assignments.find((item) => item.assignment_id === preferredId) || state.assignments[0];
  if (next) await selectAssignment(next.assignment_id);
}

async function submitUpload(event) {
  event.preventDefault();
  if (state.upload) return;
  $("upload-error").hidden = true;
  const files = Array.from($("upload-files").files);
  let problem = "";
  if (files.length < 1 || files.length > 3) problem = "Выберите от одного до трёх PDF для одного задания.";
  else if (files.some((file) => !/\.pdf$/i.test(file.name))) problem = "Пока принимаются только PDF. Выберите документ с выделяемым текстом.";
  else if (files.some((file) => !file.size)) problem = "Один из файлов пустой. Выберите другой PDF.";
  else if (files.reduce((sum, file) => sum + file.size, 0) > 10 * 1024 * 1024) problem = "Суммарный размер PDF превышает 10 МиБ. Уменьшите размер или число файлов.";
  if (problem) {
    $("upload-error").textContent = problem;
    $("upload-error").hidden = false;
    return;
  }
  const body = new FormData();
  body.append("title", $("upload-name").value.trim());
  files.forEach((file) => body.append("files", file));
  const controller = new AbortController();
  state.upload = controller;
  $("upload-status").textContent = "Передаём PDF и извлекаем текст по страницам. Можно отменить загрузку.";
  updateControls();
  try {
    const data = await requestJSON("/api/assignments", { method: "POST", body, signal: controller.signal }, 65000);
    if (!data.assignment?.assignment_id) throw new ApiError("Материалы отправлены, но сервис не вернул задание. Обновите страницу перед повторной загрузкой.", "invalid_response");
    $("upload-name").value = "";
    $("upload-files").value = "";
    $("selected-files").replaceChildren();
    $("selected-files").hidden = true;
    state.assignments = [data.assignment, ...state.assignments.filter((item) => item.assignment_id !== data.assignment.assignment_id)];
    renderAssignments();
    const opened = await selectAssignment(data.assignment.assignment_id);
    $("upload-status").textContent = opened
      ? "Готово: материалы сохранены. Следующий шаг — проверить и подтвердить карточку задания."
      : "Материалы сохранены. Выберите новое задание в списке после завершения текущих правок или повторите его открытие.";
    if (opened) { setUploadExpanded(false); focusSection("card-heading"); }
  } catch (error) {
    if (error.code === "cancelled") {
      $("upload-status").textContent = "Загрузка отменена. Если задание успело сохраниться, оно появится в списке.";
      try { await refreshAssignments(); } catch (_) { /* Preserve the cancellation message. */ }
    } else {
      $("upload-status").textContent = "Материалы не загружены. Исправьте причину и повторите попытку.";
      $("upload-error").textContent = error.message;
      $("upload-error").hidden = false;
    }
  } finally {
    if (state.upload === controller) state.upload = null;
    updateControls();
  }
}

async function searchMaterials(event) {
  event.preventDefault();
  if (!state.detail || !isUploaded() || state.search) return;
  const query = $("material-query").value.trim();
  if (!query) return renderMaterialResults(state.detail.sources);
  const controller = new AbortController();
  const selection = state.selection;
  state.search = controller;
  $("material-search-status").textContent = "Ищем фрагменты в материалах этого задания…";
  updateControls();
  try {
    const data = await requestJSON(`/api/assignments/${encodeURIComponent(state.selectedId)}/materials?q=${encodeURIComponent(query)}`, { signal: controller.signal }, 15000);
    if (selection === state.selection) renderMaterialResults(data.matches, query);
  } catch (error) {
    if (selection === state.selection && error.code !== "cancelled") $("material-search-status").textContent = error.message;
  } finally {
    if (state.search === controller) { state.search = null; updateControls(); }
  }
}

async function deleteAssignment() {
  if (!state.selectedId || !isUploaded() || state.deleting) return;
  const id = state.selectedId;
  state.deleting = true;
  $("delete-error").hidden = true;
  updateControls();
  try {
    await requestJSON(`/api/assignments/${encodeURIComponent(id)}`, { method: "DELETE" }, 15000);
    state.assignments = state.assignments.filter((assignment) => assignment.assignment_id !== id);
    renderAssignments();
    if (state.selectedId === id) {
      const next = state.assignments[0];
      if (next) await selectAssignment(next.assignment_id, true);
    }
    $("upload-status").textContent = "Задание и его материалы удалены.";
  } catch (error) {
    if (state.selectedId === id) {
      $("delete-error").textContent = error.message;
      $("delete-error").hidden = false;
    }
  } finally { state.deleting = false; updateControls(); }
}

async function selectAssignment(id, force = false) {
  if (state.selectedId === id && state.detail && !force) return true;
  if (state.cardDirty && state.selectedId && !force) {
    $("card-status").textContent = "Сначала подтвердите изменения карточки или нажмите «Отменить правки», затем выберите другое задание.";
    $("card-confirm").focus();
    return false;
  }
  state.chat?.abort();
  state.plan?.abort();
  state.search?.abort();
  state.analysis?.abort();
  state.confirmation?.abort();
  state.chat = null;
  state.plan = null;
  state.search = null;
  state.analysis = null;
  state.confirmation = null;
  state.cardDraft = null;
  state.cardDirty = false;
  state.planTasks = [];
  state.planVersion += 1;
  const selection = ++state.selection;
  state.selectedId = id;
  state.history = [];
  conversationAtBottom = true;
  state.detail = null;
  state.loading = true;
  $("conversation").querySelectorAll(".message").forEach((node) => node.remove());
  $("chat-welcome").hidden = false;
  $("plan-result").hidden = true;
  $("plan-error").hidden = true;
  $("plan-stale").hidden = true;
  $("card-form").hidden = true;
  $("card-error").hidden = true;
  $("card-reload").hidden = true;
  $("card-evidence").hidden = true;
  $("card-questions").hidden = true;
  $("card-status").textContent = "";
  $("question").value = "";
  document.querySelectorAll("[data-assignment-id]").forEach((button) => {
    const selected = button.dataset.assignmentId === id;
    button.classList.toggle("is-selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  const brief = state.assignments.find((assignment) => assignment.assignment_id === id);
  if (brief) showAssignmentMode(brief);
  $("assignment-title").textContent = brief?.title || "Задание";
  $("assignment-description").textContent = "Загружаем описание и материалы задания…";
  $("assignment-deadline").textContent = brief ? dateLabel(brief) : "";
  $("sources").replaceChildren(element("p", "empty-note", "Загружаем материалы задания…"));
  $("source-count").textContent = "—";
  $("assignment-details").hidden = true;
  $("delete-confirmation").hidden = true;
  if (isUploaded(brief)) {
    $("documents").replaceChildren(element("p", "empty-note", "Загружаем файлы задания…"));
    $("material-results").replaceChildren();
    $("material-search-status").textContent = "Загружаем сохранённый текст…";
  }
  updateControls();
  try {
    const data = await requestJSON(`/api/assignments/${encodeURIComponent(id)}`, {}, 15000);
    if (selection !== state.selection) return false;
    if (!data.assignment || typeof data.assignment.title !== "string") throw new ApiError("Не удалось прочитать задание.", "invalid_response");
    state.loading = false;
    applyAssignment(data.assignment);
    return true;
  } catch (error) {
    if (selection !== state.selection) return;
    const target = isUploaded(brief) ? $("material-results") : $("sources");
    target.replaceChildren(element("p", "inline-error", error.message));
    const retry = element("button", "retry-button", "Загрузить задание ещё раз");
    retry.type = "button";
    retry.addEventListener("click", () => { selectAssignment(id); });
    target.append(retry);
    if (isUploaded(brief)) {
      $("materials-body").hidden = false;
      $("materials-toggle").setAttribute("aria-expanded", "true");
      $("materials-toggle").textContent = "Скрыть материалы";
      $("documents").replaceChildren();
      $("material-search-status").textContent = "Не удалось получить материалы задания.";
    }
    return false;
  } finally {
    if (selection === state.selection) { state.loading = false; updateControls(); }
  }
}

function messageNode(role, text = "", pending = false) {
  $("chat-welcome").hidden = true;
  const node = element("article", `message message-${role}${pending ? " is-pending" : ""}`);
  const header = element("div", "message-header");
  header.append(element("span", "message-author", role === "user" ? "Вы" : "StudyNavigator"));
  node.append(header, element("p", "message-content", text));
  $("conversation").append(node);
  scrollConversation();
  return node;
}

function scrollConversation() {
  if (conversationAtBottom) $("conversation").scrollTop = $("conversation").scrollHeight;
}

function fillAnswer(node, data) {
  node.classList.remove("is-pending");
  const header = node.querySelector(".message-header");
  const live = data.mode === "live";
  header.append(element("span", `message-mode${live ? " is-live" : ""}`, live ? "LLM" : "Демо · без LLM"));
  const statuses = { needs_clarification: "Нужно уточнение", insufficient_data: "Недостаточно данных", out_of_scope: "Вне материалов", conflict: "Есть противоречие" };
  if (statuses[data.status]) header.append(element("span", "message-status", statuses[data.status]));
  node.querySelector(".message-content").textContent = data.answer;
  const sources = Array.isArray(data.sources) ? data.sources : [];
  if (sources.length) {
    const links = element("div", "response-sources");
    sources.forEach((source) => {
      const url = sourceURL(source.url);
      if (!url) return;
      const link = element("a", "source-chip", `${source.title || "Источник"} · ${sourceLocation(source)} ↗`);
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      links.append(link);
    });
    node.append(links);
  }
  if (Array.isArray(data.trace) && data.trace.length) {
    const details = element("details", "trace");
    const list = element("ul");
    details.append(element("summary", "", "Как получен ответ"));
    data.trace.forEach((step) => list.append(element("li", "", `${step.name || "Инструмент"} · ${step.status === "ok" || step.status === "success" ? "выполнен" : step.status || "результат получен"}${Number.isFinite(step.duration_ms) ? ` · ${Math.round(step.duration_ms)} мс` : ""}`)));
    details.append(list);
    node.append(details);
  }
  const meta = [];
  if (Number.isFinite(data.elapsed_ms)) meta.push(`${(data.elapsed_ms / 1000).toFixed(1)} с`);
  if (data.request_id) meta.push(`Запрос ${data.request_id}`);
  if (meta.length) node.append(element("div", "response-meta", meta.join(" · ")));
  renderSources(sources, true);
  scrollConversation();
}

async function sendMessage(value) {
  const message = (typeof value === "string" ? value : $("question").value).trim();
  if (!message || !state.detail || state.loading || state.chat || materialOnly() || state.cardDirty || state.analysis || state.confirmation) return;
  if (message.length > 2000) { $("question").focus(); return; }
  const assignmentId = state.selectedId;
  const selection = state.selection;
  const controller = new AbortController();
  state.chat = controller;
  messageNode("user", message);
  const pending = messageNode("assistant", "Обрабатываем запрос. Его можно отменить.", true);
  $("question").value = "";
  updateControls();
  try {
    const history = state.history.slice(-6);
    const payload = { message, assignment_id: assignmentId, history };
    while (history.length && new TextEncoder().encode(JSON.stringify(payload)).length > 15000) history.splice(0, 2);
    const data = await requestJSON("/api/chat", { method: "POST", body: JSON.stringify(payload), signal: controller.signal });
    if (selection !== state.selection) return;
    if (typeof data.answer !== "string" || !data.answer.trim() || !["offline", "live"].includes(data.mode)) throw new ApiError("Получен неполный ответ ассистента.", "invalid_response", data.request_id);
    fillAnswer(pending, data);
    state.history.push({ role: "user", content: Array.from(message).slice(0, 500).join("") },
      { role: "assistant", content: Array.from(data.answer).slice(0, 500).join("") });
    state.history = state.history.slice(-6);
  } catch (error) {
    if (selection !== state.selection) return;
    pending.classList.remove("is-pending");
    pending.classList.add("is-error");
    pending.querySelector(".message-content").textContent = userErrorMessage(error);
    if (error.requestId) pending.append(element("div", "response-meta", `Запрос ${error.requestId}`));
    const retry = element("button", "retry-button", error.code === "cancelled" ? "Задать вопрос снова" : "Повторить запрос");
    retry.type = "button";
    retry.addEventListener("click", () => { sendMessage(message); });
    pending.append(retry);
    scrollConversation();
  } finally {
    if (state.chat === controller) { state.chat = null; updateControls(); }
  }
}

function renderPlan(data) {
  const totals = [data.available_minutes, data.required_minutes, data.deficit_minutes];
  if (!["feasible", "insufficient_time"].includes(data.status) || !Array.isArray(data.schedule) || !totals.every((value) => Number.isFinite(value) && value >= 0)) throw new ApiError("Получен неполный план подготовки.", "invalid_response");
  if (!data.schedule.every((day) => day && Number.isInteger(day.day) && day.day > 0 && Array.isArray(day.tasks) && day.tasks.every((task) => task && typeof task.title === "string" && Number.isFinite(task.minutes) && task.minutes > 0))) throw new ApiError("Не удалось проверить этапы плана.", "invalid_response");
  const target = $("plan-result");
  target.replaceChildren();
  const insufficient = data.status === "insufficient_time";
  const summary = element("div", `plan-summary${insufficient ? " is-insufficient" : ""}`);
  const heading = element("strong", "", insufficient ? `Не хватает ${formatMinutes(data.deficit_minutes)}` : data.required_minutes === 0 ? "Все задачи отмечены выполненными" : "План укладывается в доступное время");
  heading.setAttribute("role", "heading");
  heading.setAttribute("aria-level", "3");
  heading.setAttribute("tabindex", "-1");
  summary.append(heading);
  summary.append(element("span", "", `Нужно ${formatMinutes(data.required_minutes)} · доступно ${formatMinutes(data.available_minutes)}`));
  target.append(summary);
  if (Array.isArray(data.assumptions) && data.assumptions.length) {
    const assumptions = element("ul", "plan-assumptions");
    data.assumptions.forEach((text) => assumptions.append(element("li", "", text)));
    target.append(assumptions);
  }
  data.schedule.forEach((day) => {
    const tasks = Array.isArray(day.tasks) ? day.tasks : [];
    if (!tasks.length) return;
    const section = element("section", "plan-day");
    const heading = element("h3", "", `День ${day.day}`);
    heading.append(element("span", "", formatMinutes(tasks.reduce((sum, task) => sum + (Number.isFinite(task.minutes) ? task.minutes : 0), 0))));
    const list = element("ul");
    tasks.forEach((task) => list.append(element("li", "", `${task.title} · ${formatMinutes(task.minutes)}`)));
    section.append(heading, list);
    target.append(section);
  });
  target.hidden = false;
  $("plan-stale").hidden = true;
  return heading;
}

async function submitPlan(event) {
  event.preventDefault();
  const keyboardActivation = planKeyboardActivation;
  planKeyboardActivation = false;
  if (!state.detail || state.loading || state.plan || materialOnly() || state.cardDirty || state.analysis || state.confirmation) return;
  if (!$("plan-form").reportValidity()) return;
  const days = Number($("plan-days").value);
  const minutes = Number($("plan-minutes").value);
  if (!Number.isInteger(days) || !Number.isInteger(minutes) || days < 1 || days > 30 || minutes < 15 || minutes > 480) return;
  const controller = new AbortController();
  const selection = state.selection;
  const version = state.planVersion;
  state.plan = controller;
  $("plan-error").hidden = true;
  $("plan-result").hidden = true;
  updateControls();
  let focusTarget = null;
  let focusMoved = false;
  const trackFocus = (event) => { if (event.target !== document.body) focusMoved = true; };
  if (keyboardActivation) document.addEventListener("focusin", trackFocus);
  try {
    const payload = { assignment_id: state.selectedId, days, minutes_per_day: minutes };
    if (isUploaded()) {
      payload.card_revision = state.detail.card_revision;
      payload.tasks = cloneData(state.planTasks);
    }
    const data = await requestJSON("/api/plan", { method: "POST", body: JSON.stringify(payload), signal: controller.signal }, 15000);
    if (selection === state.selection && version === state.planVersion) focusTarget = renderPlan(data);
  } catch (error) {
    if (selection !== state.selection || error.code === "cancelled") return;
    $("plan-error").textContent = error.message;
    $("plan-error").hidden = false;
    if (error.status === 409 && isUploaded()) { $("card-reload").hidden = false; $("plan-error").textContent += " Обновите карточку перед новым расчётом."; }
    focusTarget = $("plan-button");
  } finally {
    if (keyboardActivation) document.removeEventListener("focusin", trackFocus);
    if (state.plan === controller) {
      state.plan = null;
      updateControls();
      if (keyboardActivation && !focusMoved && !controller.signal.aborted && selection === state.selection
          && version === state.planVersion && focusTarget && document.hasFocus()) {
        focusTarget.focus({ preventScroll: true });
        focusTarget.scrollIntoView({ behavior: scrollBehavior(), block: "nearest" });
      }
    }
  }
}

async function initialize() {
  $("initial-error").hidden = true;
  $("reload-button").disabled = true;
  try {
    const [status, data] = await Promise.all([requestJSON("/api/status", {}, 15000), requestJSON("/api/assignments", {}, 15000)]);
    if (!["offline", "live"].includes(status.mode) || !Array.isArray(data.assignments)) throw new ApiError("Сервис вернул неполную конфигурацию.", "invalid_response");
    showMode(status);
    state.assignments = data.assignments;
    renderAssignments();
    if (state.assignments.length) await selectAssignment(state.assignments[0].assignment_id);
    else {
      $("assignment-description").textContent = "Добавьте учебные материалы, чтобы начать работу с заданиями.";
      renderSources([]);
    }
  } catch (error) {
    $("initial-error-text").textContent = error.message;
    $("initial-error").hidden = false;
    $("mode-badge").className = "mode-badge is-error";
    $("mode-badge").hidden = true;
    $("mode-label").textContent = "Нет связи с сервисом";
    $("mode-notice").className = "mode-notice is-error";
    $("mode-notice").textContent = "Не удалось загрузить состояние сервиса. Сведения о подключении модели пока недоступны.";
  } finally { $("reload-button").disabled = false; updateControls(); }
}

$("chat-form").addEventListener("submit", (event) => { event.preventDefault(); sendMessage(); });
$("question").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); sendMessage(); } });
$("cancel-button").addEventListener("click", () => state.chat?.abort());
$("reload-button").addEventListener("click", initialize);
$("plan-button").addEventListener("click", (event) => { planKeyboardActivation = event.detail === 0; });
$("plan-form").addEventListener("submit", submitPlan);
$("plan-form").addEventListener("input", (event) => { if (!event.target.closest("#plan-tasks")) { invalidatePlan(true); updateControls(); } });
$("upload-form").addEventListener("submit", submitUpload);
$("upload-files").addEventListener("change", selectedFilesChanged);
$("upload-reset").addEventListener("click", () => { $("upload-files").value = ""; selectedFilesChanged(); });
$("upload-cancel").addEventListener("click", () => state.upload?.abort());
$("upload-toggle").addEventListener("click", () => { setUploadExpanded($("upload-body").hidden); });
$("material-search-form").addEventListener("submit", searchMaterials);
$("materials-toggle").addEventListener("click", () => {
  const expanded = $("materials-body").hidden;
  $("materials-body").hidden = !expanded;
  $("materials-toggle").setAttribute("aria-expanded", String(expanded));
  $("materials-toggle").textContent = expanded ? "Скрыть материалы" : "Показать материалы";
});
$("analyze-button").addEventListener("click", analyzeCard);
$("manual-card-button").addEventListener("click", openManualCard);
$("analyze-cancel").addEventListener("click", () => state.analysis?.abort());
$("card-form").addEventListener("submit", confirmCard);
$("card-form").addEventListener("input", () => {
  if (!state.cardDraft) return;
  state.cardDirty = true;
  invalidatePlan();
  updateControls();
});
$("card-toggle").addEventListener("click", () => {
  if (state.cardDirty || state.analysis || state.confirmation || state.detail?.status !== "ready") return;
  state.cardExpanded = !state.cardExpanded;
  updateCardVisibility();
});
$("card-reload").addEventListener("click", () => { if (state.selectedId) selectAssignment(state.selectedId, true); });
$("card-discard").addEventListener("click", () => {
  if (!state.detail) return;
  renderCard(state.detail, true);
  $("card-status").textContent = "Правки отменены. Показана последняя сохранённая карточка.";
  updateControls();
  $("card-goal").focus();
});
$("delete-assignment").addEventListener("click", () => { $("delete-confirmation").hidden = false; $("delete-confirm").focus(); });
$("delete-cancel").addEventListener("click", () => { $("delete-confirmation").hidden = true; $("delete-assignment").focus(); });
$("delete-confirm").addEventListener("click", deleteAssignment);
$("conversation").addEventListener("scroll", () => {
  const log = $("conversation");
  conversationAtBottom = log.scrollHeight - log.scrollTop - log.clientHeight <= 80;
});
for (const [id, step] of [["upload-panel", "upload"], ["materials-panel", "upload"], ["card-panel", "card"], ["planning", "planning"], ["chat-panel", "chat"]]) {
  $(id).addEventListener("focusin", () => {
    if (step === "planning" || step === "chat") collapseCardIfReady();
    if (workflowAvailability()[step]) { state.workflow = step; updateWorkflow(); }
  });
}
document.querySelectorAll("#workflow-nav [data-workflow]").forEach((button) => {
  button.addEventListener("click", () => navigateWorkflow(button.dataset.workflow));
  button.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const available = Array.from(document.querySelectorAll("#workflow-nav [data-workflow]")).filter((item) => !item.disabled);
    if (!available.length) return;
    event.preventDefault();
    const index = available.indexOf(button);
    const next = event.key === "Home" ? 0 : event.key === "End" ? available.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + available.length) % available.length;
    available[next].focus();
  });
});
document.querySelectorAll("[data-prompt]").forEach((button) => button.addEventListener("click", () => { sendMessage(button.dataset.prompt); }));
initialize();
