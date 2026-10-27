"""Stage 1 upload, provenance, persistence and browser-session boundaries.

PDF fixtures are generated in memory using a regular text layer; tests do not
need an external PDF program, reportlab, a network provider or paid LLM calls.
"""

import asyncio
from http.cookies import SimpleCookie
from io import BytesIO

import httpx
import pytest
from fastapi.testclient import TestClient

from studynavigator.config import Settings
from studynavigator.ingestion import UploadError
from studynavigator.main import create_app
from studynavigator.main import extract_uploaded_documents
from studynavigator.mcp_gateway import GatewayError, MCPGateway
from studynavigator.sessions import COOKIE_NAME


def text_pdf(*pages: str, padding: int = 0) -> bytes:
    """Build a small valid PDF with selectable Helvetica text on every page."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids ["
        + b" ".join(f"{4 + index * 2} 0 R".encode() for index in range(len(pages)))
        + f"] /Count {len(pages)} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index, content in enumerate(pages):
        escaped = content.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 50 750 Td ({escaped}) Tj ET".encode("ascii")
        if padding:
            stream += b"\n%" + b"x" * padding
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> "
            + f"/Contents {5 + index * 2} 0 R >>".encode()
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        result.extend(f"{offset:010} 00000 n \n".encode())
    result.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(result)


class NoModelCalls:
    mode = "offline"
    model = None

    def __init__(self):
        self.calls = 0

    async def complete(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("Stage 1 must not call a model")


@pytest.fixture
def settings(tmp_path):
    return Settings(database_path=tmp_path / "study.sqlite", rate_limit_per_minute=1000)


@pytest.fixture
def provider():
    return NoModelCalls()


@pytest.fixture
def client(settings, provider):
    with TestClient(create_app(settings=settings, provider=provider)) as browser:
        assert browser.get("/").status_code == 200
        yield browser
    assert provider.calls == 0


def upload(client, *files, title="My assignment"):
    files = files or [("assignment.pdf", text_pdf("ORION assignment requirements"))]
    return client.post(
        "/api/assignments",
        data={"title": title},
        files=[("files", (name, content, "application/pdf")) for name, content in files],
    )


def assignment_ids(client):
    response = client.get("/api/assignments")
    assert response.status_code == 200
    return {item["assignment_id"] for item in response.json()["assignments"]}


def assert_controlled_error(response, status=None):
    if status is None:
        assert 400 <= response.status_code < 500
    else:
        assert response.status_code == status
    body = response.json()
    assert body["error"]["code"]
    assert body["error"]["message"]
    assert body["request_id"]


def test_upload_indexes_multiple_documents_with_original_pages(client, provider):
    main = text_pdf("Alpha requirement: submit a notebook.", "ORION milestone belongs on page two.")
    appendix = text_pdf("Telescope appendix and supporting materials.")
    response = upload(client, ("main.pdf", main), ("appendix.pdf", appendix), title="Astronomy project")
    assert response.status_code == 201
    assignment = response.json()["assignment"]
    selected_id = assignment["assignment_id"]
    assert assignment["title"] == "Astronomy project"
    assert assignment["status"] == "awaiting_analysis"
    assert assignment["due_date"] is None and assignment["due_at"] is None
    assert assignment["requirements"] == []
    assert assignment["criteria"] == []
    assert assignment["facts"] == []
    documents = {item["filename"]: item for item in assignment["documents"]}
    assert set(documents) == {"main.pdf", "appendix.pdf"}
    assert documents["main.pdf"]["page_count"] == 2
    assert documents["appendix.pdf"]["page_count"] == 1
    assert all("stored_name" not in item for item in documents.values())
    assert all(len(item["sha256"]) == 64 for item in documents.values())
    assert selected_id in assignment_ids(client)
    details = client.get(f"/api/assignments/{selected_id}")
    assert details.status_code == 200
    assert details.json()["assignment"]["documents"] == assignment["documents"]

    found = client.get(f"/api/assignments/{selected_id}/materials", params={"q": "ORION"})
    assert found.status_code == 200
    matches = found.json()["matches"]
    assert len(matches) == 1
    source = matches[0]
    assert source["page"] == 2
    assert source["document_id"] == documents["main.pdf"]["document_id"]
    assert "ORION milestone" in source["excerpt"]
    assert len(source["excerpt"]) <= 1200
    page = client.get(source["url"])
    assert page.status_code == 200
    assert source["document_url"] in page.text
    assert "#page=2" in page.text
    source_data = client.get(f"/api/sources/{source['source_id']}")
    assert source_data.status_code == 200
    assert source_data.json()["excerpt"] == source["excerpt"]
    document = client.get(documents["main.pdf"]["document_url"])
    assert document.status_code == 200
    assert document.content == main
    assert "application/pdf" in document.headers["content-type"]
    assert provider.calls == 0


def test_upload_has_its_own_body_limit_instead_of_the_small_json_limit(client):
    document = text_pdf("A valid ORION text layer", padding=20000)
    assert len(document) > 16384
    response = upload(client, ("large-valid.pdf", document))
    assert response.status_code == 201
    document_url = response.json()["assignment"]["documents"][0]["document_url"]
    assert client.get(document_url).content == document


def test_pending_assignment_does_not_generate_fake_answers_or_a_plan(client):
    response = upload(client)
    assert response.status_code == 201
    selected_id = response.json()["assignment"]["assignment_id"]
    for endpoint, body in [
        ("/api/chat", {"message": "Explain these requirements", "assignment_id": selected_id}),
        ("/api/plan", {"assignment_id": selected_id, "days": 3, "minutes_per_day": 60}),
    ]:
        rejected = client.post(endpoint, json=body)
        assert_controlled_error(rejected, 409)
        assert rejected.json()["error"]["code"] == "assignment_not_ready"


@pytest.mark.parametrize(
    "files",
    [
        [("broken.pdf", b"%PDF-1.4\nnot a real PDF")],
        [("scan.pdf", text_pdf(""))],
        [("not-a-pdf.txt", b"plain text pretending to be a file")],
        [(f"document-{index}.pdf", text_pdf("Valid material")) for index in range(4)],
        [("too-long.pdf", text_pdf("A" * 16001))],
    ],
    ids=["corrupt", "no-text-layer", "wrong-format", "four-files", "too-much-text"],
)
def test_invalid_upload_leaves_no_partial_assignment_or_original_files(client, tmp_path, files):
    before = assignment_ids(client)
    response = upload(client, *files)
    assert_controlled_error(response)
    assert assignment_ids(client) == before
    assert not list(tmp_path.rglob("*.pdf"))


def test_empty_upload_is_rejected(client):
    response = client.post("/api/assignments", data={"title": "Empty assignment"})
    assert_controlled_error(response)
    assert assignment_ids(client) == {"personal-project"}


def test_encrypted_pdf_is_rejected_without_importing_partial_content(client, tmp_path):
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(text_pdf("Secret ORION requirement"))))
    writer.encrypt("private-password")
    buffer = BytesIO()
    writer.write(buffer)
    response = upload(client, ("encrypted.pdf", buffer.getvalue()))
    assert_controlled_error(response)
    assert assignment_ids(client) == {"personal-project"}
    assert not list(tmp_path.rglob("*.pdf"))


def test_mixed_valid_and_corrupt_upload_is_all_or_nothing(client, tmp_path):
    response = upload(
        client,
        ("valid.pdf", text_pdf("Valid ORION assignment")),
        ("broken.pdf", b"%PDF-1.7\nbroken"),
    )
    assert_controlled_error(response)
    assert assignment_ids(client) == {"personal-project"}
    assert not list(tmp_path.rglob("*.pdf"))


@pytest.mark.parametrize("streamed", [False, True])
def test_oversized_pdf_is_rejected_with_or_without_content_length(client, tmp_path, streamed):
    data = text_pdf("Valid but oversized material")
    data += b" " * ((10 * 1024 * 1024 + 1) - len(data))
    if streamed:
        request = httpx.Request(
            "POST", "http://testserver/api/assignments",
            files=[("files", ("large.pdf", data, "application/pdf"))],
        )
        body = request.read()
        response = client.post(
            "/api/assignments",
            content=(body[offset:offset + 65536] for offset in range(0, len(body), 65536)),
            headers={"content-type": request.headers["content-type"]},
        )
    else:
        response = upload(client, ("large.pdf", data))
    assert_controlled_error(response, 413)
    assert assignment_ids(client) == {"personal-project"}
    assert not list(tmp_path.rglob("*.pdf"))


def test_two_browser_sessions_cannot_read_search_plan_or_delete_each_others_materials(settings, provider):
    app = create_app(settings=settings, provider=provider)
    with TestClient(app) as first, TestClient(app) as second:
        first.get("/")
        second.get("/")
        response = upload(first)
        assert response.status_code == 201
        assignment = response.json()["assignment"]
        selected_id = assignment["assignment_id"]
        source = assignment["sources"][0]
        assert selected_id in assignment_ids(first)
        assert selected_id not in assignment_ids(second)
        for endpoint in [
            f"/api/assignments/{selected_id}",
            f"/api/assignments/{selected_id}/materials?q=ORION",
            f"/api/sources/{source['source_id']}",
            source["url"],
            assignment["documents"][0]["document_url"],
        ]:
            assert_controlled_error(second.get(endpoint), 404)
        assert_controlled_error(second.post("/api/chat", json={
            "assignment_id": selected_id, "message": "What is ORION?",
        }), 404)
        assert_controlled_error(second.post("/api/plan", json={
            "assignment_id": selected_id, "days": 2, "minutes_per_day": 60,
        }), 404)
        assert_controlled_error(second.delete(f"/api/assignments/{selected_id}"), 404)
        assert first.get(source["url"]).status_code == 200
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_mcp_without_a_trusted_workspace_never_exposes_private_uploads(client, settings):
    response = upload(client)
    assert response.status_code == 201
    selected_id = response.json()["assignment"]["assignment_id"]
    async with MCPGateway(settings.database_path) as gateway:
        result = await gateway.call("list_assignments", {})
        assert {item["assignment_id"] for item in result["assignments"]} == {"personal-project"}
        result = await gateway.call("search_materials", {"query": "ORION"})
        assert result == {"matches": []}
        with pytest.raises(GatewayError) as error:
            await gateway.call("get_assignment", {"assignment_id": selected_id})
        assert error.value.code == "unknown_assignment"


def test_uploaded_assignment_survives_new_app_and_seed_with_same_browser_cookie(settings, provider):
    original_pdf = text_pdf("ORION requirements survive server restart.")
    with TestClient(create_app(settings=settings, provider=provider)) as first:
        first.get("/")
        response = upload(first, ("persist.pdf", original_pdf))
        assert response.status_code == 201
        assignment = response.json()["assignment"]
        saved_cookies = dict(first.cookies.items())
    with TestClient(create_app(settings=settings, provider=provider)) as restarted:
        restarted.cookies.update(saved_cookies)
        selected_id = assignment["assignment_id"]
        assert assignment_ids(restarted) == {"personal-project", selected_id}
        response = restarted.get(f"/api/assignments/{selected_id}")
        assert response.status_code == 200
        assert response.json()["assignment"]["documents"] == assignment["documents"]
        response = restarted.get(assignment["documents"][0]["document_url"])
        assert response.status_code == 200 and response.content == original_pdf
        response = restarted.get(f"/api/assignments/{selected_id}/materials", params={"q": "ORION"})
        assert response.status_code == 200 and response.json()["matches"]
    assert provider.calls == 0


def test_delete_removes_originals_sources_and_search_but_preserves_seed(client, tmp_path):
    response = upload(client)
    assert response.status_code == 201
    assignment = response.json()["assignment"]
    selected_id = assignment["assignment_id"]
    source = assignment["sources"][0]
    assert list(tmp_path.rglob("*.pdf"))
    assert client.delete(f"/api/assignments/{selected_id}").status_code == 204
    assert assignment_ids(client) == {"personal-project"}
    assert not list(tmp_path.rglob("*.pdf"))
    for endpoint in [
        f"/api/assignments/{selected_id}",
        source["url"],
        f"/api/assignments/{selected_id}/materials?q=ORION",
        assignment["documents"][0]["document_url"],
    ]:
        assert_controlled_error(client.get(endpoint), 404)
    rejected = client.delete("/api/assignments/personal-project")
    assert_controlled_error(rejected)
    assert client.get("/materials/personal-project.pdf").status_code == 200


def test_existing_example_still_answers_through_actual_mcp_after_upload(settings):
    with TestClient(create_app(settings=settings)) as browser:
        browser.get("/")
        response = upload(browser)
        assert response.status_code == 201
        response = browser.post("/api/chat", json={
            "message": "Что отправить в LMS?", "assignment_id": "personal-project",
        })
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "answered"
        assert any(item["name"] == "get_assignment" and item["status"] == "ok" for item in result["trace"])
        assert {fact["key"]: fact["value"] for fact in result["facts"]}["lms_links_count"] == 2


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_browser_cookie_flags_follow_the_transport(settings, provider, scheme):
    with TestClient(create_app(settings=settings, provider=provider), base_url=f"{scheme}://testserver") as browser:
        response = browser.get("/")
        cookies = SimpleCookie()
        cookies.load(response.headers["set-cookie"])
        session = cookies[COOKIE_NAME]
        assert len(session.value) == 64
        assert session["httponly"]
        assert session["samesite"].lower() == "lax"
        assert session["path"] == "/"
        assert bool(session["secure"]) is (scheme == "https")


def test_private_material_responses_are_not_cached(client):
    response = upload(client)
    assert response.status_code == 201
    assignment = response.json()["assignment"]
    source = assignment["sources"][0]
    for endpoint in [
        "/api/assignments",
        f"/api/assignments/{assignment['assignment_id']}",
        f"/api/sources/{source['source_id']}",
        source["url"],
        assignment["documents"][0]["document_url"],
    ]:
        response = client.get(endpoint)
        assert response.status_code == 200
        directives = {item.strip().lower() for item in response.headers["cache-control"].split(",")}
        assert {"private", "no-store"} <= directives
        assert "cookie" in response.headers["vary"].lower()


def test_cross_origin_mutations_are_rejected_without_changing_materials(client):
    response = client.post(
        "/api/assignments", headers={"origin": "https://unrelated.invalid"},
        files=[("files", ("assignment.pdf", text_pdf("ORION material"), "application/pdf"))],
    )
    assert_controlled_error(response, 403)
    assert response.json()["error"]["code"] == "invalid_origin"
    assert assignment_ids(client) == {"personal-project"}

    response = upload(client)
    assert response.status_code == 201
    selected_id = response.json()["assignment"]["assignment_id"]
    response = client.delete(f"/api/assignments/{selected_id}", headers={"origin": "null"})
    assert_controlled_error(response, 403)
    assert selected_id in assignment_ids(client)


class StalledExtractor:
    """Model a child blocked during parsing, then require it to be reaped."""

    def __init__(self):
        self.returncode = None
        self.started = asyncio.Event()
        self.killed = False
        self.reaped = False

    async def communicate(self, payload=None):
        if payload is not None:
            self.started.set()
            await asyncio.Event().wait()
        assert self.killed
        self.reaped = True
        return b"", b""

    def kill(self):
        self.killed = True
        self.returncode = -9


@pytest.mark.asyncio
async def test_pdf_parse_timeout_kills_and_reaps_the_child(monkeypatch):
    process = StalledExtractor()

    async def spawn(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(UploadError) as error:
        await extract_uploaded_documents([("assignment.pdf", text_pdf("ORION"))], timeout=0.01)
    assert error.value.code == "upload_timeout"
    assert error.value.status_code == 408
    assert process.killed and process.reaped


@pytest.mark.asyncio
async def test_cancelling_pdf_parse_kills_and_reaps_the_child(monkeypatch):
    process = StalledExtractor()

    async def spawn(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(extract_uploaded_documents([("assignment.pdf", text_pdf("ORION"))], timeout=10))
    await asyncio.wait_for(process.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed and process.reaped
