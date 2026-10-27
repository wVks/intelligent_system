"""Conservative presentation of plain text; never interpret document HTML."""

import html
import re


_BULLETS = re.compile(r"[●•▪◦]")
_HYPHEN = re.compile(r"^-\s+(.+)$")
_NUMBER = re.compile(r"^([0-9]+)[.)]\s+(.+)$")


def parse_text_blocks(text: str) -> list[dict]:
    """Recognize explicit markers only, without guessing lost PDF headings."""
    blocks = []
    current = None

    def plain(value):
        nonlocal current
        if not value:
            return
        if current is None:
            current = {"type": "p", "text": value}
            blocks.append(current)
        elif current["type"] == "p":
            current["text"] += " " + value
        else:
            item = current["items"][-1]
            item["text"] = (item["text"] + " " + value).strip()

    def item(kind, value, number=None):
        nonlocal current
        if not value:
            return
        if current is None or current["type"] != kind:
            current = {"type": kind, "items": []}
            if kind == "ol":
                current["start"] = number
            blocks.append(current)
        entry = {"text": value}
        if kind == "ol":
            entry["value"] = number
        current["items"].append(entry)

    for line in text.replace("\r\n", "\n").replace("\r", "\n").strip().split("\n"):
        line = line.strip()
        if not line:
            current = None
            continue
        parts = _BULLETS.split(line)
        if len(parts) > 1:
            plain(parts[0].strip())
            for value in parts[1:]:
                if value.strip():
                    item("ul", value.strip())
            continue
        hyphen = _HYPHEN.fullmatch(line)
        if hyphen:
            item("ul", hyphen.group(1))
            continue
        numbered = _NUMBER.fullmatch(line)
        if numbered:
            digits = numbered.group(1)
            # HTML ol.start/li.value are signed 32-bit. Keep ambiguous IDs,
            # leading zeroes and larger ordinals as their unchanged plain text.
            if len(digits) <= 10:
                number = int(digits)
                if number <= 2_147_483_647 and str(number) == digits:
                    item("ol", numbered.group(2), number)
                    continue
        plain(line)
    return blocks


def render_structured_text(text: str) -> str:
    """Render only fixed tags, escaping every document-supplied text fragment."""
    rendered = []
    for block in parse_text_blocks(text):
        if block["type"] == "p":
            rendered.append("<p>" + html.escape(block["text"]) + "</p>")
            continue
        ordered = block["type"] == "ol"
        tag = "ol" if ordered else "ul"
        start = f' start="{block["start"]}"' if ordered else ""
        items = []
        for entry in block["items"]:
            value = f' value="{entry["value"]}"' if ordered else ""
            items.append(f"<li{value}>" + html.escape(entry["text"]) + "</li>")
        rendered.append(f"<{tag}{start}>" + "".join(items) + f"</{tag}>")
    return "\n".join(rendered)
