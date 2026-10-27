"""Real PDF parsing, persistence and workspace boundaries (no LLM calls)."""

import base64
import hashlib
import json
import sqlite3
import subprocess
import sys
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from studynavigator.catalog import CatalogError, PDF_PATH, Store, seed_database
from studynavigator.ingestion import (
    MAX_PAGE_STREAM_BYTES,
    MAX_UPLOAD_BYTES,
    UploadError,
    extract_documents,
    ingest_documents,
)


def text_pdf(*pages):
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        literal = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({literal}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.fixture
def storage(tmp_path):
    database = tmp_path / "study.sqlite"
    seed_database(database)
    return database, tmp_path / "uploads"


def test_multiple_pdfs_are_page_linked_and_scoped(storage):
    database, root = storage
    pdf = text_pdf("Nebula assignment requirements", "Submit orbit simulation")
    second = text_pdf("Rubric: simulation accuracy")
    result = ingest_documents(database, root, "owner-a", [("assignment.pdf", pdf), ("rubric.pdf", second)])
    assignment = result["assignment"]
    assert assignment["status"] == "awaiting_analysis"
    assert assignment["due_date"] is assignment["due_at"] is None
    assert assignment["requirements"] == assignment["criteria"] == assignment["facts"] == []
    assert [document["page_count"] for document in assignment["documents"]] == [2, 1]
    assert all("stored_name" not in document for document in assignment["documents"])
    assert {source["page"] for source in assignment["sources"]} == {1, 2}
    assert all(source["document_url"].endswith(".pdf") for source in assignment["sources"])
    owner = Store(database, workspace_id="owner-a")
    matches = owner.search_materials("orbit", assignment_id=assignment["assignment_id"])["matches"]
    assert len(matches) == 1 and matches[0]["page"] == 2
    assert "orbit" in matches[0]["excerpt"]
    for stranger in (Store(database), Store(database, workspace_id="owner-b")):
        assert assignment["assignment_id"] not in {item["assignment_id"] for item in stranger.list_assignments()["assignments"]}
        with pytest.raises(CatalogError, match="не найдено"):
            stranger.get_assignment(assignment["assignment_id"])
        assert stranger.search_materials("orbit")["matches"] == []
        assert stranger.get_source(matches[0]["source_id"]) is None
        assert stranger.get_document(assignment["documents"][0]["document_id"]) is None
        assert stranger.get_assignment("personal-project")["assignment"]["facts"]


def test_restart_retains_files_metadata_and_full_extracted_pages(storage):
    database, root = storage
    data = text_pdf("Retain everything after restart")
    assignment = ingest_documents(database, root, "owner-a", [("notes.pdf", data)])["assignment"]
    document = assignment["documents"][0]
    before = Store(database, "owner-a").get_document(document["document_id"])
    seed_database(database)
    restored = Store(database, "owner-a").get_assignment(assignment["assignment_id"])["assignment"]
    assert restored == assignment
    assert (root / before["stored_name"]).read_bytes() == data
    assert document["sha256"] == hashlib.sha256(data).hexdigest()
    with sqlite3.connect(database) as connection:
        pages = json.loads(connection.execute("SELECT pages_json FROM documents WHERE document_id=?",
                                             (document["document_id"],)).fetchone()[0])
    assert pages == ["Retain everything after restart"]


@pytest.mark.parametrize("files,code", [
    ([], "invalid_file_count"),
    ([("n.pdf", b"%PDF-")] * 4, "invalid_file_count"),
    ([("notes.txt", b"%PDF-")], "invalid_file_type"),
    ([("fake.pdf", b"not a PDF")], "invalid_pdf"),
    ([("broken.pdf", b"%PDF-1.7\ntruncated")], "invalid_pdf"),
])
def test_bad_file_sets_do_not_create_records_or_files(storage, files, code):
    database, root = storage
    with pytest.raises(UploadError) as error:
        ingest_documents(database, root, "owner-a", files)
    assert error.value.code == code
    assert len(Store(database, "owner-a").list_assignments()["assignments"]) == 1
    assert not root.exists() or not list(root.iterdir())


def test_second_invalid_pdf_rolls_back_the_entire_upload(storage):
    database, root = storage
    with pytest.raises(UploadError):
        ingest_documents(database, root, "owner-a", [("valid.pdf", text_pdf("Valid text")),
                                                    ("bad.pdf", b"%PDF-1.7\ntruncated")])
    assert len(Store(database, "owner-a").list_assignments()["assignments"]) == 1
    assert not root.exists() or not list(root.iterdir())


def test_scans_and_password_protected_pdfs_are_explicit_errors():
    blank = PdfWriter()
    blank.add_blank_page(width=612, height=792)
    buffer = BytesIO()
    blank.write(buffer)
    with pytest.raises(UploadError) as error:
        extract_documents([("scan.pdf", buffer.getvalue())])
    assert error.value.code == "no_text_layer"
    encrypted = PdfWriter()
    encrypted.append(BytesIO(text_pdf("Private requirements")))
    encrypted.encrypt("secret")
    output = BytesIO()
    encrypted.write(output)
    with pytest.raises(UploadError) as error:
        extract_documents([("locked.pdf", output.getvalue())])
    assert error.value.code == "encrypted_pdf"


def test_upload_limits_include_combined_bytes_text_and_page_count():
    with pytest.raises(UploadError) as error:
        extract_documents([("large.pdf", b"%PDF-" + b"x" * MAX_UPLOAD_BYTES)])
    assert error.value.code == "upload_too_large" and error.value.status_code == 413
    with pytest.raises(UploadError) as error:
        extract_documents([("long.pdf", text_pdf("a" * 8_100)), ("longer.pdf", text_pdf("b" * 8_100))])
    assert error.value.code == "text_too_large"
    with pytest.raises(UploadError) as error:
        extract_documents([("pages.pdf", text_pdf(*(["text"] * 101)))])
    assert error.value.code == "too_many_pages"


def test_complex_content_stream_is_rejected_before_text_processing():
    with pytest.raises(UploadError) as error:
        extract_documents([("complex.pdf", text_pdf("a" * MAX_PAGE_STREAM_BYTES))])
    assert error.value.code == "pdf_too_complex"


def test_compressed_stream_cannot_expand_beyond_the_parser_limit():
    writer = PdfWriter()
    writer.append(BytesIO(text_pdf("a" * MAX_PAGE_STREAM_BYTES)))
    writer.pages[0].compress_content_streams()
    output = BytesIO()
    writer.write(output)
    assert len(output.getvalue()) < 10_000
    with pytest.raises(UploadError) as error:
        extract_documents([("compressed.pdf", output.getvalue())])
    assert error.value.code == "pdf_too_complex"


def test_chunking_keeps_page_content_and_bounded_excerpts(storage):
    database, root = storage
    content = " ".join(f"token{i}" for i in range(450))
    assignment = ingest_documents(database, root, "owner-a", [("long.pdf", text_pdf(content))])["assignment"]
    sources = assignment["sources"]
    assert len(sources) > 1
    assert all(len(source["excerpt"]) <= 1_200 and source["page"] == 1 for source in sources)
    assert " ".join(source["excerpt"] for source in sources) == content


def test_failed_index_write_cleans_files_and_transaction(storage):
    database, root = storage
    with sqlite3.connect(database) as connection:
        connection.execute("""CREATE TRIGGER fail_upload BEFORE INSERT ON sources
            WHEN NEW.document_id IS NOT NULL BEGIN SELECT RAISE(ABORT, 'controlled index failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        ingest_documents(database, root, "owner-a", [("notes.pdf", text_pdf("Atomic import"))])
    assert len(Store(database, "owner-a").list_assignments()["assignments"]) == 1
    assert not list(root.iterdir())
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM documents").fetchone()[0] == 0


def test_delete_owns_only_private_records_and_removes_originals(storage):
    database, root = storage
    assignment = ingest_documents(database, root, "owner-a", [("../../outside.pdf", text_pdf("Delete this upload"))])["assignment"]
    document = assignment["documents"][0]
    assert document["filename"] == "outside.pdf"
    with pytest.raises(CatalogError):
        Store(database, "owner-b").delete_assignment(assignment["assignment_id"], root)
    assert len(list(root.iterdir())) == 1
    assert Store(database, "owner-a").delete_assignment(assignment["assignment_id"], root) == {"deleted": assignment["assignment_id"]}
    assert not list(root.iterdir())
    assert Store(database, "owner-a").search_materials("Delete")["matches"] == []
    assert Store(database, "owner-a").get_document(document["document_id"]) is None
    with pytest.raises(CatalogError) as error:
        Store(database, "owner-a").delete_assignment("personal-project", root)
    assert error.value.code == "read_only_assignment"


def test_extract_child_is_read_only_and_parent_checks_its_result(storage):
    database, root = storage
    data = text_pdf("Readonly extraction child")
    body = {"files": [{"filename": "notes.pdf", "data": base64.b64encode(data).decode()}]}
    child = subprocess.run([sys.executable, "-m", "studynavigator.ingestion", "--extract"],
                           input=json.dumps(body), capture_output=True, text=True, check=True)
    extracted = json.loads(child.stdout)["documents"]
    assert extracted[0]["pages"] == ["Readonly extraction child"]
    assert not root.exists()
    assert len(Store(database, "owner-a").list_assignments()["assignments"]) == 1
    assignment = ingest_documents(database, root, "owner-a", [("notes.pdf", data)],
                                  extracted_documents=extracted)["assignment"]
    assert assignment["status"] == "awaiting_analysis"
    extracted[0]["sha256"] = "fake"
    with pytest.raises(UploadError) as error:
        ingest_documents(database, root, "owner-a", [("notes.pdf", data)], extracted_documents=extracted)
    assert error.value.code == "invalid_extraction"


def test_actual_project_pdf_extracts_without_invented_requirements():
    extracted = extract_documents([("project.pdf", PDF_PATH.read_bytes())])[0]
    assert extracted["page_count"] == 3
    assert extracted["character_count"] > 1_000
    assert "MCP" in " ".join(extracted["pages"])
    assert "Рубежный контроль" in extracted["pages"][0]
    assert "Критерии оценивания" in extracted["pages"][1]
    assert all("\n" not in page and "  " not in page for page in extracted["pages"])
