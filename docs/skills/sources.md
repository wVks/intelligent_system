# Источники и выбор методов

Проверено 26 октября 2026 года. Это небольшой набор навыков для разработки
StudyNavigator; он не является рейтингом всех существующих skills.

## Создание и размещение skills

- [Official OpenAI documentation: Build skills](https://learn.chatgpt.com/docs/build-skills):
  проектное размещение `.agents/skills`, точные descriptions, автоматический
  выбор по задаче, короткий SKILL.md и подробности по необходимости.
- [Official OpenAI documentation: Best practices](https://learn.chatgpt.com/docs/learn/best-practices):
  постоянные правила проекта в AGENTS.md, ограниченная задача у каждого skill,
  конкретные входы/выходы и проверка на реальных сценариях.
- [Agent Skills specification](https://agentskills.io/specification): переносимый
  формат каталога с SKILL.md. Проект использует минимальные метаданные name/description.

Использованы локальные методики `skill-creator` из системной поставки Codex и
установленного набора навыков. Они дали цикл: черновик → независимое выполнение
задач со skill и baseline → оценка исходов → узкая корректировка → повторный прогон.

## Отбор инженерных подходов

Из установленного `claude-cowork/engineering/1.2.0/skills` изучены:

| Исходный skill | Что адаптировано | Куда |
|---|---|---|
| system-design | Границы MVP, поток данных, явные контракты | study-build, study-mcp |
| debug | Воспроизведение → локализация причины → исправление → регрессия | study-build |
| testing-strategy | Проверка критических путей и реальных ошибок | study-eval |
| code-review | Валидация границ, безопасный вывод текста, конкретные дефекты | study-build, study-mcp |
| deploy-checklist | Чистый запуск, smoke-сценарий, конфигурация и состояние данных | study-release |

Инструкции написаны заново под учебное задание. Корпоративные процессы staging,
canary и многоступенчатые approvals не нужны выбранному объёму MVP.

## Технические источники

- [MCP Python SDK](https://py.sdk.modelcontextprotocol.io/) и
  [MCP client](https://py.sdk.modelcontextprotocol.io/client/): проверка версии
  SDK перед выбором API, использование готовой реализации протокола.
- [MCP tools specification](https://modelcontextprotocol.io/specification/2026-07-28/server/tools):
  схемы инструментов, структурированные результаты, различение ошибок.
- [MCP testing](https://py.sdk.modelcontextprotocol.io/get-started/testing/) и
  [транспорты](https://py.sdk.modelcontextprotocol.io/client/transports/):
  настоящий client/server обмен плюс проверка выбранного транспорта.
- [FastAPI lifespan tests](https://fastapi.tiangolo.com/advanced/testing-events/):
  жизненный цикл ресурсов в приложении и тестах.
- [Pydantic strict mode](https://docs.pydantic.dev/latest/concepts/strict_mode/):
  осмысленная валидация входов без механического глобального strict mode.
- [Anthropic: Demystifying evals for AI agents](https://www.anthropic.com/research/demystifying-evals-for-ai-agents):
  оценка исхода, изоляция прогонов, свидетельства, несколько trials для
  стохастических результатов, различение поведения инструмента и агента.

## Проектные решения

Вертикальные срезы, read-only MCP, источники у фактов, неизвестные даты как null,
проверка арифметики плана кодом и разделение offline/live/hosted — практические
решения для данной задачи. Они не являются дополнительными требованиями PDF.
Модель, её провайдер, точные версии SDK и хостинг выбираются при реализации.

Вспомогательный score_eval.py только агрегирует оценки с доказательствами.
Самостоятельно оценивать достоверность текста или подтверждать live режим он не умеет.
