"""Explicit source formatting and HTTP privacy, without external or model APIs."""

from html.parser import HTMLParser

from fastapi.testclient import TestClient

from studynavigator.catalog import PDF_PATH
from studynavigator.config import Settings
from studynavigator.main import create_app
from studynavigator.text_format import parse_text_blocks, render_structured_text


class ObservedHTML(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.elements = []
        self.text = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


def test_inline_pdf_bullets_become_a_semantic_list_without_losing_item_text():
    text = "Правила: ● Первый пункт; • Второй пункт. ▪ Python 3.12: сохранить. ◦ Последний. ● ●"
    blocks = parse_text_blocks(text)
    assert blocks == [{"type": "p", "text": "Правила:"}, {"type": "ul", "items": [
        {"text": "Первый пункт;"}, {"text": "Второй пункт."},
        {"text": "Python 3.12: сохранить."}, {"text": "Последний."}]}]
    parsed = ObservedHTML(render_structured_text(text))
    assert [tag for tag, _ in parsed.elements] == ["p", "ul", "li", "li", "li", "li"]


def test_paragraphs_continuations_and_nonconsecutive_numbering_are_preserved():
    text = "Введение.\r\nПродолжение.\r\n\r\n- Альфа\n  продолжение\n- Бета\n\n3. Третий\n5) Пятый\n\nФинал."
    blocks = parse_text_blocks(text)
    assert blocks == [
        {"type": "p", "text": "Введение. Продолжение."},
        {"type": "ul", "items": [{"text": "Альфа продолжение"}, {"text": "Бета"}]},
        {"type": "ol", "start": 3, "items": [{"text": "Третий", "value": 3}, {"text": "Пятый", "value": 5}]},
        {"type": "p", "text": "Финал."},
    ]
    parsed = ObservedHTML(render_structured_text(text))
    assert ("ol", {"start": "3"}) in parsed.elements
    assert ("li", {"value": "5"}) in parsed.elements


def test_versions_urls_decimals_minus_signs_and_ambiguous_ids_stay_plain():
    text = ("Python 3.12; 2.5; https://example.org/v3.12?q=2.5. Значение -5, разность 4 - 2.\n\n"
            "003. Это ID, не перенумеровывать.\n2147483648. Большой ID.\nColon: literal text.")
    blocks = parse_text_blocks(text)
    assert [block["type"] for block in blocks] == ["p", "p"]
    assert blocks[0]["text"] == text.split("\n\n")[0]
    assert blocks[1]["text"] == "003. Это ID, не перенумеровывать. 2147483648. Большой ID. Colon: literal text."


def test_document_html_is_escaped_and_entities_are_never_decoded_into_markup():
    text = 'До <script>alert("x")</script>.\n\n● <img src=x onerror=alert(1)> & текст.\n● &lt;em&gt;буквально&lt;/em&gt;'
    rendered = render_structured_text(text)
    parsed = ObservedHTML(rendered)
    assert {tag for tag, _ in parsed.elements} == {"p", "ul", "li"}
    assert '<script>alert("x")</script>' in "".join(parsed.text)
    assert '<img src=x onerror=alert(1)> & текст.' in "".join(parsed.text)
    assert '&lt;em&gt;буквально&lt;/em&gt;' in "".join(parsed.text)
    assert "&lt;script&gt;" in rendered and "&amp;lt;em&amp;gt;" in rendered


def test_source_reader_formats_actual_pdf_bullets_but_keeps_raw_api_and_private_pdf_links(tmp_path):
    class NoModel:
        mode = "offline"
        model = None

        async def complete(self, *args, **kwargs):
            raise AssertionError("Formatting must never invoke a language model")

    app = create_app(Settings(database_path=tmp_path / "study.db", rate_limit_per_minute=1000), provider=NoModel())
    with TestClient(app) as owner, TestClient(app) as stranger:
        uploaded = owner.post("/api/assignments", files={"files": (
            "<img src=x onerror=alert(1)>.pdf", PDF_PATH.read_bytes(), "application/pdf")})
        assert uploaded.status_code == 201
        source = uploaded.json()["assignment"]["sources"][0]
        api_path = "/api/sources/" + source["source_id"]
        before = owner.get(api_path).json()
        assert "●" in before["excerpt"]
        rendered = owner.get(source["url"])
        assert rendered.status_code == 200
        parsed = ObservedHTML(rendered.text)
        assert ("main", {"id": "source-reader", "class": "source-reader"}) in parsed.elements
        assert ("div", {"class": "structured-text"}) in parsed.elements
        assert "ul" in [tag for tag, _ in parsed.elements]  # Previously a single flat blockquote.
        assert not {"script", "img", "blockquote"} & {tag for tag, _ in parsed.elements}
        assert any(tag == "link" and attrs.get("href") == "/static/styles.css?v=20261027-ui3" for tag, attrs in parsed.elements)
        assert any(tag == "a" and attrs.get("href") == source["document_url"] + "#page=1"
                   and attrs.get("rel") == "noopener" for tag, attrs in parsed.elements)
        assert owner.get(api_path).json() == before
        assert stranger.get(source["url"]).status_code == 404
        assert stranger.get(api_path).status_code == 404
        assert stranger.get(source["document_url"]).status_code == 404
        seeded = owner.get("/sources/personal-project-mcp-p2")
        assert seeded.status_code == 200
        assert 'href="/materials/personal-project.pdf#page=2"' in seeded.text
