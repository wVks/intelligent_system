"""Versioned seed and browser-scoped materials backed by SQLite/FTS5."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import closing
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = PROJECT_ROOT / "data" / "course.json"
PDF_PATH = next(PROJECT_ROOT.glob("*.pdf"))
SOURCE_FIELDS = ("source_id", "title", "page", "section", "excerpt", "url")


class CatalogError(ValueError):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def seed_database(db_path: Path) -> None:
    """Upsert only this dataset's IDs; retain private uploads on every restart."""
    dataset = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    if hashlib.sha256(PDF_PATH.read_bytes()).hexdigest() != dataset["document"]["sha256"]:
        raise CatalogError("source_changed", "Исходный PDF изменился: требуется сверка учебных данных.")
    sources = {source["source_id"]: source for source in dataset["sources"]}
    assignment = dataset["assignment"]
    if len(sources) != len(dataset["sources"]):
        raise CatalogError("invalid_seed", "Повторяющийся идентификатор источника.")
    for record in [*assignment["requirements"], *assignment["criteria"]]:
        if not record["source_ids"] or any(item not in sources for item in record["source_ids"]):
            raise CatalogError("invalid_seed", "Требование ссылается на неизвестный источник.")
    for fact in assignment["facts"]:
        if fact["source_id"] not in sources:
            raise CatalogError("invalid_seed", "Факт ссылается на неизвестный источник.")
    if any(not 1 <= source["page"] <= dataset["document"]["pages"] or
           not source["excerpt"].strip() or len(source["excerpt"]) > 1200 for source in sources.values()):
        raise CatalogError("invalid_seed", "Неверная страница или размер выдержки.")
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS courses (course_id TEXT PRIMARY KEY, title TEXT NOT NULL, demo INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS assignments (
                assignment_id TEXT PRIMARY KEY, course_id TEXT NOT NULL REFERENCES courses(course_id),
                title TEXT NOT NULL, description TEXT NOT NULL, due_date TEXT, due_at TEXT,
                demo INTEGER NOT NULL, readme_sections TEXT NOT NULL, facts TEXT NOT NULL,
                workspace_id TEXT, status TEXT NOT NULL DEFAULT 'ready', created_at TEXT,
                card_json TEXT, card_revision INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sources (
                source_id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL REFERENCES assignments(assignment_id),
                title TEXT NOT NULL, page INTEGER NOT NULL, section TEXT, excerpt TEXT NOT NULL, url TEXT NOT NULL,
                document_id TEXT
            );
            CREATE TABLE IF NOT EXISTS requirements (
                id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL REFERENCES assignments(assignment_id), body TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS criteria (
                id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL REFERENCES assignments(assignment_id), body TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS material_fts USING fts5(
                source_id UNINDEXED, assignment_id UNINDEXED, content, tokenize='unicode61'
            );
            CREATE TABLE IF NOT EXISTS documents (
                document_id TEXT PRIMARY KEY,
                assignment_id TEXT NOT NULL REFERENCES assignments(assignment_id),
                filename TEXT NOT NULL, stored_name TEXT NOT NULL UNIQUE,
                sha256 TEXT NOT NULL, page_count INTEGER NOT NULL,
                character_count INTEGER NOT NULL, byte_size INTEGER NOT NULL,
                pages_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
        """)
        # Existing local databases predate uploads. Add only the missing columns.
        existing = {row[1] for row in connection.execute("PRAGMA table_info(assignments)")}
        for name, declaration in (("workspace_id", "TEXT"), ("status", "TEXT NOT NULL DEFAULT 'ready'"),
                                  ("created_at", "TEXT"), ("card_json", "TEXT"),
                                  ("card_revision", "INTEGER NOT NULL DEFAULT 0")):
            if name not in existing:
                connection.execute(f"ALTER TABLE assignments ADD COLUMN {name} {declaration}")
        if "document_id" not in {row[1] for row in connection.execute("PRAGMA table_info(sources)")}:
            connection.execute("ALTER TABLE sources ADD COLUMN document_id TEXT")
        connection.execute("CREATE INDEX IF NOT EXISTS assignment_workspace ON assignments(workspace_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS document_assignment ON documents(assignment_id)")
        with connection:
            course = dataset["course"]
            connection.execute("INSERT INTO courses VALUES (?, ?, ?) ON CONFLICT(course_id) DO UPDATE SET title=excluded.title, demo=excluded.demo",
                               (course["course_id"], course["title"], int(course["demo"])))
            connection.execute("""INSERT INTO assignments
                (assignment_id,course_id,title,description,due_date,due_at,demo,readme_sections,facts,workspace_id,status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'ready')
                ON CONFLICT(assignment_id) DO UPDATE SET course_id=excluded.course_id,title=excluded.title,
                description=excluded.description,due_date=excluded.due_date,due_at=excluded.due_at,
                demo=excluded.demo,readme_sections=excluded.readme_sections,facts=excluded.facts,
                workspace_id=NULL,status='ready'""",
                (assignment["assignment_id"], assignment["course_id"], assignment["title"], assignment["description"],
                 assignment["due_date"], assignment["due_at"], int(assignment["demo"]),
                 json.dumps(assignment["readme_sections"], ensure_ascii=False),
                 json.dumps(assignment["facts"], ensure_ascii=False)))
            for source in sources.values():
                connection.execute("""INSERT INTO sources
                    (source_id,assignment_id,title,page,section,excerpt,url,document_id) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                    ON CONFLICT(source_id) DO UPDATE SET assignment_id=excluded.assignment_id,title=excluded.title,
                    page=excluded.page,section=excluded.section,excerpt=excluded.excerpt,url=excluded.url,
                    document_id=NULL""",
                    (source["source_id"], assignment["assignment_id"], source["title"], source["page"],
                     source["section"], source["excerpt"], source["url"]))
            for table in ("requirements", "criteria"):
                connection.execute(f"DELETE FROM {table} WHERE assignment_id=?", (assignment["assignment_id"],))
                for record in assignment[table]:
                    connection.execute(f"INSERT INTO {table} VALUES (?, ?, ?)",
                                       (record["id"], assignment["assignment_id"], json.dumps(record, ensure_ascii=False)))
            connection.execute("DELETE FROM material_fts WHERE assignment_id=?", (assignment["assignment_id"],))
            for source in sources.values():
                content = _normalize(" ".join((source["title"], source["section"] or "", source["excerpt"])))
                connection.execute("INSERT INTO material_fts VALUES (?, ?, ?)",
                                   (source["source_id"], assignment["assignment_id"], content))
            connection.execute("INSERT OR REPLACE INTO metadata VALUES ('dataset_version', ?)", (dataset["version"],))
            connection.execute("INSERT OR REPLACE INTO metadata VALUES ('document', ?)", (json.dumps(dataset["document"], ensure_ascii=False),))


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).lower().replace("ё", "е")


def _search_tokens(query: str) -> list[str]:
    tokens = re.findall(r"[^\W_]+", _normalize(query), flags=re.UNICODE)
    stop = {"что", "как", "где", "когда", "нужно", "надо", "это", "для", "или", "при", "на", "по", "за", "из", "ли", "и", "в", "с", "у", "во", "сколько"}
    result = [token for token in tokens if token not in stop and len(token) >= 2]
    normalized = " ".join(tokens)
    expansions = (
        (("лмс", "лмз", "lms", "отправ", "загруз", "сдать", "сдавать"), ("lms", "ссылку")),
        (("мсп", "mcp"), ("mcp",)),
        (("дедлайн", "срок", "дата", "когда"), ("срок", "дата", "сдачи")),
        (("балл", "оценк"), ("баллов", "оценивания")),
        (("защит", "презентац"), ("защита", "минут", "презентацию")),
        (("ридми", "readme"), ("readme",)),
    )
    for stems, additions in expansions:
        if any(stem in normalized for stem in stems):
            result.extend(additions)
    return list(dict.fromkeys(result))[:30]


class Store:
    """Read access to the public example and, optionally, one trusted workspace."""

    def __init__(self, db_path: Path, workspace_id: str | None = None):
        self.db_path = Path(db_path)
        self.workspace_id = workspace_id

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def list_assignments(self, course_id: str | None = None) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            rows = connection.execute("""SELECT * FROM assignments
                WHERE (workspace_id IS NULL OR workspace_id=?) AND (? IS NULL OR course_id=?)
                ORDER BY CASE WHEN workspace_id IS NULL THEN 1 ELSE 0 END, created_at DESC, assignment_id""",
                (self.workspace_id, course_id, course_id)).fetchall()
            assignments = []
            for row in rows:
                item = self._assignment_summary(row)
                item["source_ids"] = [source[0] for source in connection.execute(
                    "SELECT source_id FROM sources WHERE assignment_id=? ORDER BY rowid", (row["assignment_id"],))]
                item["documents"] = [self._document(doc) for doc in connection.execute(
                    "SELECT * FROM documents WHERE assignment_id=? ORDER BY rowid", (row["assignment_id"],))]
                assignments.append(item)
            return {"assignments": assignments}

    @staticmethod
    def _assignment_summary(row: sqlite3.Row) -> dict[str, Any]:
        item = {key: row[key] for key in ("assignment_id", "course_id", "title", "description", "due_date", "due_at")}
        item.update(demo=bool(row["demo"]), status=row["status"],
                    is_uploaded=row["workspace_id"] is not None, created_at=row["created_at"],
                    card_revision=row["card_revision"])
        return item

    def get_assignment(self, assignment_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute("""SELECT * FROM assignments WHERE assignment_id=?
                AND (workspace_id IS NULL OR workspace_id=?)""", (assignment_id, self.workspace_id)).fetchone()
            if row is None:
                raise CatalogError("unknown_assignment", "Учебное задание не найдено.")
            assignment = self._assignment_summary(row)
            assignment["card"] = json.loads(row["card_json"]) if row["card_json"] else None
            assignment["facts"] = json.loads(row["facts"])
            assignment["readme_sections"] = json.loads(row["readme_sections"])
            assignment["sources"] = [self._source(source) for source in connection.execute(
                "SELECT * FROM sources WHERE assignment_id=? ORDER BY rowid", (assignment_id,))]
            assignment["source_ids"] = [source["source_id"] for source in assignment["sources"]]
            assignment["documents"] = [self._document(doc) for doc in connection.execute(
                "SELECT * FROM documents WHERE assignment_id=? ORDER BY rowid", (assignment_id,))]
            for table in ("requirements", "criteria"):
                assignment[table] = [json.loads(record[0]) for record in connection.execute(
                    f"SELECT body FROM {table} WHERE assignment_id=? ORDER BY rowid", (assignment_id,))]
            return {"assignment": assignment}

    def save_card(self, assignment_id: str, card: dict[str, Any], *, confirmed=False,
                  expected_revision: int | None = None) -> dict[str, Any]:
        """Persist a server-validated card; the HTTP caller normalizes user edits first."""
        from .cards import CardError, validate_card_sources

        current = self.get_assignment(assignment_id)["assignment"]
        if not current["is_uploaded"] or self.workspace_id is None:
            raise CardError("read_only_assignment", "Подготовленный пример нельзя изменять.", 403)
        parsed = validate_card_sources(card, current["sources"], confirmed=confirmed)
        revision = current["card_revision"] if expected_revision is None else expected_revision
        if type(revision) is not int or revision < 0:
            raise CardError("invalid_revision", "Версия карточки должна быть целым неотрицательным числом.")
        if revision != current["card_revision"]:
            raise CardError("card_conflict", "Карточка уже изменилась. Обновите задание и повторите действие.", 409)
        encoded = json.dumps(parsed.model_dump(), ensure_ascii=False)
        status = "ready" if confirmed else "needs_confirmation"
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            with connection:
                result = connection.execute("""UPDATE assignments SET card_json=?,card_revision=card_revision+1,status=?
                    WHERE assignment_id=? AND workspace_id=? AND card_revision=?""",
                    (encoded, status, assignment_id, self.workspace_id, revision))
                if result.rowcount != 1:
                    raise CardError("card_conflict", "Карточка уже изменилась. Обновите задание и повторите действие.", 409)
                if confirmed:
                    connection.execute("""UPDATE assignments SET title=?,description=?,due_date=?,due_at=NULL
                        WHERE assignment_id=? AND workspace_id=?""",
                        (parsed.title, parsed.goal.text, parsed.deadline.date, assignment_id, self.workspace_id))
                    for table in ("requirements", "criteria"):
                        connection.execute(f"DELETE FROM {table} WHERE assignment_id=?", (assignment_id,))
                        for index, field in enumerate(getattr(parsed, table), 1):
                            record = {**field.model_dump(), "id": f"{assignment_id}-{table}-{index}"}
                            if table == "criteria":
                                record["title"] = field.text
                            connection.execute(f"INSERT INTO {table} VALUES (?, ?, ?)",
                                               (record["id"], assignment_id, json.dumps(record, ensure_ascii=False)))
        return self.get_assignment(assignment_id)

    @staticmethod
    def _source(row: sqlite3.Row) -> dict[str, Any]:
        source = {key: row[key] for key in SOURCE_FIELDS}
        if row["document_id"]:
            source["document_id"] = row["document_id"]
            source["document_url"] = f"/materials/{row['document_id']}.pdf"
        return source

    @staticmethod
    def _document(row: sqlite3.Row) -> dict[str, Any]:
        doc = {key: row[key] for key in ("document_id", "filename", "sha256", "page_count",
                                        "character_count", "byte_size", "created_at")}
        doc["url"] = doc["document_url"] = f"/materials/{row['document_id']}.pdf"
        return doc

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute("""SELECT s.* FROM sources s JOIN assignments a USING(assignment_id)
                WHERE s.source_id=? AND (a.workspace_id IS NULL OR a.workspace_id=?)""",
                (source_id, self.workspace_id)).fetchone()
            return self._source(row) if row else None

    def get_document(self, document_id: str) -> dict[str, Any] | None:
        """Internal FileResponse lookup. Do not expose stored_name in JSON."""
        with closing(self._connect()) as connection:
            row = connection.execute("""SELECT d.* FROM documents d JOIN assignments a USING(assignment_id)
                WHERE d.document_id=? AND (a.workspace_id IS NULL OR a.workspace_id=?)""",
                (document_id, self.workspace_id)).fetchone()
            return {**self._document(row), "stored_name": row["stored_name"],
                    "assignment_id": row["assignment_id"]} if row else None

    def search_materials(self, query: str, limit: int = 5, assignment_id: str | None = None) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise CatalogError("invalid_arguments", "Поисковый запрос должен содержать от 1 до 500 символов.")
        if type(limit) is not int or not 1 <= limit <= 10:
            raise CatalogError("invalid_arguments", "Количество результатов должно быть целым числом от 1 до 10.")
        tokens = _search_tokens(query)
        if not tokens:
            return {"matches": []}
        match_query = " OR ".join('"' + token + '"' for token in tokens)
        with closing(self._connect()) as connection:
            rows = connection.execute("""SELECT s.* FROM material_fts JOIN sources s USING(source_id)
                JOIN assignments a ON a.assignment_id=s.assignment_id
                WHERE material_fts MATCH ? AND (? IS NULL OR s.assignment_id=?)
                AND (a.workspace_id IS NULL OR a.workspace_id=?)
                ORDER BY bm25(material_fts),s.source_id LIMIT ?""",
                (match_query, assignment_id, assignment_id, self.workspace_id, limit)).fetchall()
            return {"matches": [{**self._source(row), "assignment_id": row["assignment_id"]} for row in rows]}

    def delete_assignment(self, assignment_id: str, upload_root: Path) -> dict[str, Any]:
        """Remove only this workspace's upload; the prepared example is read-only."""
        self.get_assignment(assignment_id)
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            owner = connection.execute("SELECT workspace_id FROM assignments WHERE assignment_id=?",
                                       (assignment_id,)).fetchone()
            if owner is None or owner[0] != self.workspace_id or self.workspace_id is None:
                raise CatalogError("read_only_assignment", "Подготовленный пример нельзя удалить.")
            files = [row[0] for row in connection.execute(
                "SELECT stored_name FROM documents WHERE assignment_id=?", (assignment_id,))]
            with connection:
                for table in ("material_fts", "requirements", "criteria", "sources", "documents"):
                    connection.execute(f"DELETE FROM {table} WHERE assignment_id=?", (assignment_id,))
                connection.execute("DELETE FROM assignments WHERE assignment_id=? AND workspace_id=?",
                                   (assignment_id, self.workspace_id))
        root = Path(upload_root).resolve()
        for stored_name in files:
            path = root / stored_name
            if path.parent == root and re.fullmatch(r"[a-f0-9]{32}\.pdf", stored_name):
                path.unlink(missing_ok=True)
        return {"deleted": assignment_id}
