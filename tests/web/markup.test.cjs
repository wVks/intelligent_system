const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

test("the shipped HTML contains the DOM targets and accessible field references used by the app", () => {
  const root = path.resolve(__dirname, "../..");
  const html = fs.readFileSync(path.join(root, "web/index.html"), "utf8");
  const script = fs.readFileSync(path.join(root, "web/app.js"), "utf8");
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
  const available = new Set(ids);
  assert.equal(available.size, ids.length, "HTML IDs must be unique");
  const references = [...script.matchAll(/\$\("([^"]+)"\)/g)].map((match) => match[1]);
  for (const match of html.matchAll(/\b(?:for|aria-controls|aria-labelledby|aria-describedby)="([^"]+)"/g)) {
    references.push(...match[1].split(/\s+/));
  }
  const missing = [...new Set(references.filter((id) => !available.has(id)))];
  assert.deepEqual(missing, [], "Missing DOM targets can prevent application startup");
});

test("planning follows chat as a full-width grid child and the shared text formatter loads before the app", () => {
  const html = fs.readFileSync(path.resolve(__dirname, "../../web/index.html"), "utf8");
  const root = { tag: "root", children: [] }, stack = [root], ids = new Map();
  const voidTags = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"]);
  for (const match of html.matchAll(/<(\/?)([a-z][\w:-]*)([^>]*)>/gi)) {
    const tag = match[2].toLowerCase();
    if (match[1]) { if (stack.at(-1).tag === tag) stack.pop(); continue; }
    const id = /\bid="([^"]+)"/.exec(match[3])?.[1];
    const node = { tag, id, parent: stack.at(-1), children: [] };
    node.parent.children.push(node);
    if (id) ids.set(id, node);
    if (!voidTags.has(tag) && !/\/\s*$/.test(match[3])) stack.push(node);
  }
  const grid = ids.get("workspace-grid"), chat = ids.get("chat-panel"), planning = ids.get("planning");
  assert.equal(chat.parent, grid);
  assert.equal(planning.parent, grid, "Planning must not remain in the narrow source column");
  assert.equal(grid.children[grid.children.indexOf(chat) + 1], planning, "Planning follows chat in reading order");
  assert.equal(ids.get("sources-panel").parent, ids.get("context-column"));
  const scripts = [...html.matchAll(/<script\b[^>]*\bsrc="([^"]+)"[^>]*>/g)].map((match) => match[1]);
  assert.deepEqual(scripts.slice(0, 2), ["/static/text-format.js?v=20261027-ui3", "/static/app.js?v=20261027-ui3"]);
});
