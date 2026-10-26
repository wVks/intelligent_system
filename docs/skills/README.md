# Skills для разработки StudyNavigator

Четыре проектных skill находятся в `.agents/skills/`. Постоянный контекст и
маршрутизация — в [AGENTS.md](../../AGENTS.md), исходные требования и границы
MVP — в [project-brief.md](../project-brief.md).

| Skill | Пример запроса |
|---|---|
| [study-build](../../.agents/skills/study-build/SKILL.md) | `$study-build Сделай первый сценарий ответа про сдачу в LMS` |
| [study-mcp](../../.agents/skills/study-mcp/SKILL.md) | `$study-mcp Реализуй сервер и клиент для трёх инструментов` |
| [study-eval](../../.agents/skills/study-eval/SKILL.md) | `$study-eval Проверь ответы, источники и неизвестные сроки` |
| [study-release](../../.agents/skills/study-release/SKILL.md) | `$study-release Проверь готовность и подготовь материалы защиты` |

Skills могут выбираться автоматически по описанию задачи; явный вызов позволяет
указать нужный навык. Codex обнаруживает проектные skills в `.agents/skills`;
если новый список не появился, переоткрой проект/сессию.
[Официальная документация](https://learn.chatgpt.com/docs/build-skills).

Используй их по этапам: первый работающий срез → MCP и LLM → проверка качества →
хостинг и сдача. Для небольшой правки достаточно относящегося к ней навыка.
Краткие инструкции подгружают подробные references только при необходимости.

## Локальные проверки набора

Из корня проекта, без API-ключей и дополнительных Python-пакетов:

```sh
python3 tools/validate_skills.py
python3 -m unittest discover -s tests/skills -v
```

Первая команда проверяет используемое здесь подмножество YAML, имена, локальные
ссылки, UI-метаданные и наличие испытаний. Это не универсальный YAML-парсер.
Вторая проверяет вспомогательный агрегатор результатов и другие программы набора.
Ни одна из них не подменяет тесты приложения.

Программа `.agents/skills/study-eval/scripts/score_eval.py` суммирует уже
проверенные результаты по полному манифесту. Её формат и ограничения описаны в
[evaluation.md](../../.agents/skills/study-eval/references/evaluation.md).

## Испытания и происхождение

Методика и источники: [sources.md](sources.md).
Сценарии, snapshots, результаты независимых исполнителей и сравнения:
[validation](validation/).
Итоговая методика и результаты: [отчёт](validation/report.md).
Результаты проверки skills не являются Eval ещё не созданного StudyNavigator.
