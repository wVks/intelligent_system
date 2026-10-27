"""Assignment cards with explicit document/user provenance, independent of a provider."""

from __future__ import annotations

import re
from datetime import date as CalendarDate
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, ValidationError, field_validator, model_validator

from .catalog import CatalogError

SourceID = Annotated[str, StringConstraints(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)]
Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class CardError(CatalogError):
    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(code, message)
        self.status_code = status_code


class CardField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Text = ""
    origin: Literal["document", "user"] = "user"
    source_ids: list[SourceID] = Field(default_factory=list, max_length=5)


class CardDeadline(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: str | None = None
    origin: Literal["document", "user", "unknown"] = "unknown"
    source_ids: list[SourceID] = Field(default_factory=list, max_length=5)

    @field_validator("date")
    @classmethod
    def iso_date(cls, value):
        if value is not None:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError("date must be YYYY-MM-DD")
            CalendarDate.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def consistent_unknown(self):
        if self.date is None:
            if self.origin != "unknown" or self.source_ids:
                raise ValueError("an unknown deadline has no origin or sources")
        elif self.origin == "unknown":
            raise ValueError("a known deadline needs an origin")
        return self


class CardTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Annotated[str, StringConstraints(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")]
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    minutes: Annotated[StrictInt, Field(ge=1, le=4800)]
    estimate_source: Literal["estimate", "user", "document"] = "estimate"
    source_ids: list[SourceID] = Field(default_factory=list, max_length=5)


class Card(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    goal: CardField = Field(default_factory=CardField)
    deliverable: CardField = Field(default_factory=CardField)
    requirements: list[CardField] = Field(default_factory=list, max_length=12)
    criteria: list[CardField] = Field(default_factory=list, max_length=10)
    deadline: CardDeadline = Field(default_factory=CardDeadline)
    tasks: list[CardTask] = Field(default_factory=list, max_length=20)
    questions: list[Question] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def useful_entries(self):
        if any(not item.text for item in [*self.requirements, *self.criteria]):
            raise ValueError("requirement and criterion entries must not be empty")
        if len({task.id for task in self.tasks}) != len(self.tasks):
            raise ValueError("task IDs must be unique")
        return self


def parse_card(value: Card | dict[str, Any]) -> Card:
    try:
        return Card.model_validate(value)
    except (ValidationError, TypeError, ValueError) as exc:
        raise CardError("invalid_card", "Проверьте поля карточки, даты и оценки длительности.") from exc


def _source_set(available_sources) -> set[str]:
    return {source if isinstance(source, str) else source["source_id"] for source in available_sources}


def validate_card_sources(card: Card | dict[str, Any], available_sources, *, confirmed=False) -> Card:
    """Check structure and scoped references; semantic support is a separate Eval."""
    parsed = parse_card(card)
    allowed = _source_set(available_sources)
    for item in [parsed.goal, parsed.deliverable, *parsed.requirements, *parsed.criteria]:
        if any(source_id not in allowed for source_id in item.source_ids):
            raise CardError("unsupported_source", "Карточка ссылается на источник другого задания или неизвестный фрагмент.")
        if item.origin == "document" and (not item.text or not item.source_ids):
            raise CardError("missing_sources", "Сведение из документа должно иметь текст и ссылку на его страницу.")
        if item.origin == "user" and item.source_ids:
            raise CardError("invalid_provenance", "Пользовательское уточнение не является фактом документа.")
    deadline = parsed.deadline
    if any(source_id not in allowed for source_id in deadline.source_ids):
        raise CardError("unsupported_source", "Источник срока не относится к этому заданию.")
    if deadline.origin == "document" and not deadline.source_ids:
        raise CardError("missing_sources", "Для срока из документа нужен источник.")
    if deadline.origin == "user" and deadline.source_ids:
        raise CardError("invalid_provenance", "Пользовательский срок не должен ссылаться на документ.")
    for task in parsed.tasks:
        if any(source_id not in allowed for source_id in task.source_ids):
            raise CardError("unsupported_source", "Источник оценки времени не относится к этому заданию.")
        if task.estimate_source == "document" and not task.source_ids:
            raise CardError("missing_sources", "Оценка времени из документа должна иметь источник.")
        if task.estimate_source != "document" and task.source_ids:
            raise CardError("invalid_provenance", "Рекомендация или пользовательская оценка не является фактом документа.")
    if confirmed and (not parsed.goal.text or not parsed.deliverable.text):
        raise CardError("incomplete_card", "Перед подтверждением укажите цель задания и результат, который нужно сдать.")
    return parsed


def manual_card(title: str) -> dict[str, Any]:
    """An empty form, not a fabricated analysis of an arbitrary uploaded PDF."""
    return Card(title=title, questions=[
        "Какова цель задания и что нужно сдать?",
        "Какие требования и критерии указаны в материалах?",
        "Если срок известен, укажите его; иначе оставьте поле пустым.",
    ]).model_dump()


async def analyze_assignment(gateway, provider, assignment_id: str) -> dict[str, Any]:
    result = await gateway.call("get_assignment", {"assignment_id": assignment_id})
    assignment = result["assignment"]
    if assignment.get("assignment_id") != assignment_id:
        raise CardError("invalid_assignment", "Получено другое задание.")
    if not assignment.get("is_uploaded"):
        raise CardError("read_only_assignment", "Подготовленный пример уже разобран. Загрузите своё задание.", 403)
    if provider.mode == "offline":
        card = manual_card(assignment["title"])
        return {"card": card, "mode": "offline", "usage": None,
                "trace": list(gateway.trace), "analysis_kind": "manual",
                "notice": "Модель не подключена. Заполните карточку вручную по своим материалам; автоматический AI-разбор не выполнялся."}
    # Previously confirmed user edits are not evidence for a new document analysis.
    context = {key: assignment[key] for key in ("assignment_id", "title", "sources")}
    generated = await provider.analyze(context)
    if not isinstance(generated, dict) or not isinstance(generated.get("card"), dict):
        raise CardError("invalid_card", "Модель вернула карточку в неподдерживаемом формате.")
    parsed = validate_card_sources(generated["card"], assignment.get("sources", []))
    # The model received documents, not newly supplied user constraints.
    if any(item.origin == "user" and item.text for item in [parsed.goal, parsed.deliverable, *parsed.requirements, *parsed.criteria]):
        raise CardError("invalid_provenance", "Модель не может выдавать свои предположения за пользовательские уточнения.")
    if parsed.deadline.origin == "user":
        raise CardError("invalid_provenance", "Модель не может придумывать пользовательский срок.")
    if any(task.estimate_source == "user" for task in parsed.tasks):
        raise CardError("invalid_provenance", "Предложенная моделью длительность является оценкой, а не вводом пользователя.")
    return {"card": parsed.model_dump(), "mode": provider.mode,
            "usage": generated.get("usage"), "trace": list(gateway.trace),
            "analysis_kind": "model", "notice": "Проверьте извлечённые требования и источники перед подтверждением."}


def confirm_card(proposed: Card | dict[str, Any], draft: dict[str, Any] | None,
                 available_sources) -> dict[str, Any]:
    """Only unchanged, server-issued document facts retain document provenance."""
    parsed = parse_card(proposed)
    old = parse_card(draft) if draft is not None else None
    allowed = _source_set(available_sources)
    # Reject foreign IDs even when an edit is about to lose document provenance.
    all_items = [parsed.goal, parsed.deliverable, *parsed.requirements, *parsed.criteria,
                 parsed.deadline, *parsed.tasks]
    if any(ref not in allowed for item in all_items for ref in item.source_ids):
        raise CardError("unsupported_source", "В карточке есть источник другого задания или неизвестный фрагмент.")

    def normalize(item: CardField, candidates: list[CardField]) -> CardField:
        unchanged = any(previous.origin == "document" and previous.text == item.text
                        and previous.source_ids == item.source_ids for previous in candidates)
        return item if item.origin == "document" and unchanged else CardField(text=item.text)

    changed_scope = old is None or any([
        parsed.goal.text != old.goal.text,
        parsed.deliverable.text != old.deliverable.text,
        [item.text for item in parsed.requirements] != [item.text for item in old.requirements],
    ])
    parsed.goal = normalize(parsed.goal, [old.goal] if old else [])
    parsed.deliverable = normalize(parsed.deliverable, [old.deliverable] if old else [])
    parsed.requirements = [normalize(item, old.requirements if old else []) for item in parsed.requirements]
    parsed.criteria = [normalize(item, old.criteria if old else []) for item in parsed.criteria]
    if parsed.deadline.date is None:
        parsed.deadline = CardDeadline()
    elif not (old and old.deadline.origin == "document" and parsed.deadline.origin == "document"
              and parsed.deadline.date == old.deadline.date and parsed.deadline.source_ids == old.deadline.source_ids):
        parsed.deadline = CardDeadline(date=parsed.deadline.date, origin="user")

    if changed_scope and old and parsed.tasks == old.tasks:
        parsed.tasks = []
    else:
        normalized_tasks = []
        for task in parsed.tasks:
            previous = next((item for item in old.tasks if item.id == task.id), None) if old else None
            unchanged = previous is not None and task == previous
            if not unchanged:
                task = CardTask(id=task.id, title=task.title, minutes=task.minutes, estimate_source="user")
            normalized_tasks.append(task)
        parsed.tasks = normalized_tasks
    parsed.questions = []
    return validate_card_sources(parsed, available_sources, confirmed=True).model_dump()
