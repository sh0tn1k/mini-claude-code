"""
Инструменты агента (руки модели).

Каждый инструмент — это:
  1. Python-функция, которая реально что-то делает.
  2. JSON-схема (typed schema), которую мы отдаём модели, чтобы она знала,
     как этот инструмент вызывать.
  3. Флаг read_only — read-only инструменты можно запускать параллельно,
     пишущие безопаснее сериализовать (мы это проговаривали: write+read одного
     файла в одном ходу = гонка).

Это пока минимальный набор под пройденную тему "мастер-луп".
Контекст/память/разрешения добавим позже, по мере прохождения статьи.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import background as bgexec
import snapshots
import task_graph
import team


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]   # JSON-схема входа для модели
    handler: Callable[..., str]  # сама функция
    read_only: bool              # можно ли параллелить


# --- сами функции-инструменты ---------------------------------------------

def _read_file(path: str, offset: int = 0, limit: int = 200) -> str:
    """Читаем файл кусками (offset/limit по строкам).

    Почему кусками, а не целиком: контекст — дорогой ресурс. Берём ровно
    столько строк, сколько нужно. Большой файл модель прочитает за несколько
    витков, каждый раз сдвигая offset.
    """
    p = Path(path)
    if not p.exists():
        return f"ОШИБКА: файла нет: {path}"
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    chunk = lines[offset:offset + limit]
    numbered = "\n".join(f"{i + offset + 1}\t{line}" for i, line in enumerate(chunk))
    tail = "" if offset + limit >= len(lines) else f"\n... (ещё {len(lines) - offset - limit} строк, сдвинь offset)"
    return numbered + tail if numbered else "(пусто)"


def _write_file(path: str, content: str) -> str:
    """Записываем файл целиком. Пишущая операция — НЕ read_only.

    Перед перезаписью КОД молча снимает снимок прежнего состояния (тема 14):
    если правка что-то сломает, revert_file вернёт файл одним вызовом. Снимок
    делает обвязка, а не модель, — страховка срабатывает всегда.
    """
    snapshots.snapshot(path)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"OK: записано {len(content)} символов в {path} (снимок сохранён — можно revert_file)"


def _list_dir(path: str = ".") -> str:
    """Список содержимого директории."""
    p = Path(path)
    if not p.exists():
        return f"ОШИБКА: нет пути: {path}"
    entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name))
    return "\n".join(("[dir] " if e.is_dir() else "      ") + e.name for e in entries) or "(пусто)"


def _bash(command: str, background: bool = False) -> str:
    """Выполнить shell-команду. Пишущая/опасная — НЕ read_only.

    background=True — уводим команду в отдельный поток и возвращаемся СРАЗУ
    (см. конспект, тема 8): для долгих операций (тесты, сборка, миграция),
    результат которых не нужен для следующего шага. Итог придёт позже
    отдельным сообщением. Выбор фон/не-фон делает модель, не обвязка.
    """
    if background:
        return bgexec.launch(command)
    try:
        r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return "ОШИБКА: таймаут 30с"
    out = (r.stdout or "") + (r.stderr or "")
    return f"(код выхода {r.returncode})\n{out.strip()}" or "(пустой вывод)"


def _edit_file(path: str, old_string: str, new_string: str) -> str:
    """Точечная правка: заменить кусок текста на новый.

    В отличие от write_file (перезаписывает целиком), edit меняет только
    найденный фрагмент. old_string должен встречаться РОВНО один раз — иначе
    непонятно, какое из вхождений менять, и мы отказываемся (защита от
    случайной массовой замены). Пишущая операция — НЕ read_only.
    """
    p = Path(path)
    if not p.exists():
        return f"ОШИБКА: файла нет: {path}"
    text = p.read_text(encoding="utf-8", errors="replace")
    count = text.count(old_string)
    if count == 0:
        return "ОШИБКА: old_string не найден в файле"
    if count > 1:
        return f"ОШИБКА: old_string встречается {count} раз — уточни его, нужно ровно 1 совпадение"
    snapshots.snapshot(path)   # снимок ДО правки — как и в write_file (тема 14)
    p.write_text(text.replace(old_string, new_string), encoding="utf-8")
    return f"OK: заменено в {path} (снимок сохранён — можно revert_file)"


def _revert_file(path: str) -> str:
    """Откатить файл к состоянию ДО последней записи (тема 14).

    Снимок сняла обвязка при write/edit; здесь мы им пользуемся. Пишущая
    операция — НЕ read_only. Решение «пора откатывать» принимает модель;
    само восстановление байтов — тупая структура, её делает код.
    """
    return snapshots.revert(path)


def _glob(pattern: str, path: str = ".") -> str:
    """Найти файлы по маске (например **/*.py). Read-only."""
    base = Path(path)
    if not base.exists():
        return f"ОШИБКА: нет пути: {path}"
    hits = [str(p) for p in sorted(base.glob(pattern)) if p.is_file()]
    return "\n".join(hits) if hits else "(ничего не найдено)"


def _grep(pattern: str, path: str = ".", glob: str = "**/*") -> str:
    """Искать строки по регулярке в файлах. Read-only.

    Возвращает совпадения в виде путь:номер_строки: текст. Идейно это
    мини-ripgrep: модель ищет, где что лежит, не читая файлы целиком.
    """
    import re
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"ОШИБКА: плохая регулярка: {e}"
    base = Path(path)
    out: list[str] = []
    for f in sorted(base.glob(glob)):
        if not f.is_file():
            continue
        try:
            for n, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    out.append(f"{f}:{n}: {line.strip()}")
                    if len(out) >= 200:
                        out.append("... (обрезано на 200 совпадениях)")
                        return "\n".join(out)
        except OSError:
            continue
    return "\n".join(out) if out else "(совпадений нет)"


# Список задач живёт прямо в обвязке — как и план в контексте, но теперь явно.
_TODOS: list[dict] = []


def _todo_write(todos: list) -> str:
    """Перезаписать список задач целиком.

    Каждая задача: {"content": str, "status": "pending"|"in_progress"|"done"}.
    Это план, ставший инструментом: модель фиксирует шаги структурно, мы их
    храним и показываем. Технически пишущая, но без гонок — НЕ read_only,
    чтобы шла по очереди.
    """
    global _TODOS
    _TODOS = [dict(t) for t in todos]
    mark = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
    lines = [f"{mark.get(t.get('status', 'pending'), '[ ]')} {t.get('content', '')}" for t in _TODOS]
    return "Список задач обновлён:\n" + ("\n".join(lines) if lines else "(пусто)")


def _subagent_placeholder(**_: Any) -> str:
    """Заглушка: субагент исполняется НЕ как обычный handler.

    run_subagent — особый инструмент: его «тело» — это рекурсивный запуск
    мастер-лупа (см. agent.py), а не синхронная функция. В реестре он есть
    только ради: (1) схемы для модели и (2) флага read_only для диспетчера.
    Реальное исполнение перехватывается в run_one_tool по имени.
    """
    return "ОШИБКА: run_subagent должен исполняться циклом, а не напрямую"


def _tool_search_placeholder(**_: Any) -> str:
    """Заглушка: tool_search исполняется НЕ как обычный handler.

    Он мутирует состояние цикла (множество «раскрытых» инструментов revealed),
    поэтому перехватывается в run_one_tool. В реестре нужен только ради схемы.
    """
    return "ОШИБКА: tool_search должен исполняться циклом, а не напрямую"


# --- навыки (skills) ------------------------------------------------------
# Навык = НЕ код, а текст-инструкция для самой модели. У него нет handler'а:
# load_skill просто кладёт его тело в контекст сообщением роли tool, и модель
# дальше ему следует. В контексте постоянно висят только метаданные (имя +
# описание из каталога) — само тело подгружается по требованию.

SKILLS: dict[str, dict[str, str]] = {
    "git-commit": {
        "description": "как оформить аккуратный git-коммит (когда просят закоммитить изменения)",
        "body": (
            "# Навык: аккуратный git-коммит\n\n"
            "1. Сначала посмотри `git status` и `git diff`, чтобы понять, что меняется.\n"
            "2. Сообщение пиши в повелительном наклонении, по-русски, одной строкой сути; "
            "при необходимости — пустая строка и подробности списком.\n"
            "3. НЕ коммить то, чего пользователь не просил; не делай `git add .` вслепую.\n"
            "4. После коммита покажи короткий `git log -1 --oneline` для подтверждения.\n"
        ),
    },
    "code-review": {
        "description": "как просмотреть код перед сдачей (когда просят проверить/review)",
        "body": (
            "# Навык: ревью кода\n\n"
            "1. Пройди по изменённым файлам через grep/read, не читая весь репозиторий целиком.\n"
            "2. Ищи: необработанные ошибки, дубли, мёртвый код, рассинхрон с комментариями.\n"
            "3. Для крупного обзора — раздай участки субагентам (read-only, параллельно).\n"
            "4. Верни короткий список замечаний с путями вида path:line, без воды.\n"
        ),
    },
    "debugging": {
        "description": "систематическая методика отладки: сбор данных → гипотеза → эксперимент → вывод (когда надо найти причину бага)",
        "body": (
            "# Навык: систематическая отладка (debugging)\n\n"
            "## Методика (цикл из 5 шагов)\n\n"
            "### 1. Воспроизведи проблему\n"
            "  – Чётко сформулируй ожидаемое vs реальное поведение.\n"
            "  – Если проблема нестабильна — найди минимальный стабильный триггер.\n\n"
            "### 2. Собери данные (не гадай!)\n"
            "  – Прочитай исходный код функции/модуля.\n"
            "  – Посмотри трассировку/стек/логи, если есть.\n"
            "  – Напиши минимальный тест, который демонстрирует проблему.\n"
            "  – Проверь граничные случаи: пустой список, None, дубли, race condition.\n\n"
            "### 3. Сформулируй гипотезу(ы)\n"
            "  – На основе данных: «вот ЭТО условие ложно, когда должно быть истинно».\n"
            "  – Перечисли все возможные причины, от самых вероятных к редким.\n\n"
            "### 4. Проверь гипотезу экспериментом\n"
            "  – Добавь временный print/log в подозрительное место и запусти.\n"
            "  – ИЛИ: напиши юнит-тест на конкретный сценарий.\n"
            "  – ИЛИ: инспектируй состояние через read_file/grep/bash.\n"
            "  – ОДИН эксперимент за раз — иначе не поймёшь, что сработало.\n\n"
            "### 5. Вывод и исправление\n"
            "  – Подтвердилась гипотеза → чини и пиши тест, чтобы баг не вернулся.\n"
            "  – Не подтвердилась → вернись к шагу 2 с новыми данными.\n\n"
            "## Принципы\n"
            "  • Никогда не меняй код наугад — только под гипотезой.\n"
            "  • Одна переменная за раз (не меняй 5 вещей и не жди чуда).\n"
            "  • Если не можешь объяснить проблему словами — ты ещё не собрал достаточно данных.\n"
            "  • «Это невозможно» обычно означает «я смотрю не туда».\n"
        ),
    },
}


def skill_catalog_text() -> str:
    """Уровень 0: метаданные навыков для системного промпта (всегда в контексте)."""
    if not SKILLS:
        return "  (навыков нет)"
    return "\n".join(f"  • {name} — {s['description']}" for name, s in SKILLS.items())


def _load_skill(name: str) -> str:
    """Подгрузить тело навыка. Обычный handler: возвращает текст-инструкцию."""
    skill = SKILLS.get(name)
    if skill is None:
        avail = ", ".join(SKILLS) or "(нет)"
        return f"ОШИБКА: навыка '{name}' нет. Доступные: {avail}"
    return skill["body"]


# --- реестр инструментов (typed dispatch registry) ------------------------

REGISTRY: dict[str, Tool] = {}

# Мета-инструменты доступны ВСЕГДА (не прячутся за tool_search) — иначе нечем
# было бы раскрывать остальные. Это «уровень 0» механики раскрытия.
META_TOOLS: tuple[str, ...] = ("tool_search", "load_skill")


def register(tool: Tool) -> None:
    REGISTRY[tool.name] = tool


register(Tool(
    name="read_file",
    description="Прочитать текстовый файл по строкам (offset/limit). Возвращает строки с номерами.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "путь к файлу"},
            "offset": {"type": "integer", "description": "с какой строки (0 = начало)"},
            "limit": {"type": "integer", "description": "сколько строк за раз"},
        },
        "required": ["path"],
    },
    handler=_read_file,
    read_only=True,
))

register(Tool(
    name="write_file",
    description="Записать (перезаписать) текстовый файл целиком.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
    handler=_write_file,
    read_only=False,
))

register(Tool(
    name="list_dir",
    description="Показать содержимое директории.",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": [],
    },
    handler=_list_dir,
    read_only=True,
))

register(Tool(
    name="bash",
    description=(
        "Выполнить shell-команду и вернуть её вывод. Для ДОЛГИХ операций (тесты, "
        "сборка, миграция), результат которых не нужен для следующего шага, "
        "ставь background=true: команда уйдёт в фон, ты сразу получишь управление "
        "и сможешь заняться другой независимой работой, а итог придёт позже "
        "отдельным сообщением. Если результат нужен прямо сейчас — не ставь фон."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "background": {
                "type": "boolean",
                "description": "запустить в фоне (для долгих операций, результат которых не нужен немедленно)",
            },
        },
        "required": ["command"],
    },
    handler=_bash,
    read_only=False,
))

register(Tool(
    name="edit_file",
    description="Точечно заменить фрагмент текста в файле. old_string должен встречаться ровно один раз.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string", "description": "что заменить (уникальный фрагмент)"},
            "new_string": {"type": "string", "description": "на что заменить"},
        },
        "required": ["path", "old_string", "new_string"],
    },
    handler=_edit_file,
    read_only=False,
))

register(Tool(
    name="revert_file",
    description=(
        "Откатить файл к состоянию ДО последней записи (write_file/edit_file). "
        "Обвязка снимает снимок перед каждой правкой автоматически, поэтому "
        "откат стоит один вызов. Используй, если твоя правка что-то сломала "
        "(упали тесты, сборка) и надо быстро вернуть прежнее содержимое. "
        "Повторный вызов откатывает ещё на шаг назад по истории правок файла."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "путь к файлу, который надо откатить"},
        },
        "required": ["path"],
    },
    handler=_revert_file,
    read_only=False,
))

register(Tool(
    name="glob",
    description="Найти файлы по маске, например **/*.py. Возвращает список путей.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "маска, например **/*.py"},
            "path": {"type": "string", "description": "где искать (по умолчанию .)"},
        },
        "required": ["pattern"],
    },
    handler=_glob,
    read_only=True,
))

register(Tool(
    name="grep",
    description="Искать строки по регулярному выражению в файлах. Возвращает путь:строка: текст.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "регулярка для поиска"},
            "path": {"type": "string", "description": "где искать (по умолчанию .)"},
            "glob": {"type": "string", "description": "маска файлов, по умолчанию **/*"},
        },
        "required": ["pattern"],
    },
    handler=_grep,
    read_only=True,
))

register(Tool(
    name="TodoWrite",
    description="Записать/обновить список задач (план). Используй в начале сложной задачи и обновляй статусы по ходу.",
    parameters={
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "полный список задач (перезаписывает прошлый)",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "done"]},
                    },
                    "required": ["content", "status"],
                },
            },
        },
        "required": ["todos"],
    },
    handler=_todo_write,
    read_only=False,
))


# --- граф зависимостей задач (durable DAG в .agent_tasks.json) ------------
# Это НЕ TodoWrite. TodoWrite — плоский список на одну сессию, в памяти обвязки.
# Граф — узлы+рёбра на диске: переживает рестарт, код вычисляет готовые задачи.
# TodoWrite для простой линейной работы, граф — для крупной с зависимостями.

register(Tool(
    name="add_tasks",
    description=(
        "Завести задачи в durable-граф (.agent_tasks.json) для КРУПНОЙ работы "
        "со множеством подзадач и зависимостями между ними (то, что переживёт "
        "рестарт). Можно одним вызовом разложить всю фичу: каждая задача — "
        "{content, deps}, где deps — 0-базовые индексы задач ВНУТРИ этого же "
        "пакета, от которых она зависит. Для простого линейного плана на одну "
        "сессию используй TodoWrite, а не это."
    ),
    parameters={
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "description": "список задач для добавления в граф",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "что нужно сделать"},
                        "deps": {
                            "type": "array",
                            "description": "индексы (0-базовые) задач в этом же пакете, которые надо сделать раньше",
                            "items": {"type": "integer"},
                        },
                    },
                    "required": ["content"],
                },
            },
        },
        "required": ["tasks"],
    },
    handler=task_graph.add_tasks,
    read_only=False,
))

register(Tool(
    name="update_task",
    description="Пометить задачу графа новым статусом (pending/in_progress/done). Запись на диск делает код.",
    parameters={
        "type": "object",
        "properties": {
            "id": {"type": "integer", "description": "id задачи (#N из списка графа)"},
            "status": {"type": "string", "enum": ["pending", "in_progress", "done"]},
        },
        "required": ["id", "status"],
    },
    handler=task_graph.update_task,
    read_only=False,
))

register(Tool(
    name="list_tasks",
    description="Показать граф задач и какие задачи ГОТОВЫ к работе сейчас (зависимости закрыты). Read-only.",
    parameters={"type": "object", "properties": {}, "required": []},
    handler=task_graph.list_tasks,
    read_only=True,
))

# Автономное взятие задачи (pull-модель, тема 11): воркер сам берёт готовую
# задачу из общей кучи. read_only=False — операция ПИШЕТ (in_progress + владелец),
# и код делает её атомарной под Lock, чтобы два воркера не схватили одну задачу.
register(Tool(
    name="claim_task",
    description=(
        "Атомарно взять следующую ГОТОВУЮ задачу графа себе (pull-модель): "
        "код под замком помечает её in_progress и записывает, кто взял, — "
        "два воркера не возьмут одну задачу. Никто не адресует задачу; берёшь сам."
    ),
    parameters={
        "type": "object",
        "properties": {
            "worker": {"type": "string", "description": "Имя воркера, берущего задачу"},
        },
        "required": ["worker"],
    },
    handler=task_graph.claim_task,
    read_only=False,
))


# --- постоянные товарищи с JSONL-ящиками + FSM-протокол -------------------
# Это НЕ субагент. Субагент — одноразовый, безымянный, изолированный, умирает
# после return. Товарищ — именованный, с персистентным ящиком (.jsonl), живёт
# между сессиями, ему может писать любой; координацию дисциплинирует FSM,
# правила которого проверяет КОД (см. team.py, конспект темы 9 и 10).

register(Tool(
    name="team_register",
    description=(
        "Завести ПОСТОЯННОГО товарища по команде (имя-адрес + персистентный "
        "почтовый ящик .jsonl, стартовое состояние IDLE). В отличие от субагента "
        "он именованный, переживает сессии, и ему может писать кто угодно. "
        "Заводи товарищей, когда нужна долгая совместная работа с перепиской "
        "(проектирование, ревью по кругу), а не разовое изолированное исследование."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "уникальное имя-адрес товарища"},
            "role": {"type": "string", "description": "роль (например 'архитектор', 'ревьюер')"},
        },
        "required": ["name"],
    },
    handler=team.team_register,
    read_only=False,
))

register(Tool(
    name="team_send",
    description=(
        "Дописать письмо в ящик товарища (асинхронно). type — один из: task, "
        "question, review_request, review, reply, cancel. Доставку решает "
        "состояние получателя по FSM: если его состояние сейчас этот тип не "
        "принимает — письмо не теряется, а ждёт в ящике до подходящего состояния."
    ),
    parameters={
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "имя-адрес получателя"},
            "type": {
                "type": "string",
                "enum": ["task", "question", "review_request", "review", "reply", "cancel"],
                "description": "тип письма (определяет, какое состояние его примет)",
            },
            "content": {"type": "string", "description": "текст письма"},
            "sender": {"type": "string", "description": "кто пишет (по умолчанию coordinator)"},
        },
        "required": ["to", "type", "content"],
    },
    handler=team.team_send,
    read_only=False,
))

register(Tool(
    name="team_inbox",
    description=(
        "Прочитать ДОСТУПНЫЕ сейчас письма товарища — те, чей тип принимается его "
        "текущим состоянием по FSM. Недоступные не показываются и остаются ждать "
        "в ящике. Прочитанные больше не выдаются повторно."
    ),
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string", "description": "чей ящик читать"}},
        "required": ["name"],
    },
    handler=team.team_inbox,
    read_only=False,   # двигает курсор прочитанного — пишущая, гоняем по очереди
))

register(Tool(
    name="team_set_state",
    description=(
        "Сменить состояние товарища по FSM: IDLE, WORKING, WAITING_REVIEW, "
        "BLOCKED, DONE. Код проверит, что переход допустим. Для BLOCKED укажи "
        "waiting_on — кого именно ждём (по этому коду он ловит дедлоки)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "кому меняем состояние"},
            "state": {
                "type": "string",
                "enum": ["IDLE", "WORKING", "WAITING_REVIEW", "BLOCKED", "DONE"],
            },
            "waiting_on": {"type": "string", "description": "для BLOCKED: имя товарища, которого ждём"},
        },
        "required": ["name", "state"],
    },
    handler=team.team_set_state,
    read_only=False,
))

register(Tool(
    name="team_status",
    description="Показать всю команду: состояния, кто кого ждёт, размер ящиков и обнаруженные ДЕДЛОКИ (считает код). Read-only.",
    parameters={"type": "object", "properties": {}, "required": []},
    handler=team.team_status,
    read_only=True,
))


register(Tool(
    name="run_subagent",
    description=(
        "Запустить субагента для изолированной подзадачи (например исследовать "
        "часть кодовой базы). Субагент работает в СВОЁМ контексте и вернёт тебе "
        "только итоговый ответ — все его промежуточные шаги останутся внутри и "
        "не засорят твой контекст. Используй для объёмного исследования/поиска."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "что субагент должен сделать; пиши самодостаточно — он не видит твой диалог",
            },
        },
        "required": ["task"],
    },
    handler=_subagent_placeholder,
    read_only=False,   # упрощённо: пишущий — гоняем по очереди (см. конспект, тема 4)
))


register(Tool(
    name="tool_search",
    description=(
        "Найти и РАСКРЫТЬ инструмент по запросу, чтобы потом его вызвать. "
        "Сразу доступны не все инструменты — их полные схемы скрыты, чтобы не "
        "засорять контекст. Каталог скрытых инструментов есть в системном "
        "промпте. Передай ключевое слово в query (или 'select:имя1,имя2' для "
        "точного списка); раскрытые инструменты станут доступны со следующего хода."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "ключевое слово (ищет по имени и описанию) или 'select:имя1,имя2'",
            },
        },
        "required": ["query"],
    },
    handler=_tool_search_placeholder,
    read_only=True,
))

register(Tool(
    name="load_skill",
    description=(
        "Подгрузить навык — текстовую инструкцию для тебя самого. Каталог "
        "навыков (имя — когда применять) есть в системном промпте. Тело навыка "
        "придёт результатом и ты следуешь ему до конца задачи."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "имя навыка из каталога"},
        },
        "required": ["name"],
    },
    handler=_load_skill,
    read_only=True,
))


def deferred_tool_names(exclude: tuple[str, ...] = ()) -> list[str]:
    """Имена «скрытых» инструментов (всё, кроме мета-инструментов и exclude)."""
    return [n for n in REGISTRY
            if n not in META_TOOLS and n not in exclude]


def catalog_text(exclude: tuple[str, ...] = ()) -> str:
    """Уровень 0: каталог скрытых инструментов (имя — описание) для системного промпта."""
    names = deferred_tool_names(exclude)
    if not names:
        return "  (скрытых инструментов нет)"
    return "\n".join(f"  • {n} — {REGISTRY[n].description}" for n in names)


def search_catalog(query: str, exclude: tuple[str, ...] = ()) -> list[str]:
    """Сопоставить запрос с каталогом скрытых инструментов -> список имён.

    Поддержка двух форм:
      * 'select:имя1,имя2' — точный выбор по именам;
      * иначе — подстрока (без регистра) по имени ИЛИ описанию.
    """
    names = deferred_tool_names(exclude)
    q = (query or "").strip()
    if q.startswith("select:"):
        wanted = {w.strip() for w in q[len("select:"):].split(",") if w.strip()}
        return [n for n in names if n in wanted]
    ql = q.lower()
    if not ql:
        return []
    return [n for n in names if ql in n.lower() or ql in REGISTRY[n].description.lower()]


def spec_for(names: list[str], exclude: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Схемы для КОНКРЕТНЫХ инструментов в формате OpenAI/DeepSeek API.

    Отдаём ровно то, что сейчас «раскрыто» (мета-инструменты + найденные через
    tool_search). exclude убирает запрещённое (например run_subagent у субагента),
    даже если оно как-то просочилось в names. Дубли и неизвестные имена отсеиваем.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for n in names:
        if n in seen or n in exclude or n not in REGISTRY:
            continue
        seen.add(n)
        t = REGISTRY[n]
        out.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        })
    return out
