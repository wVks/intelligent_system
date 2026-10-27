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
