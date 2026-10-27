"""Local web service with actual MCP requests and an explicitly separate demo mode."""

import asyncio
import base64
import html
import json
import logging
import sys
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date
from typing import Annotated

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StringConstraints
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .agent import AgentError, HistoryMessage, StudyAgent
from .catalog import CatalogError, PDF_PATH, PROJECT_ROOT, Store, seed_database
from .cards import Card, CardError, analyze_assignment, confirm_card
from .config import Settings
from .ingestion import MAX_UPLOAD_BYTES, UploadError, ingest_documents
from .live_budget import BudgetTransport
from .mcp_gateway import GatewayError, MCPGateway
from .planner import build_plan, prepare_task_overrides, tasks_from_card
from .providers import ChatCompletionsProvider, OfflineProvider, ProviderError
from .sessions import BrowserSessionMiddleware

logger = logging.getLogger("studynavigator")
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")]


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    assignment_id: Identifier = "personal-project"
    history: Annotated[list[HistoryMessage], Field(max_length=6)] = Field(default_factory=list)


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    minutes: Annotated[StrictInt, Field(ge=1, le=4800)]
    estimate_source: str = "user"
    id: Identifier | None = None
    source_ids: Annotated[list[Identifier], Field(max_length=5)] = Field(default_factory=list)
    completed: StrictBool = False


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assignment_id: Identifier = "personal-project"
    days: Annotated[StrictInt, Field(ge=1, le=30)]
    minutes_per_day: Annotated[StrictInt, Field(ge=15, le=480)]
    tasks: Annotated[list[TaskInput], Field(min_length=1, max_length=20)] | None = None
    start_date: date | None = None
    card_revision: Annotated[StrictInt, Field(ge=0)] | None = None


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card: Card
    revision: Annotated[StrictInt, Field(ge=0)]


class RateLimiter:
    def __init__(self, limit):
        self.limit = limit
        self.requests = defaultdict(deque)

    def allow(self, key):
        now = time.monotonic()
        entries = self.requests[key]
        while entries and entries[0] <= now - 60:
            entries.popleft()
        if len(entries) >= self.limit:
            return False
        entries.append(now)
        # Small local service: remove inactive client buckets rather than retain them forever.
        if len(self.requests) > 1000:
            for old_key in list(self.requests):
                if not self.requests[old_key] or self.requests[old_key][-1] <= now - 60:
                    del self.requests[old_key]
        return True


class BodyLimitMiddleware:
    """Separate bounded multipart uploads from the existing 16 KiB JSON limit."""

    def __init__(self, app, limit=16_384):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)
        is_upload = scope.get("path") == "/api/assignments"
        is_confirmation = scope.get("path", "").startswith("/api/assignments/") and scope["path"].endswith("/confirm")
        limit = (MAX_UPLOAD_BYTES + 65_536 if is_upload else 98_304 if is_confirmation
                 else 32_768 if scope.get("path") == "/api/plan" else self.limit)
        body = bytearray()
        try:
            async with asyncio.timeout(30 if is_upload else 10):
                while True:
                    event = await receive()
                    if event["type"] == "http.disconnect":
                        return
                    chunk = event.get("body", b"")
                    if len(body) + len(chunk) > limit:
                        message = "Общий размер PDF превышает 10 MiB." if is_upload else f"Запрос превышает {limit // 1024} KiB."
                        return await error_response("body_too_large", message, 413)(scope, receive, send)
                    body.extend(chunk)
                    if not event.get("more_body", False):
                        break
        except TimeoutError:
            return await error_response("body_timeout", "Истекло время передачи запроса.", 408)(scope, receive, send)
        delivered = False

        async def buffered_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, buffered_receive, send)


def error_response(code, message, status_code, request_id=None):
    return JSONResponse(status_code=status_code, content={
        "error": {"code": code, "message": message},
        "request_id": request_id or uuid.uuid4().hex[:12],
    })


async def connected_request(request, operation, timeout):
    """Cancel work on disconnect; MCP context stays in its own owning task."""
    async def disconnected():
        while not await request.is_disconnected():
            await asyncio.sleep(0.2)

    work = asyncio.create_task(operation)
    watcher = asyncio.create_task(disconnected())
    try:
        async with asyncio.timeout(timeout):
            completed, _ = await asyncio.wait({work, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if work in completed:
                return await work
            raise AgentError("client_disconnected", "Запрос отменён пользователем.")
    except TimeoutError as exc:
        raise AgentError("request_timeout", "Истекло время обработки запроса.") from exc
    finally:
        for task in (work, watcher):
            if not task.done():
                task.cancel()
        await asyncio.gather(work, watcher, return_exceptions=True)


async def extract_uploaded_documents(files, timeout):
    """Bound PDF parsing in a disposable child; only the parent commits data."""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "studynavigator.ingestion", "--extract",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, cwd=PROJECT_ROOT,
    )
    payload = json.dumps({"files": [{"filename": name, "data": base64.b64encode(data).decode("ascii")}
                                    for name, data in files]}).encode()
    try:
        async with asyncio.timeout(timeout):
            output, _ = await process.communicate(payload)
        result = json.loads(output)
        if "error" in result:
            error = result["error"]
            raise UploadError(error["code"], error["message"], error["status_code"])
        if process.returncode or not isinstance(result.get("documents"), list):
            raise ValueError("invalid extraction result")
        return result["documents"]
    except TimeoutError as exc:
        raise UploadError("upload_timeout", "Обработка PDF заняла слишком много времени. Сократите файл.", 408) from exc
    except (ValueError, KeyError) as exc:
        raise UploadError("invalid_pdf", "Не удалось обработать PDF. Проверьте файл и попробуйте снова.") from exc
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


def create_app(settings=None, provider=None, analysis_provider=None):
    settings = settings or Settings.from_env()
    provider = provider or (OfflineProvider() if settings.mode == "offline" else ChatCompletionsProvider(settings))
    if analysis_provider is None:
        analysis_provider = (ChatCompletionsProvider(replace(settings, model=settings.analysis_model))
                             if settings.mode == "live" and settings.analysis_model
                             and settings.analysis_model != settings.model else provider)
    live_budget = None
    if settings.mode == "live" and settings.live_budget_rub is not None:
        live_providers = list({id(item): item for item in (provider, analysis_provider)
                               if isinstance(item, ChatCompletionsProvider)}.values())
        transports = {id(item.transport): item.transport for item in live_providers if item.transport is not None}
        if len(transports) > 1:
            raise ValueError("Budgeted live providers must share one HTTP transport")
        existing = next(iter(transports.values()), None)
        if isinstance(existing, BudgetTransport):
            if (existing.ledger_path.resolve() != settings.live_budget_path.resolve()
                    or existing.summary()["budget_rub"] != settings.live_budget_rub):
                raise ValueError("Injected budget transport differs from server budget settings")
            live_budget = existing
        elif live_providers:
            live_budget = BudgetTransport(settings.live_budget_path, budget_rub=settings.live_budget_rub,
                                          inner=existing,
                                          tokenize_payload=any("/yandexgpt-5.1" in item.model
                                                               for item in live_providers))
        for item in live_providers:
            item.transport = live_budget
    limiter = RateLimiter(settings.rate_limit_per_minute)
    capacity = asyncio.Semaphore(settings.max_concurrent_requests)

    @asynccontextmanager
    async def lifespan(app):
        try:
            seed_database(settings.database_path)
            async with MCPGateway(settings.database_path, timeout=settings.tool_timeout) as gateway:
                app.state.mcp_tools = len(gateway.tools)
            yield
        finally:
            if live_budget is not None:
                await live_budget.close()

    app = FastAPI(title="StudyNavigator", version=__version__, lifespan=lifespan,
                  docs_url="/api/docs", redoc_url=None)
    app.state.settings = settings
    app.state.provider = provider
    app.state.analysis_provider = analysis_provider
    app.state.live_budget = live_budget
    app.state.mcp_tools = 0
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(BrowserSessionMiddleware)
    app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "web"), name="static")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return error_response("invalid_request", "Проверьте поля запроса и допустимые значения.", 422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        if request.method == "POST" and request.url.path == "/api/assignments" and exc.status_code == 400:
            return error_response("invalid_upload", "Проверьте форму: от 1 до 3 PDF-файлов и короткое название задания.", 400)
        return error_response("not_found" if exc.status_code == 404 else "request_error", str(exc.detail), exc.status_code)

    @app.exception_handler(GatewayError)
    async def gateway_error(request, exc):
        status = 404 if exc.code == "unknown_assignment" else 503
        return error_response(exc.code, exc.message, status)

    @app.exception_handler(CatalogError)
    async def catalogue_error(request, exc):
        status = getattr(exc, "status_code", 404 if exc.code == "unknown_assignment" else 422)
        return error_response(exc.code, exc.message, status)

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc):
        status = 429 if exc.code == "provider_rate_limit" else 504 if "timeout" in exc.code else 503
        return error_response(exc.code, exc.message, status)

    def store(request):
        return Store(settings.database_path, workspace_id=request.state.workspace_id)

    def gateway(request):
        return MCPGateway(settings.database_path, timeout=settings.tool_timeout,
                          workspace_id=request.state.workspace_id)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse((PROJECT_ROOT / "web/index.html").read_text(encoding="utf-8"))

    @app.get("/health")
    async def health():
        return {"status": "ok", "mode": provider.mode, "version": __version__}

    @app.get("/api/status")
    async def status():
        return {"mode": provider.mode, "live_configured": settings.live_configured,
                "model": provider.model, "analysis_model": analysis_provider.model,
                "mcp_tools": app.state.mcp_tools,
                "transport": "stdio", "version": __version__}

    @app.get("/api/assignments")
    async def assignments(request: Request):
        async with gateway(request) as client:
            return await client.call("list_assignments", {})

    @app.get("/api/assignments/{assignment_id}")
    async def assignment(assignment_id: Identifier, request: Request):
        async with gateway(request) as client:
            return await client.call("get_assignment", {"assignment_id": assignment_id})

    @app.post("/api/assignments", status_code=201)
    async def upload_assignment(request: Request):
        if not check_rate(request):
            return error_response("rate_limit", "Слишком много запросов. Подождите минуту.", 429)
        if capacity.locked():
            return error_response("busy", "Сервис занят. Повторите загрузку чуть позже.", 429)
        if not request.headers.get("content-type", "").startswith("multipart/form-data"):
            return error_response("invalid_upload", "Выберите PDF-файлы в форме загрузки.", 422)
        async with request.form(max_files=3, max_fields=1, max_part_size=1024) as form:
            if any(name not in {"files", "title"} for name, _ in form.multi_items()):
                return error_response("invalid_upload", "Проверьте поля загрузки.", 422)
            parts = form.getlist("files")
            title = form.get("title", "")
            if not 1 <= len(parts) <= 3 or not all(isinstance(part, UploadFile) for part in parts) or not isinstance(title, str):
                return error_response("invalid_upload", "Для задания выберите от 1 до 3 PDF-файлов.", 422)
            files, total = [], 0
            for part in parts:
                content = bytearray()
                while chunk := await part.read(65_536):
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        return error_response("upload_too_large", "Общий размер PDF превышает 10 MiB.", 413)
                    content.extend(chunk)
                files.append((part.filename or "", bytes(content)))

            async def operation():
                extracted = await extract_uploaded_documents(files, settings.upload_timeout)
                return ingest_documents(settings.database_path, settings.uploads_path,
                                        request.state.workspace_id, files, title,
                                        extracted_documents=extracted)

            async with capacity:
                try:
                    return await connected_request(request, operation(), settings.upload_timeout + 5)
                except AgentError as exc:
                    return error_response(exc.code, exc.message, 499 if exc.code == "client_disconnected" else 408)
                except UploadError:
                    raise
                except Exception as exc:
                    logger.error("upload_failed kind=%s", type(exc).__name__)
                    return error_response("upload_failed", "Не удалось сохранить материалы. Попробуйте загрузить их снова.", 500)

    @app.get("/api/assignments/{assignment_id}/materials")
    async def assignment_materials(assignment_id: Identifier, request: Request,
                                   q: Annotated[str, StringConstraints(max_length=500)] = ""):
        selected = store(request).get_assignment(assignment_id)["assignment"]
        if not q.strip():
            return {"matches": selected["sources"]}
        async with gateway(request) as client:
            return await client.call("search_materials", {"query": q, "assignment_id": assignment_id, "limit": 10})

    @app.post("/api/assignments/{assignment_id}/analyze")
    async def analyze(assignment_id: Identifier, body: AnalyzeRequest, request: Request):
        selected = store(request).get_assignment(assignment_id)["assignment"]
        if not selected.get("is_uploaded"):
            raise CardError("read_only_assignment", "Подготовленный пример уже разобран. Загрузите своё задание.", 403)
        if not check_rate(request):
            return error_response("rate_limit", "Слишком много запросов. Подождите минуту.", 429)
        if capacity.locked():
            return error_response("busy", "Сервис занят. Повторите разбор чуть позже.", 429)

        async def operation():
            async with gateway(request) as client:
                result = await analyze_assignment(client, analysis_provider, assignment_id)
            saved = store(request).save_card(assignment_id, result["card"],
                                             expected_revision=selected["card_revision"])
            return {**result, **saved, "revision": saved["assignment"]["card_revision"]}

        try:
            async with capacity:
                return await connected_request(request, operation(), settings.request_timeout)
        except AgentError as exc:
            return error_response(exc.code, exc.message, 499 if exc.code == "client_disconnected" else 504)
        except (CardError, ProviderError, GatewayError):
            raise
        except Exception as exc:
            logger.error("analysis_failed kind=%s", type(exc).__name__)
            return error_response("analysis_failed", "Не удалось разобрать задание. Карточка не изменена; повторите попытку.", 500)

    @app.post("/api/assignments/{assignment_id}/confirm")
    async def confirm(assignment_id: Identifier, body: ConfirmRequest, request: Request):
        selected = store(request).get_assignment(assignment_id)["assignment"]
        if not check_rate(request):
            return error_response("rate_limit", "Слишком много запросов. Подождите минуту.", 429)
        if not selected.get("is_uploaded"):
            raise CardError("read_only_assignment", "Подготовленный пример доступен только для чтения.", 403)
        normalized = confirm_card(body.card, selected.get("card"), selected["sources"])
        if not normalized["tasks"]:
            normalized["tasks"] = [{key: value for key, value in task.items() if key != "completed"}
                                   for task in tasks_from_card(normalized)]
        saved = store(request).save_card(assignment_id, normalized, confirmed=True,
                                         expected_revision=body.revision)
        return {**saved, "revision": saved["assignment"]["card_revision"]}

    @app.delete("/api/assignments/{assignment_id}", status_code=204)
    async def delete_assignment(assignment_id: Identifier, request: Request):
        store(request).delete_assignment(assignment_id, settings.uploads_path)
        return Response(status_code=204)

    @app.get("/api/sources/{source_id}")
    async def source_json(source_id: Identifier, request: Request):
        source = store(request).get_source(source_id)
        if source is None:
            raise HTTPException(404, "Источник не найден.")
        return source

    @app.get("/sources/{source_id}", response_class=HTMLResponse)
    async def source_page(source_id: Identifier, request: Request):
        from .text_format import render_structured_text

        source = store(request).get_source(source_id)
        if source is None:
            raise HTTPException(404, "Источник не найден.")
        title, section = html.escape(source["title"]), html.escape(source.get("section") or "Фрагмент")
        excerpt = render_structured_text(source["excerpt"])
        page = int(source["page"])
        document_url = html.escape(source.get("document_url") or "/materials/personal-project.pdf", quote=True)
        body = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — источник StudyNavigator</title><link rel="stylesheet" href="/static/styles.css?v=20261027-ui3"></head>
<body><main id="source-reader" class="source-reader">
<header class="source-reader-header"><a class="source-back" href="/">← К ассистенту</a>
<p class="source-meta">Исходный материал · страница {page}</p><h1>{title}</h1>
<p class="source-section">{section}</p></header>
<article class="source-content" aria-label="Текст источника"><div class="structured-text">{excerpt}</div></article>
<footer class="source-reader-footer"><a class="source-pdf-link" href="{document_url}#page={page}" target="_blank" rel="noopener">Открыть страницу {page} исходного PDF ↗</a>
<p>Сверяйте извлечённый текст с оригиналом; интерпретация ассистента не заменяет его требования.</p></footer>
</main></body></html>"""
        return HTMLResponse(body)

    @app.get("/materials/personal-project.pdf")
    async def material_pdf():
        return FileResponse(PDF_PATH, media_type="application/pdf", filename="Личный проект.pdf", content_disposition_type="inline")

    @app.get("/materials/{document_id}.pdf")
    async def uploaded_pdf(document_id: Identifier, request: Request):
        document = store(request).get_document(document_id)
        if document is None:
            raise HTTPException(404, "Документ не найден.")
        path = settings.uploads_path / document["stored_name"]
        if not path.is_file():
            raise HTTPException(404, "Документ не найден.")
        return FileResponse(path, media_type="application/pdf", filename=document["filename"], content_disposition_type="inline")

    def check_rate(request):
        key = request.client.host if request.client else "local"
        return limiter.allow(key)

    @app.post("/api/chat")
    async def chat(body: ChatRequest, request: Request):
        selected = store(request).get_assignment(body.assignment_id)["assignment"]
        if selected["status"] != "ready":
            return error_response("assignment_not_ready", "Сначала заполните и подтвердите карточку задания.", 409)
        request_id = uuid.uuid4().hex[:12]
        started = time.monotonic()
        if not check_rate(request):
            return error_response("rate_limit", "Слишком много запросов. Подождите минуту.", 429, request_id)
        if capacity.locked():
            return error_response("busy", "Сервис занят. Повторите запрос чуть позже.", 429, request_id)

        async def operation():
            async with gateway(request) as client:
                agent = StudyAgent(client, provider, max_calls=settings.max_calls,
                                   request_timeout=settings.request_timeout)
                return await agent.run(body.message, body.assignment_id,
                                       history=[item.model_dump() for item in body.history])

        try:
            async with capacity:
                result = await connected_request(request, operation(), settings.request_timeout)
            result.update(request_id=request_id, elapsed_ms=round((time.monotonic() - started) * 1000),
                          assignment_id=body.assignment_id)
            logger.info("chat_complete id=%s mode=%s status=%s tools=%d", request_id, provider.mode,
                        result["status"], len(result["trace"]))
            return result
        except (AgentError, GatewayError) as exc:
            logger.warning("chat_failed id=%s code=%s", request_id, exc.code)
            code = 404 if exc.code == "unknown_assignment" else 429 if exc.code == "provider_rate_limit" else 504 if "timeout" in exc.code else 503
            if exc.code == "client_disconnected":
                code = 499
            return error_response(exc.code, exc.message, code, request_id)
        except Exception as exc:
            logger.error("chat_internal_error id=%s kind=%s", request_id, type(exc).__name__)
            return error_response("internal_error", "Не удалось обработать запрос. Попробуйте позже.", 500, request_id)

    @app.post("/api/plan")
    async def plan(body: PlanRequest, request: Request):
        if not check_rate(request):
            return error_response("rate_limit", "Слишком много запросов. Подождите минуту.", 429)
        # Validate the selected assignment against the same catalogue used by MCP.
        try:
            selected = store(request).get_assignment(body.assignment_id)["assignment"]
            if selected["status"] != "ready":
                return error_response("assignment_not_ready", "Сначала заполните и подтвердите карточку задания.", 409)
            submitted = [task.model_dump(exclude_none=True) for task in body.tasks] if body.tasks is not None else None
            if selected.get("is_uploaded"):
                if body.card_revision != selected["card_revision"]:
                    return error_response("stale_card", "Карточка изменилась. Откройте её заново и пересчитайте план.", 409)
                tasks = prepare_task_overrides(tasks_from_card(selected["card"]), submitted)
            else:
                # One-off legacy inputs are user estimates, never document facts.
                tasks = [{**task, "estimate_source": "user", "source_ids": []} for task in submitted] if submitted is not None else None
            effective_days = body.days
            deadline = (selected.get("card") or {}).get("deadline") or {}
            if body.start_date is not None and deadline.get("date") and any(not task.get("completed", False) for task in tasks or [{}]):
                remaining_days = (date.fromisoformat(deadline["date"]) - body.start_date).days + 1
                if remaining_days < 1:
                    raise ValueError("start date is after the known deadline")
                effective_days = min(body.days, remaining_days)
            result = build_plan(days=effective_days, minutes_per_day=body.minutes_per_day,
                                tasks=tasks, start_date=body.start_date, assignment_id=body.assignment_id)
            result["card_revision"] = selected["card_revision"]
            if selected.get("card"):
                result["deadline"] = selected["card"]["deadline"]
            if effective_days != body.days:
                result["assumptions"].append(f"Горизонт ограничен известным сроком: {effective_days} дн. из выбранных {body.days}.")
            result["days"] = effective_days
            return result
        except ValueError as exc:
            if getattr(exc, "code", None) == "unknown_assignment":
                return error_response("unknown_assignment", "Учебное задание не найдено.", 404)
            return error_response("invalid_plan", "Проверьте длительности и ограничения плана.", 422)

    return app


app = create_app()
