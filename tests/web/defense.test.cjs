const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const html = fs.readFileSync(path.join(__dirname, "../../web/defense.html"), "utf8");
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];

// State/interaction unit fixture only; this does not run a real browser.
function harness(hash = "") {
  let focused = null;
  function node(id = "", tagName = "BUTTON", dataset = {}) {
    return { id, tagName, dataset, children: [], attributes: {}, events: {}, value: "", textContent: "",
      append(child) { this.children.push(child); },
      addEventListener(name, handler) { this.events[name] = handler; },
      setAttribute(name, value) { this.attributes[name] = value; },
      focus() { focused = this.id; },
      click() { if (!this.disabled) this.events.click?.({ preventDefault() {} }); } };
  }
  const nodes = new Map();
  const get = id => { if (!nodes.has(id)) nodes.set(id, node(id)); return nodes.get(id); };
  const slides = Array.from(html.matchAll(/<article class="slide" id="([^"]+)"[\s\S]*?<h[12] id="([^"]+)"[^>]*>([\s\S]*?)<\/h[12]>/g), match => {
    const heading = get(match[2]); heading.textContent = match[3].replace(/<[^>]*>/g, "");
    return { id: match[1], querySelector: () => heading };
  });
  const scenarios = Array.from(html.matchAll(/data-scenario="([^"]+)"/g), match => node("", "BUTTON", { scenario: match[1] }));
  const errors = Array.from(html.matchAll(/data-error="([^"]+)"/g), match => node("", "BUTTON", { error: match[1] }));
  get("available-minutes").tagName = "INPUT";
  get("available-minutes").value = html.match(/id="available-minutes"[^>]*value="(\d+)"/)[1];
  const document = { events: {}, getElementById: get, createElement: () => node(),
    querySelector: () => get("brand"), addEventListener(name, handler) { this.events[name] = handler; },
    querySelectorAll(selector) { return selector === ".slide" ? slides : selector === "[data-scenario]" ? scenarios : errors; } };
  const location = { hash };
  vm.runInNewContext(source, { document, location, history: { replaceState(_state, _title, next) { location.hash = next; } }, window: { scrollTo() {} } });
  function press(key, target = { tagName: "BODY" }) {
    let prevented = false;
    document.events.keydown({ key, target, preventDefault() { prevented = true; } });
    return prevented;
  }
  return { get, slides, scenarios, location, press, focused: () => focused };
}

test("slide navigation updates visibility, hash, controls and heading focus", () => {
  const app = harness("#slide-4");
  assert.equal(app.get("slide-count").textContent, "4 / 8");
  assert.equal(app.slides.filter(slide => !slide.hidden).length, 1);
  assert.equal(app.focused(), null);
  app.press("ArrowRight");
  assert.equal(app.location.hash, "#slide-5");
  assert.equal(app.focused(), "title-5");
  app.press("ArrowLeft");
  assert.equal(app.location.hash, "#slide-4");
  app.press("End");
  assert.equal(app.get("next").disabled, true);
  assert.equal(app.location.hash, "#slide-8");
  app.press("Home");
  assert.equal(app.get("previous").disabled, true);
  app.get("next").click();
  assert.equal(app.get("slide-count").textContent, "2 / 8");
});

test("range keyboard stays in the control; arithmetic handles deficit and feasible boundaries", () => {
  const app = harness();
  const range = app.get("available-minutes"); range.focus();
  assert.equal(app.press("ArrowRight", range), false);
  assert.equal(app.location.hash, "#slide-1");
  assert.equal(app.focused(), "available-minutes");
  for (const [value, expected] of [[15, "Не хватает 285 минут"], [120, "Не хватает 180 минут"], [480, "План укладывается"]]) {
    range.value = String(value); range.events.input();
    assert.equal(app.get("plan-summary").textContent, expected);
  }
});

test("MCP examples distinguish a sourced fact from the unknown deadline", () => {
  const app = harness();
  app.scenarios.find(button => button.dataset.scenario === "unknown").click();
  const unknown = JSON.parse(app.get("mcp-example").textContent);
  assert.equal(unknown[1].due_date, null);
  assert.equal(unknown[2].status, "insufficient_data");
  assert.match(app.get("example-answer").textContent, /дата сдачи не указана/);
  app.scenarios.find(button => button.dataset.scenario === "known").click();
  const known = JSON.parse(app.get("mcp-example").textContent);
  assert.equal(known[1].tool, "search_materials");
  assert.equal(known[2].page, 1);
  assert.match(app.get("example-answer").textContent, /репозиторий/);
});
