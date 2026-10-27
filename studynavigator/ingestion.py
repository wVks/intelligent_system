"""Bounded PDF extraction and atomic persistence for one browser workspace.

The --extract child command only parses bytes. SQLite and files are written by
the parent after successful extraction, so cancelling a parser cannot half-import.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sqlite3
import sys
import uuid
from contextlib import closing
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

from pypdf import PdfReader, apply_configuration
from pypdf.errors import LimitReachedError
from pypdf.generic import ArrayObject

from .catalog import CatalogError, Store, _normalize

MAX_FILES = 3
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARACTERS = 16_000
MAX_PAGES = 100
MAX_PAGE_STREAM_BYTES = 4 * 1024 * 1024
CHUNK_CHARACTERS = 1_200


class UploadError(CatalogError):
    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(code, message)
        self.status_code = status_code


def _filename(value: str) -> str:
    # Original names are labels only; storage always uses a generated UUID.
    label = str(value).replace("\\", "/").split("/")[-1]
    label = "".join(char for char in label if char >= " " and char != "\x7f").strip()
    if not label or len(label) > 200 or not label.lower().endswith(".pdf"):
        raise UploadError("invalid_file_type", "Загрузите PDF-файлы с расширением .pdf.")
    return label


def _validate_files(files: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    if not 1 <= len(files) <= MAX_FILES:
        raise UploadError("invalid_file_count", "Для одного задания выберите от 1 до 3 PDF-файлов.")
    if sum(len(data) for _, data in files) > MAX_UPLOAD_BYTES:
        raise UploadError("upload_too_large", "Общий размер PDF превышает 10 MiB.", 413)
    validated = []
    for name, data in files:
        label = _filename(name)
        if not isinstance(data, bytes) or not data.startswith(b"%PDF-"):
            raise UploadError("invalid_pdf", f"«{label}» не является корректным PDF.")
        validated.append((label, data))
    return validated


def _check_page_stream(page: Any) -> None:
    """Reject large content streams before text layout work.

    pypdf decompresses streams in get_data; the parser runs in a killable child
    with a request deadline. Both stored and decoded sizes are bounded here.
    """
    contents = page.get("/Contents")
    if contents is None:
        return
    contents = contents.get_object()
    streams = contents if isinstance(contents, ArrayObject) else [contents]
    total_raw = total_decoded = 0
    for reference in streams:
        stream = reference.get_object()
        if not hasattr(stream, "get_data"):
            raise UploadError("invalid_pdf", "Не удалось прочитать содержимое страницы PDF.")
        total_raw += len(getattr(stream, "_data", b""))
        if total_raw > MAX_PAGE_STREAM_BYTES:
            raise UploadError("pdf_too_complex", "Одна из страниц PDF слишком сложная для MVP. Сократите файл.")
        total_decoded += len(stream.get_data())
        if total_decoded > MAX_PAGE_STREAM_BYTES:
            raise UploadError("pdf_too_complex", "Одна из страниц PDF слишком сложная для MVP. Сократите файл.")


def extract_documents(files: list[tuple[str, bytes]]) -> list[dict[str, Any]]:
    """Validate every file before any disk/DB writes; preserve page numbering."""
    with apply_configuration(
        maximum_declared_stream_length=MAX_UPLOAD_BYTES,
        array_based_stream_maximum_output_length=MAX_PAGE_STREAM_BYTES,
        zlib_maximum_output_length=MAX_PAGE_STREAM_BYTES,
        lzw_maximum_output_length=MAX_PAGE_STREAM_BYTES,
        run_length_maximum_output_length=MAX_PAGE_STREAM_BYTES,
        brotli_maximum_output_length=MAX_PAGE_STREAM_BYTES,
    ):
        return _extract_documents(files)


def _extract_documents(files: list[tuple[str, bytes]]) -> list[dict[str, Any]]:
    validated = _validate_files(files)
    documents: list[dict[str, Any]] = []
    total_characters = total_pages = 0
    for filename, data in validated:
        try:
            reader = PdfReader(BytesIO(data), strict=True)
            if reader.is_encrypted:
                raise UploadError("encrypted_pdf", f"«{filename}» защищён паролем. Загрузите незашифрованный PDF.")
            page_count = len(reader.pages)
            total_pages += page_count
            if not page_count or total_pages > MAX_PAGES:
                raise UploadError("too_many_pages", "В комплекте допускается от 1 до 100 страниц PDF.")
            pages = []
            for page in reader.pages:
                _check_page_stream(page)
                text = (page.extract_text() or "").replace("\x00", "")
                # PDF positioning can put every word on its own artificial line.
                # Keep page boundaries, but show a readable text flow within them.
                text = re.sub(r"\s+", " ", text).strip()
                total_characters += len(text)
                if total_characters > MAX_TEXT_CHARACTERS:
                    raise UploadError("text_too_large", "В материалах больше 16 000 символов текста. Загрузите только страницы с заданием.")
                pages.append(text)
            if not any(page.strip() for page in pages):
                raise UploadError("no_text_layer", f"В «{filename}» нет извлекаемого текста. Нужен текстовый PDF; сканы пока не поддерживаются.")
            documents.append({"filename": filename, "sha256": hashlib.sha256(data).hexdigest(),
                              "page_count": page_count, "character_count": sum(map(len, pages)),
                              "byte_size": len(data), "pages": pages})
        except UploadError:
            raise
        except LimitReachedError as exc:
            raise UploadError("pdf_too_complex", "Одна из страниц PDF слишком сложная для MVP. Сократите файл.") from exc
        except Exception as exc:
            raise UploadError("invalid_pdf", f"Не удалось прочитать «{filename}». Проверьте файл и попробуйте снова.") from exc
    return documents


def _chunks(text: str):
    """Split inside one page without dropping any non-whitespace text."""
    while text:
        end = min(len(text), CHUNK_CHARACTERS)
        if end < len(text):
            boundary = max(text.rfind("\n", 0, end), text.rfind(" ", 0, end))
            if boundary >= CHUNK_CHARACTERS // 2:
                end = boundary
        excerpt = text[:end].strip()
        if excerpt:
            yield excerpt
        text = text[end:].lstrip()


def ingest_documents(db_path: Path, upload_root: Path, workspace_id: str,
                     files: list[tuple[str, bytes]], title: str = "", *,
                     extracted_documents: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Persist an already validated set, or extract synchronously for local tests."""
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise UploadError("invalid_workspace", "Не удалось определить сессию. Обновите страницу.", 400)
    validated = _validate_files(files)
    title = title.strip()
    if len(title) > 200:
        raise UploadError("invalid_title", "Название задания должно быть не длиннее 200 символов.")
    documents = extract_documents(validated) if extracted_documents is None else extracted_documents
    if len(documents) != len(validated):
        raise UploadError("invalid_extraction", "Не удалось подтвердить обработку PDF. Повторите загрузку.")
    total_characters = total_pages = 0
    for document, (filename, data) in zip(documents, validated):
        pages = document.get("pages")
        if (document.get("filename") != filename or document.get("sha256") != hashlib.sha256(data).hexdigest()
                or document.get("byte_size") != len(data) or not isinstance(pages, list)
                or not pages or any(not isinstance(page, str) for page in pages)
                or not any(page.strip() for page in pages)
                or document.get("page_count") != len(pages)
                or document.get("character_count") != sum(map(len, pages))):
            raise UploadError("invalid_extraction", "Не удалось подтвердить обработку PDF. Повторите загрузку.")
        total_characters += sum(map(len, pages))
        total_pages += len(pages)
    if total_characters > MAX_TEXT_CHARACTERS or total_pages > MAX_PAGES:
        raise UploadError("invalid_extraction", "Материалы превышают ограничения обработки.")

    assignment_id = "upload-" + uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    title = title or Path(validated[0][0]).stem
    root = Path(upload_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    try:
        with closing(sqlite3.connect(db_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            with connection:
                connection.execute("INSERT OR IGNORE INTO courses VALUES ('uploaded-materials', 'Мои задания', 0)")
                connection.execute("""INSERT INTO assignments
                    (assignment_id,course_id,title,description,due_date,due_at,demo,readme_sections,facts,workspace_id,status,created_at)
                    VALUES (?, 'uploaded-materials', ?, ?, NULL, NULL, 0, '[]', '[]', ?, 'awaiting_analysis', ?)""",
                    (assignment_id, title, "Материалы сохранены. Проверьте и подтвердите карточку задания.",
                     workspace_id, created_at))
                for document, (_, data) in zip(documents, validated):
                    document_id = uuid.uuid4().hex
                    stored_name = document_id + ".pdf"
                    path = root / stored_name
                    # Exclusive creation ensures cleanup never touches another import.
                    with path.open("xb") as stream:
                        written.append(path)
                        stream.write(data)
                    connection.execute("""INSERT INTO documents
                        (document_id,assignment_id,filename,stored_name,sha256,page_count,character_count,byte_size,pages_json,created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (document_id, assignment_id, document["filename"], stored_name, document["sha256"],
                         document["page_count"], document["character_count"], document["byte_size"],
                         json.dumps(document["pages"], ensure_ascii=False), created_at))
                    for page_number, page in enumerate(document["pages"], 1):
                        for excerpt in _chunks(page):
                            source_id = "src-" + uuid.uuid4().hex
                            section = f"Страница {page_number}"
                            url = f"/sources/{source_id}"
                            connection.execute("""INSERT INTO sources
                                (source_id,assignment_id,title,page,section,excerpt,url,document_id)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                                (source_id, assignment_id, document["filename"], page_number, section,
                                 excerpt, url, document_id))
                            connection.execute("INSERT INTO material_fts VALUES (?, ?, ?)",
                                (source_id, assignment_id, _normalize(" ".join((document["filename"], section, excerpt)))))
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return Store(db_path, workspace_id=workspace_id).get_assignment(assignment_id)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extract", action="store_true", required=True)
    parser.parse_args()
    try:
        # base64 expands the parent's bounded 10 MiB request by 4/3.
        raw = sys.stdin.buffer.read(MAX_UPLOAD_BYTES * 2 + 1)
        if len(raw) > MAX_UPLOAD_BYTES * 2:
            raise UploadError("upload_too_large", "Общий размер PDF превышает 10 MiB.", 413)
        body = json.loads(raw)
        files = [(record["filename"], base64.b64decode(record["data"], validate=True))
                 for record in body["files"]]
        result = {"documents": extract_documents(files)}
    except UploadError as exc:
        result = {"error": {"code": exc.code, "message": exc.message, "status_code": exc.status_code}}
    except Exception:
        result = {"error": {"code": "invalid_pdf", "message": "Не удалось прочитать PDF. Проверьте файлы.", "status_code": 422}}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
