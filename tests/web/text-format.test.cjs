const test = require("node:test");
const assert = require("node:assert/strict");
const { parse, render } = require("../../web/text-format.js");

test("inline PDF bullet glyphs become one list without losing the introductory paragraph", () => {
  assert.deepEqual(parse("Нужно подготовить: ● README • рабочий прототип ▪ MCP ◦ защиту"), [
    { type: "p", text: "Нужно подготовить:" },
    { type: "ul", items: ["README", "рабочий прототип", "MCP", "защиту"].map((text) => ({ text })) },
  ]);
});

test("numbered lines retain their original starting number, gaps and wrapped explanations", () => {
  assert.deepEqual(parse("3. Загрузить PDF\nи проверить текст\n5) Составить план"), [
    { type: "ol", start: 3, items: [{ text: "Загрузить PDF и проверить текст", value: 3 }, { text: "Составить план", value: 5 }] },
  ]);
});

test("blank lines separate paragraphs and lists while ordinary line wraps remain readable", () => {
  assert.deepEqual(parse("Первый абзац\r\nпродолжается.\r\n\r\n- PDF\n- План\n\nПоследний абзац."), [
    { type: "p", text: "Первый абзац продолжается." },
    { type: "ul", items: [{ text: "PDF" }, { text: "План" }] },
    { type: "p", text: "Последний абзац." },
  ]);
});

test("versions, decimals, URLs, negative values and inline hyphens are plain text", () => {
  const text = "Python 3.12\n2.5 часа\nhttps://example.test/v1.2/3.4\n-5 градусов\nСрок - не указан.\n999999999999999999999. Число\n2147483648. Число\n03. Идентификатор";
  assert.deepEqual(parse(text), [{ type: "p", text: text.split("\n").join(" ") }]);
});

test("render creates only safe text blocks, preserves literal HTML, and clears a previous pending message", () => {
  const created = [];
  const document = {
    createElement(tagName) {
      created.push(tagName);
      return {
        tagName, children: [], attributes: {}, textContent: "",
        set innerHTML(_) { throw new Error("HTML must never be interpreted"); },
        setAttribute(name, value) { this.attributes[name] = value; },
        append(node) { this.children.push(node); },
      };
    },
  };
  const target = {
    ownerDocument: document, children: ["pending"],
    replaceChildren() { this.children = []; }, append(node) { this.children.push(node); },
  };
  const malicious = '<img src=x onerror="alert(1)"><script>alert(2)</script>';
  render(target, `${malicious}\n\n4. javascript:alert(3)\n6. &lt;b&gt;literal&lt;/b&gt;`);
  assert.deepEqual(created, ["p", "ol", "li", "li"]);
  assert.equal(target.children[0].textContent, malicious);
  assert.equal(target.children[1].attributes.start, "4");
  assert.equal(target.children[1].children[1].attributes.value, "6");
  assert.equal(target.children[1].children[1].textContent, "&lt;b&gt;literal&lt;/b&gt;");
  render(target, " \n ");
  assert.deepEqual(target.children, []);
});
