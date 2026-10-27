/* Plain text presentation only: document/model content never becomes HTML. */
(function (root) {
  "use strict";

  function parse(value) {
    const blocks = [];
    let current = null;
    const flush = () => { if (current) blocks.push(current); current = null; };
    function plain(text) {
      text = text.trim();
      if (!text) return;
      if (current && current.type !== "p") {
        const item = current.items[current.items.length - 1];
        item.text += ` ${text}`;
      } else {
        if (current?.type !== "p") { flush(); current = { type: "p", text: "" }; }
        current.text += `${current.text ? " " : ""}${text}`;
      }
    }
    function item(type, text, number) {
      text = text.trim();
      if (!text) return;
      if (current?.type !== type) {
        flush();
        current = type === "ol" ? { type, start: number, items: [] } : { type, items: [] };
      }
      current.items.push(type === "ol" ? { text, value: number } : { text });
    }
    for (const rawLine of String(value ?? "").replace(/\r\n?/g, "\n").split("\n")) {
      const line = rawLine.trim();
      if (!line) { flush(); continue; }
      if (/[●•▪◦]/u.test(line)) {
        const pieces = line.split(/[●•▪◦]/u);
        if (pieces[0].trim()) plain(pieces[0]);
        pieces.slice(1).forEach((text) => item("ul", text));
        continue;
      }
      const ordered = line.match(/^(\d+)[.)]\s+(.+)$/u);
      if (ordered && String(Number(ordered[1])) === ordered[1] && Number(ordered[1]) <= 2147483647) {
        item("ol", ordered[2], Number(ordered[1]));
        continue;
      }
      const bullet = line.match(/^-\s+(.+)$/u);
      if (bullet) { item("ul", bullet[1]); continue; }
      plain(line);
    }
    flush();
    return blocks;
  }

  function render(target, value) {
    const document = target.ownerDocument || root.document;
    target.replaceChildren();
    for (const block of parse(value)) {
      const node = document.createElement(block.type);
      if (block.type === "p") node.textContent = block.text;
      else {
        if (block.type === "ol") node.setAttribute("start", String(block.start));
        for (const item of block.items) {
          const row = document.createElement("li");
          if (block.type === "ol") row.setAttribute("value", String(item.value));
          row.textContent = item.text;
          node.append(row);
        }
      }
      target.append(node);
    }
    return target;
  }

  root.StudyTextFormat = Object.freeze({ parse, render });
  if (typeof module !== "undefined" && module.exports) module.exports = root.StudyTextFormat;
})(globalThis);
