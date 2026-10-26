# Независимое ревью вспомогательных программ

Дата: 2026-10-26. Объект: `tools/validate_skills.py` и `.agents/skills/study-eval/scripts/score_eval.py`. Это не проверка приложения, модели, MCP-сервиса или хостинга.

До выбора маршрутов прочитаны только `inputs/skill-descriptions.json` и `inputs/trigger-queries.json`; решения записаны в `routing-v1.json`. Авторские expected labels, тела project skills и чужие результаты не использовались. Затем прочитаны только два проверяемых скрипта. Существующие файлы программ не изменялись; синтетические проверки выполнялись в `/tmp`.

## Подтверждённые дефекты

### 1. P2: каталог skill без SKILL.md исчезает из проверки

Место: `tools/validate_skills.py:85–86`.

`main()` собирает только уже существующие `*/SKILL.md`. Поэтому каталог skill с удалённым или переименованным главным файлом не передаётся в `validate()`, хотя у неё есть проверка отсутствующего SKILL.md. Если хотя бы один другой skill цел, процесс печатает `OK` и завершается с кодом 0. Число проверенных skills уменьшается; потерянный skill не становится ошибкой.

Воспроизведено: один исправный skill `valid` и каталог `missing-skill` без главного файла → `OK: 1 skills; metadata, resource links, UI and eval cases valid.`, exit 0.

Исправление: обнаруживать ожидаемые каталоги/явно заданный набор проектных skills до поиска SKILL.md и валидировать каждый. Проверять отсутствие одного skill при наличии остальных, а не только отсутствие всех skills.

### 2. P2: UI-метаданные принимаются без interface

Место: `tools/validate_skills.py:54–68`.

Строка `interface:` пропускается при наличии, но её наличие и единственность нигде не проверяются. Удаление этой строки оставляет три отступленных поля, которые скрипт принимает. В YAML эти поля находятся в корневом mapping, а ожидаемый `interface` отсутствует. Валидатор всё равно объявляет UI корректным.

Воспроизведено: удалить только `interface:\n` из исправного `agents/openai.yaml` → тот же `OK`, exit 0.

Исправление: в поддерживаемом маленьком подмножестве явно требовать один заголовок `interface:` перед его полями; отклонять повторные поля и неверную вложенность.

### 3. P2: Eval-файлы с неверными типами полей объявляются корректными

Место: `tools/validate_skills.py:70–78`.

Поля `prompt`, `expected_output`, `expectations` проверяются только на truthiness. Число вместо текста prompt, массив вместо текста expected_output и строка вместо списка expectations проходят. Это ложный успех валидации тестового набора; такие поля не представляют заявленную структуру задания и проверяемых ожиданий.

Воспроизведено для обоих случаев: `prompt: 123`, `expected_output: ["not a string"]`, `expectations: "not a list"` → тот же `OK`, exit 0.

Исправление: проверять тип контейнера evals, тип каждого случая, непустые текстовые prompt/expected_output и непустой список непустых текстовых expectations. Некорректный тип должен давать диагностируемую ошибку.

### 4. P2: повторяющийся JSON-ключ может затереть провал и дать 100%

Место: `.agents/skills/study-eval/scripts/score_eval.py:101–102`; такой же permissive JSON loader используется в `tools/validate_skills.py:71`.

`json.loads()` по умолчанию сохраняет последнее значение повторяющегося ключа. Проверки duplicate case IDs и duplicate check names выполняются уже после потери предыдущего значения. Поэтому вход с `"passed": false, "passed": true` принимается как полностью успешный, хотя содержит два противоречащих суждения. Аналогично повторный ключ `cases` способен скрыть предыдущий массив ещё до фиксации знаменателя.

Воспроизведено на одном критичном случае и одном check: вход ниже → exit 0, `check_summary = {"passed": 1, "total": 1, "pass_rate": 1.0}`, `all_critical_passed = true`.

```json
{"dataset_id":"review","run_id":"r","mode":"offline","cases":[{"id":"x","checks":[{"name":"source","passed":false,"passed":true,"evidence":"wrong source"}]}]}
```

Исправление: загружать оба JSON-документа через `object_pairs_hook`, который отвергает повторные ключи во всех объектах. Для CLI некорректный вход должен завершаться кодом 2, а не отчётом об успехе. Это проверка структуры входа, а не автоматическая проверка истинности предоставленных суждений.

## Полный воспроизводимый пример

Запустить из корня проекта. Скрипт копирует только валидатор в новый временный каталог и создаёт там собственные минимальные fixtures. Реальные skills и результаты не меняются.

```sh
python3 - <<'PY'
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

project = Path.cwd()
tmp = Path(tempfile.mkdtemp(prefix='study-tool-review-', dir='/tmp'))
(tmp / 'tools').mkdir()
shutil.copyfile(project / 'tools/validate_skills.py', tmp / 'tools/validate_skills.py')
p = tmp / '.agents/skills/valid'
(p / 'agents').mkdir(parents=True)
(p / 'evals').mkdir()
(p / 'SKILL.md').write_text(
    '---\nname: valid\ndescription: "Example skill description."\n---\nExample body.\n'
)
ui = p / 'agents/openai.yaml'
ui_text = ('interface:\n  display_name: "Example"\n'
           '  short_description: "Example description of valid length"\n'
           '  default_prompt: "Use $valid"\n')
ui.write_text(ui_text)
evals = p / 'evals/evals.json'
eval_data = {'skill_name': 'valid', 'evals': [
    {'id': i, 'prompt': 'p', 'expected_output': 'o', 'expectations': ['e']}
    for i in (1, 2)
]}
evals.write_text(json.dumps(eval_data))

def run(label):
    r = subprocess.run(['python3', str(tmp / 'tools/validate_skills.py')],
                       capture_output=True, text=True)
    print(label, 'exit', r.returncode, repr(r.stdout), repr(r.stderr))

missing = tmp / '.agents/skills/missing-skill'
missing.mkdir()
run('missing SKILL.md')
missing.rmdir()
ui.write_text(ui_text.replace('interface:\n', ''))
run('missing interface')
ui.write_text(ui_text)
for row in eval_data['evals']:
    row.update(prompt=123, expected_output=['not a string'], expectations='not a list')
evals.write_text(json.dumps(eval_data))
run('wrong eval field types')

dataset = tmp / 'dataset.json'
results = tmp / 'results.json'
dataset.write_text('{"dataset_id":"review","cases":[{"id":"x","checks":["source"],"critical":true}]}')
results.write_text('{"dataset_id":"review","run_id":"r","mode":"offline","cases":[{"id":"x","checks":[{"name":"source","passed":false,"passed":true,"evidence":"wrong source"}]}]}')
r = subprocess.run(['python3', str(project / '.agents/skills/study-eval/scripts/score_eval.py'),
                    str(dataset), str(results)], capture_output=True, text=True)
print('duplicate passed key', 'exit', r.returncode, r.stdout, r.stderr)
print('Fixtures:', tmp)
PY
```

Фактический результат: все четыре сценария завершились с кодом 0. Три запуска валидатора напечатали `OK`; агрегатор выставил 100%.

## Положительные контрольные проверки

Отдельно вызвана `score()` с фиксированными 30 ожидаемыми случаями: 24 успешных, 4 с `error="timeout"` и пустыми checks, 2 без результата. Фактически получено 24/30 = 0.8 и по случаям, и по проверкам; `all_critical_passed=false`. Ошибки и пропуски из знаменателя не исчезли.

Отдельные мутации этого контрольного ввода отклонены с `ValueError`: повторный result ID, неизвестный result ID, строка `"true"` вместо boolean passed и отсутствующий evidence. Эти защитные проверки работают. Непустая строка evidence сама по себе не доказывает истинность ответа; это явно заявленное ограничение агрегатора, а не отдельный обнаруженный дефект.
