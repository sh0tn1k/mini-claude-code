"""
Мастер-луп — главный цикл агента.

Воплощаем ровно то, что разобрали:

  * Цикл крутится по СТРУКТУРЕ ответа модели: есть tool_calls -> продолжаем,
    нет tool_calls -> это финальный текст -> выходим. Мы НЕ читаем смысл текста.
  * Текст рядом с инструментами ("мысли вслух") показываем пользователю и
    сохраняем в контекст вместе с tool_calls.
  * Несколько инструментов в одном ходу независимы по данным -> read-only
    запускаем параллельно, пишущие — по очереди.
  * Результат инструмента попадает в контекст и виден модели только на
    следующем витке.

Модель: DeepSeek (OpenAI-совместимый API).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from types import SimpleNamespace

from dotenv import load_dotenv
from openai import AsyncOpenAI

import background
import cache
import events
import mcp_runtime
import permissions
import sessions
import steering
import task_graph
import team
import ui
from tools import (
    META_TOOLS,
    REGISTRY,
    catalog_text,
    search_catalog,
    skill_catalog_text,
    spec_for,
)

load_dotenv()

client = AsyncOpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
)

MODEL = "deepseek-v4-flash"

# --- Сжатие контекста -------------------------------------------------------
# Окно DeepSeek — 1М токенов. Токены не считаем точно: берём символы как
# грубый прокси (≈4 символа на токен). Нам нужна не точность, а сигнал
# «пора сжимать».
CONTEXT_WINDOW_TOKENS = 1_000_000
CHARS_PER_TOKEN = 4
CONTEXT_WINDOW_CHARS = CONTEXT_WINDOW_TOKENS * CHARS_PER_TOKEN

COMPACT_THRESHOLD = 0.92      # на ~92% заполнения запускаем сжатие
KEEP_RECENT = 6               # столько последних сообщений храним ДОСЛОВНО (горячий хвост)
TOOL_TRIM_OVER = 2_000        # тело tool-результата длиннее этого — кандидат на обрезку (ур.1)

COMPACT_SYSTEM = (
    "Ты — сжиматель контекста. Тебе дают историю диалога агента с инструментами. "
    "Сожми её в плотную сводку: что просил пользователь, какие выводы/факты добыты, "
    "что уже сделано, какие файлы/пути затронуты, что осталось. Без воды, по делу, "
    "сохрани конкретику (имена файлов, числа, решения). Это станет памятью агента "
    "вместо выброшенной истории."
)

SYSTEM_PROMPT = (
    "Ты — агент с доступом к инструментам (файлы, директории, shell). "
    "Чтобы что-то узнать или сделать в системе пользователя — ВЫЗЫВАЙ инструмент. "
    "Просто написать про действие недостаточно: без вызова инструмента ничего не произойдёт. "
    "\n\n"
    "ПЛАНИРОВАНИЕ. Если задача состоит из нескольких шагов — В САМОМ НАЧАЛЕ вызови "
    "инструмент TodoWrite и распиши план списком коротких этапов (status='pending'). "
    "По ходу работы ОБНОВЛЯЙ его тем же TodoWrite: текущий этап ставь 'in_progress', "
    "завершённый — 'done'. Так план всегда перед глазами и видно прогресс. "
    "Не записывай план в обычный файл (.md) — для рабочего плана задачи используй именно TodoWrite. "
    "Для простой задачи в один шаг план не нужен. "
    "\n\n"
    "КРУПНАЯ РАБОТА С ЗАВИСИМОСТЯМИ. Если работа большая (много подзадач, "
    "зависящих друг от друга, возможно на несколько сессий) — вместо TodoWrite "
    "заведи durable-граф: add_tasks(...) разложит фичу на подзадачи со связями "
    "(deps), update_task помечает статусы, list_tasks показывает, какие задачи "
    "ГОТОВЫ к работе сейчас. Граф лежит на диске и переживает рестарт. "
    "TodoWrite — для простого линейного плана в одну сессию; граф — для сложного с зависимостями. "
    "\n\n"
    "КОМАНДА ПОСТОЯННЫХ ТОВАРИЩЕЙ. Субагент (run_subagent) — одноразовый, "
    "безымянный, изолированный: сделал одну подзадачу и вернул текст, контекст "
    "стёрся. Для ДОЛГОЙ совместной работы с перепиской (проектирование, ревью по "
    "кругу) заводи именованных товарищей: team_register создаёт товарища с "
    "персистентным ящиком, team_send шлёт письмо (task/question/review_request/"
    "review/reply/cancel), team_inbox читает доступные письма, team_set_state "
    "меняет его состояние по FSM (IDLE/WORKING/WAITING_REVIEW/BLOCKED/DONE), "
    "team_status показывает всю команду и дедлоки. Правила протокола (допустимость "
    "перехода, доставка письма по состоянию, детект тупиков) проверяет КОД — ты "
    "лишь двигаешь смысл. Для BLOCKED указывай, кого ждёшь (waiting_on). "
    "\n\n"
    "Когда задача выполнена — ответь обычным текстом без вызовов инструментов."
    "\n\n"
    "ПРЕРЫВАНИЕ. Если посреди работы приходит user-сообщение с пометкой [INTERRUPT] — "
    "это пользователь нажал Ctrl+C и перенаправляет тебя. НЕМЕДЛЕННО остановись: "
    "не вызывай инструменты, кратко суммируй обычным текстом, что уже сделано, над чем "
    "работал и что осталось, и жди новой инструкции."
    "\n\n"
    "ПОСТЕПЕННЫЙ ДОСТУП К ИНСТРУМЕНТАМ. Сразу тебе доступны только tool_search и "
    "load_skill — остальные инструменты СКРЫТЫ, чтобы не засорять контекст. "
    "Чтобы получить нужный инструмент, вызови tool_search(query=...): он его раскроет, "
    "и СЛЕДУЮЩИМ ходом ты сможешь его вызвать. Если задача подходит под навык — "
    "сначала подгрузи его через load_skill(name=...)."
)

# Динамический хвост системного промпта: каталоги уровня 0 (метаданные).
# Само наполнение скрытых тулов и тел навыков сюда НЕ кладём — только имена.
_CATALOG_TEMPLATE = (
    "\n\nКАТАЛОГ СКРЫТЫХ ИНСТРУМЕНТОВ (имя — зачем; раскрывай через tool_search):\n{tools}"
    "\n\nКАТАЛОГ НАВЫКОВ (имя — когда применять; подгружай через load_skill):\n{skills}"
)


def build_system_prompt(base: str, exclude: tuple[str, ...] = ()) -> str:
    """Системный промпт + каталоги (уровень 0). exclude прячет тул из каталога
    (например run_subagent у субагента — чтобы он его и найти не мог)."""
    return base + _CATALOG_TEMPLATE.format(
        tools=catalog_text(exclude),
        skills=skill_catalog_text(),
    )

# Системный промпт субагента: он — изолированный работник, видит только
# свою задачу (она придёт user-сообщением) и должен вернуть готовый итог текстом.
SUBAGENT_SYSTEM_PROMPT = (
    "Ты — субагент-исполнитель. Тебе дали одну конкретную подзадачу. "
    "Ты работаешь в собственном изолированном контексте: основного диалога ты НЕ видишь. "
    "Используй инструменты (файлы, поиск, shell), чтобы выполнить задачу, "
    "а затем верни РОВНО итог — короткий самодостаточный ответ обычным текстом без вызовов. "
    "Не пересказывай свои шаги: наружу уйдёт только твой финальный текст."
    "\n\n"
    "Инструменты тебе тоже доступны постепенно: сразу есть только tool_search и "
    "load_skill, остальное раскрывай через tool_search(query=...) по каталогу ниже."
)


def _msg_chars(m: dict) -> int:
    """Грубый размер одного сообщения в символах (прокси токенов)."""
    if m.get("tool_calls"):
        return len(json.dumps(m["tool_calls"], ensure_ascii=False))
    return len(m.get("content", "") or "")


def context_chars(messages: list[dict]) -> int:
    return sum(_msg_chars(m) for m in messages)


def _trim_old_tool_results(messages: list[dict]) -> int:
    """Уровень 1 — обрезка раздутых СТАРЫХ результатов инструментов на лету.

    Самое дешёвое и частое. Не трогаем системку и горячий хвост (KEEP_RECENT).
    Тело большого tool-результата заменяем заглушкой: данные не потеряны —
    при нужде агент просто перевызовет инструмент. Возвращаем, сколько обрезали.
    """
    trimmed = 0
    upper = len(messages) - KEEP_RECENT      # за хвост не лезем
    for i in range(1, max(1, upper)):        # messages[0] — система, её не трогаем
        m = messages[i]
        if m.get("role") != "tool":
            continue
        body = m.get("content", "") or ""
        if len(body) > TOOL_TRIM_OVER and not body.startswith("[свёрнуто"):
            m["content"] = f"[свёрнуто: результат инструмента, {len(body)} символов — перевызови инструмент при нужде]"
            trimmed += 1
    return trimmed


def _safe_cut(messages: list[dict]) -> int:
    """Индекс начала горячего хвоста так, чтобы не оторвать tool-ответ от его
    assistant-хода с tool_calls (иначе API упадёт). Сдвигаем границу вперёд,
    пока хвост начинается с role=='tool'."""
    cut = max(1, len(messages) - KEEP_RECENT)
    while cut < len(messages) and messages[cut].get("role") == "tool":
        cut += 1
    return cut


async def maybe_compact(messages: list[dict], quiet: bool) -> None:
    """Сжатие контекста по лесенке. Мутирует messages НА МЕСТЕ.

    Срабатывает только при заполнении выше порога. Сначала дешёвый уровень 1
    (обрезка старых tool-результатов); если этого мало — уровень 3 (полный
    compaction: один API-вызов пересказывает всё, кроме горячего хвоста).
    """
    before = context_chars(messages)
    if before < CONTEXT_WINDOW_CHARS * COMPACT_THRESHOLD:
        return                                  # места ещё хватает — не трогаем

    # --- Уровень 1: обрезать старые жирные результаты инструментов ---
    trimmed = _trim_old_tool_results(messages)
    if context_chars(messages) < CONTEXT_WINDOW_CHARS * COMPACT_THRESHOLD:
        if not quiet:
            ui.show_compaction("уровень 1: обрезка старых результатов",
                               before, context_chars(messages), detail=f"свёрнуто результатов: {trimmed}")
        return

    # --- Уровень 3: полный compaction ---
    cut = _safe_cut(messages)
    system_msg = messages[0]
    to_summarize = messages[1:cut]              # старое — на пересказ
    tail = messages[cut:]                        # горячий хвост — ДОСЛОВНО
    if not to_summarize:
        return                                   # сжимать нечего

    transcript = "\n".join(
        f"[{m.get('role')}] " + (
            json.dumps(m["tool_calls"], ensure_ascii=False) if m.get("tool_calls")
            else (m.get("content", "") or "")
        )
        for m in to_summarize
    )
    resp = await client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": COMPACT_SYSTEM},
            {"role": "user", "content": transcript},
        ],
    )
    summary = resp.choices[0].message.content or "(пустая сводка)"

    # Пересобираем контекст: система + сводка (как user) + горячий хвост дословно.
    messages[:] = [
        system_msg,
        {"role": "user", "content": "[Сводка предыдущего диалога]\n" + summary},
        *tail,
    ]
    if not quiet:
        ui.show_compaction("уровень 3: полный compaction",
                           before, context_chars(messages),
                           detail=f"сжато сообщений: {len(to_summarize)}, хвост дословно: {len(tail)}")


async def run_one_tool(call, revealed: set[str], exclude: tuple[str, ...], quiet: bool = False) -> dict:
    """Исполнить один tool_call и вернуть сообщение роли 'tool' для контекста.

    revealed — множество уже раскрытых инструментов (его мутирует tool_search).
    exclude  — что запрещено раскрывать (run_subagent у субагента).
    quiet=True — тихий режим (внутри субагента): не печатаем кухню на экран.
    """
    name = call.function.name
    try:
        args = json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError:
        args = {}

    # --- особый случай: tool_search. Мутирует состояние цикла (revealed). ---
    if name == "tool_search":
        query = args.get("query", "")
        found = search_catalog(query, exclude=exclude)
        revealed.update(found)        # со следующего витка их схемы попадут в tools[]
        if not quiet:
            ui.show_tool_search(query, found)
        result = ("Раскрыты инструменты: " + ", ".join(found) +
                  ". Теперь их можно вызывать.") if found else \
                 "Ничего не найдено — уточни запрос (или используй select:имя)."
        return {"role": "tool", "tool_call_id": call.id, "content": result}

    # --- особый случай: субагент. Его «тело» — рекурсивный цикл, не handler. ---
    if name == "run_subagent":
        task = args.get("task", "")
        if not quiet:
            ui.show_subagent_spawn(task)
        result = await run_subagent(task)
        if not quiet:
            ui.show_subagent_result(result)
        return {"role": "tool", "tool_call_id": call.id, "content": result}

    # План и навык показываем своими жанрами, а не как обычный вызов инструмента.
    if not quiet and name not in ("TodoWrite", "load_skill"):
        ui.show_tool_call(name, args)

    # --- шлюз разрешений (тема 15): оценка ПЕРЕД исполнением. -----------------
    # Только когда рядом человек (не quiet/субагент): ask требует его решения,
    # а субагент отвечать не может. deny/allow/default — чистая структура (regex).
    if not quiet:
        verdict = permissions.check_permission(name, args)
        subject = permissions._subject(name, args)
        if verdict.level == "deny":
            ui.show_permission_denied(name, subject, verdict.reason)
            return {"role": "tool", "tool_call_id": call.id,
                    "content": f"ЗАПРЕЩЕНО политикой: {verdict.reason}. Вызов не выполнен."}
        if verdict.level == "ask" and not ui.show_permission_ask(name, subject, verdict.reason):
            return {"role": "tool", "tool_call_id": call.id,
                    "content": (f"Пользователь ОТКЛОНИЛ вызов ({verdict.reason}). "
                                "Не выполнено — предложи другой путь или спроси, как быть.")}
        # allow / default / подтверждённый ask — проваливаемся к исполнению.

    # --- шина событий (тема 16): наблюдаемость ВНЕ цикла. --------------------
    # Цикл лишь объявляет события; что с ними делать — решают хуки снаружи.
    # pre_tool_use может ВЕРНУТЬ {"block": True} — это слой enforcement поверх
    # разрешений. В тихом режиме (субагент) события не шлём: хуки печатают на
    # экран, а субагент работает молча.
    if not quiet:
        pre = events.bus.emit("pre_tool_use", tool=name, args=args)
        blocked = next((r for r in pre if isinstance(r, dict) and r.get("block")), None)
        if blocked:
            reason = blocked.get("reason", "заблокировано хуком")
            ui.show_hook_block("pre_tool_use", name, reason)
            return {"role": "tool", "tool_call_id": call.id,
                    "content": f"Заблокировано хуком: {reason}. Вызов не выполнен."}

    tool = REGISTRY.get(name)
    if name.startswith("mcp__"):
        # MCP-инструмент (тема 21). Диспетчеризуется РОВНО так же, как встроенный
        # (те же разрешения, шина событий, UI выше/ниже) — единственная разница
        # здесь: «тело» это async-вызов на удалённый сервер, а не sync-handler
        # в потоке. Отсюда развилка по префиксу mcp__.
        try:
            result = await mcp_runtime.call_tool(name, args)
        except Exception as e:  # noqa: BLE001
            if not quiet:
                events.bus.emit("tool_error", tool=name, args=args, error=str(e))
            result = f"ОШИБКА при выполнении {name}: {e}"
        else:
            if not quiet:
                events.bus.emit("post_tool_use", tool=name, args=args, output=result)
    elif tool is None:
        result = f"ОШИБКА: неизвестный инструмент {name}"
    else:
        # Реальная функция синхронная — уводим в поток, чтобы не блокировать цикл.
        try:
            result = await asyncio.to_thread(tool.handler, **args)
        except Exception as e:  # noqa: BLE001
            if not quiet:
                events.bus.emit("tool_error", tool=name, args=args, error=str(e))
            result = f"ОШИБКА при выполнении {name}: {e}"
        else:
            if not quiet:
                events.bus.emit("post_tool_use", tool=name, args=args, output=result)

    if not quiet:
        if name == "TodoWrite":
            ui.show_plan(args.get("todos", []))
        elif name == "load_skill":
            ui.show_skill_load(args.get("name", ""), result)
        else:
            ui.show_tool_result(name, result)
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "content": result,
    }


async def execute_tool_calls(tool_calls, revealed: set[str], exclude: tuple[str, ...],
                             quiet: bool = False) -> list[dict]:
    """Исполнить все запрошенные инструменты одного хода.

    read-only -> параллельно (asyncio.gather), пишущие -> по очереди.
    Порядок результатов сохраняем как в запросе — это важно для API.
    """
    results: list[dict | None] = [None] * len(tool_calls)

    parallel_idx = [i for i, c in enumerate(tool_calls)
                    if REGISTRY.get(c.function.name) and REGISTRY[c.function.name].read_only]
    serial_idx = [i for i in range(len(tool_calls)) if i not in parallel_idx]

    # read-only — все разом
    if parallel_idx:
        done = await asyncio.gather(
            *(run_one_tool(tool_calls[i], revealed, exclude, quiet) for i in parallel_idx))
        for i, msg in zip(parallel_idx, done):
            results[i] = msg

    # пишущие — строго по очереди
    for i in serial_idx:
        results[i] = await run_one_tool(tool_calls[i], revealed, exclude, quiet)

    return results  # type: ignore[return-value]


def _to_ns(tc: dict) -> SimpleNamespace:
    """Обернуть tool_call-словарь в объект с атрибутами (.function.name и т.п.).

    Пути исполнения (execute_tool_calls, run_one_tool) читают tool_call через
    атрибуты — как у объекта из не-потокового API. В потоке мы собираем tool_calls
    руками в словари, поэтому здесь возвращаем им привычную «атрибутную» форму.
    В сам контекст (messages) при этом кладём именно словари — они и уходят в API,
    и сериализуются при сжатии.
    """
    fn = tc["function"]
    return SimpleNamespace(
        id=tc["id"],
        type=tc.get("type", "function"),
        function=SimpleNamespace(name=fn["name"], arguments=fn["arguments"]),
    )


async def _stream_model(messages: list[dict], tools: list, quiet: bool) -> dict:
    """Один вызов модели В ПОТОКЕ. Возвращает ход модели как словарь (текст +
    tool_calls) — той же структуры, что давал не-потоковый .message.

    Меняется ТОЛЬКО способ потребления ответа (тема 13): вместо готового объекта
    мы читаем дельты по мере генерации и печатаем видимый текст сразу (ощущение
    «соработника», а не пакетной задачи). Логика цикла и диспетчеризации — та же.

    Тонкость OpenAI-совместимого протокола (в отличие от Anthropic .stream()):
    tool_calls приходят КУСКАМИ в разных чанках. Их надо склеить по индексу
    (tc.index): имя и строку arguments накапливаем по слотам, id ловим где придёт.
    """
    stream = await client.chat.completions.create(
        model=MODEL,
        messages=messages,
        tools=tools,
        tool_choice="auto",
        stream=True,
        # Тема 20: без этого в стриме usage не приходит. С ним DeepSeek шлёт
        # финальный чанк (choices пустой) с usage, где есть поля кэша.
        stream_options={"include_usage": True},
    )

    content_parts: list[str] = []
    tool_acc: dict[int, dict] = {}      # index -> собираемый tool_call
    started = False                     # печатали ли уже заголовок потока
    usage = None                        # придёт в финальном чанке

    async for chunk in stream:
        # Финальный usage-чанк идёт БЕЗ choices — забираем кэш-метрику и дальше.
        if getattr(chunk, "usage", None):
            usage = chunk.usage
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta

        # видимый текст — печатаем токен за токеном сразу по приходу
        if delta.content:
            content_parts.append(delta.content)
            if not quiet:
                if not started:
                    ui.stream_start()
                    started = True
                ui.stream_delta(delta.content)

        # tool_calls — не печатаем, а СКЛЕИВАЕМ по индексу из фрагментов
        if delta.tool_calls:
            for tc in delta.tool_calls:
                slot = tool_acc.setdefault(
                    tc.index,
                    {"id": None, "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if tc.id:
                    slot["id"] = tc.id
                if tc.function and tc.function.name:
                    slot["function"]["name"] += tc.function.name
                if tc.function and tc.function.arguments:
                    slot["function"]["arguments"] += tc.function.arguments

    if not quiet and started:
        ui.stream_end()

    # Тема 20: делаем работу prompt-кэша видимой. Трекер общий на сессию;
    # печатаем HIT/MISS только в громком (родительском) цикле, чтобы не шуметь
    # из субагентов. Сам кэш — автоматический на стороне DeepSeek.
    if usage is not None:
        hit, miss = cache.stats.record(usage)
        if not quiet:
            ui.show_cache_turn(hit, miss, cache.stats.saved(hit))

    # Пересобираем ход модели в тот же словарь, что и раньше клали в контекст.
    msg: dict = {"role": "assistant"}
    content = "".join(content_parts)
    if content:
        msg["content"] = content
    if tool_acc:
        msg["tool_calls"] = [tool_acc[i] for i in sorted(tool_acc)]
    return msg


async def _run_loop(messages: list[dict], *, exclude: tuple[str, ...] = (),
                    quiet: bool, max_turns: int) -> str:
    """Общее тело цикла для родителя и субагента.

    Различие только в «громкости» (quiet) и в exclude (что субагенту нельзя).
    Структура витка одна и та же — субагент = тот же луп.

    Постепенное раскрытие: набор инструментов в tools[] НЕ статичен. Каждый
    виток он = мета-инструменты + всё, что модель уже раскрыла через tool_search
    (revealed). Так в контексте/запросе нет схем тех тулов, что пока не нужны.
    """
    revealed: set[str] = set()   # имена раскрытых инструментов, растёт по ходу
    for turn in range(1, max_turns + 1):
        # h2A (тема 8): в начале каждого хода вычитываем ГОТОВЫЕ фоновые
        # уведомления и вставляем их как user-сообщения — так завершившаяся
        # фоновая команда попадает в контекст на ближайшей границе хода.
        # Фон — забота главного цикла; субагент (quiet) очередь не трогает.
        if not quiet:
            for note in background.drain():
                messages.append({"role": "user", "content": note})
                ui.show_background_note(note)

        # Steering-очередь h2A (тема 19), точка проверки №1 — начало витка.
        # Прерывание, поставленное по Ctrl+C, вычитываем ТОЛЬКО здесь (и перед
        # инструментами) — так у messages единственный писатель — сам цикл, и
        # прерывание вклеивается на чистой границе. Субагент (quiet) канал не трогает.
        if not quiet:
            for note in steering.drain():
                messages.append({"role": "user", "content": note})
                ui.show_interrupt(note)

        # Перед вызовом модели проверяем заполнение окна и при нужде сжимаем.
        await maybe_compact(messages, quiet)

        if not quiet:
            ui.turn_header(turn)
            ui.show_context(messages)   # показываем РОВНО то, что уходит модели

        # tools[] собираем заново: мета (всегда) + раскрытое. exclude отрезает запретное.
        tools = spec_for(list(META_TOOLS) + sorted(revealed), exclude=exclude)

        # Потоковая выдача (тема 13): токены печатаем по мере генерации.
        # Меняется только СПОСОБ потребления ответа — структура витка та же:
        # собираем ход модели (текст + tool_calls) и решаем выход по структуре.
        msg = await _stream_model(messages, tools, quiet)

        # Сохраняем ход модели целиком (текст + tool_calls) в контекст.
        messages.append(msg)

        tool_calls = msg.get("tool_calls")

        # РЕШЕНИЕ О ВЫХОДЕ — по структуре, не по смыслу:
        if not tool_calls:
            # Модель хочет закончить. Но если фоновая задача ещё крутится —
            # не выходим и не теряем её результат: делать больше нечего (тот
            # самый случай «фон бесполезен, когда нет независимой работы»),
            # поэтому честно ждём итог, вливаем его и даём модели отреагировать.
            if not quiet and background.has_pending():
                note = background.wait_one(timeout=600)
                if note:
                    messages.append({"role": "user", "content": note})
                    ui.show_background_note(note)
                continue
            return msg.get("content", "") or ""

        # Собранные в потоке словари-tool_calls оборачиваем в атрибутную форму
        # для путей исполнения (они читают .function.name и т.п.).
        calls = [_to_ns(tc) for tc in tool_calls]

        # Steering-очередь h2A (тема 19), точка проверки №2 — ПОСЛЕ ответа модели,
        # но ДО запуска инструментов. Прерывание, пришедшее пока модель стримила
        # ответ, ловим здесь — иначе инструменты (возможно, ПИШУЩИЕ) успели бы
        # выполниться до следующей проверки. Сами инструменты не запускаем.
        #
        # Тонкость нашего API (в отличие от статьи, где просто append+continue):
        # на КАЖДЫЙ tool_call обязателен tool-ответ, иначе следующий вызов упадёт.
        # Поэтому отменяемые инструменты не бросаем, а помечаем невыполненными —
        # контракт цел, а модель на новом витке увидит прерывание и остановится.
        if not quiet:
            interrupts = steering.drain()
            if interrupts:
                for c in calls:
                    messages.append({"role": "tool", "tool_call_id": c.id,
                                     "content": "Прервано пользователем — инструмент не выполнен."})
                for note in interrupts:
                    messages.append({"role": "user", "content": note})
                    ui.show_interrupt(note)
                continue

        tool_messages = await execute_tool_calls(calls, revealed, exclude, quiet)
        messages.extend(tool_messages)

    return "(достигнут лимит витков)"


async def run_subagent(task: str, max_turns: int = 15) -> str:
    """Субагент: тот же мастер-луп, но со СВЕЖИМ изолированным контекстом.

    * messages[] свой — общего состояния с родителем нет, единственный мостик
      это task, прилетевший аргументом и ставший user-сообщением.
    * крутится тихо (quiet=True) — наружу не льём его внутренние шаги.
    * run_subagent ему НЕ даём (exclude) — иначе бесконечный спавн.
    * наружу возвращаем только финальный текст -> он сядет результатом tool
      в контекст родителя. Все промежуточные шаги умрут вместе с sub_messages.
    """
    sub_messages: list[dict] = [
        {"role": "system",
         "content": build_system_prompt(SUBAGENT_SYSTEM_PROMPT, exclude=("run_subagent",))},
        {"role": "user", "content": task},
    ]
    return await _run_loop(
        sub_messages,
        exclude=("run_subagent",),   # найти и раскрыть run_subagent субагент не сможет
        quiet=True,
        max_turns=max_turns,
    )


def new_session() -> list[dict]:
    """Свежий контекст сессии: системный промпт + напоминания восстановления.

    Это и есть «начало истории». Дальше весь диалог копится в ЭТОМ списке —
    он и есть сессия (тема 17). Сохранить сессию = сериализовать этот список.
    """
    messages: list[dict] = [
        {"role": "system", "content": build_system_prompt(SYSTEM_PROMPT)},
    ]
    # Восстановление при старте: код читает .agent_tasks.json и, если остался
    # незавершённый граф, вливает его состояние в контекст — продолжаем с места
    # обрыва, а не с нуля (см. конспект, тема 7).
    note = task_graph.restore_note()
    if note:
        messages.append({"role": "user", "content": note})
    # Постоянные товарищи тоже переживают рестарт: если с прошлой сессии остались
    # незавершённые — код напоминает их состояние (см. конспект, темы 9 и 10).
    team_note = team.restore_note()
    if team_note:
        messages.append({"role": "user", "content": team_note})
    return messages


async def master_loop(messages: list[dict], user_input: str, max_turns: int = 20) -> str:
    """Прогнать один ход диалога поверх ЖИВОЙ сессии.

    messages — постоянный контекст сессии (растёт между вводами REPL). Мы лишь
    дописываем в него реплику пользователя и крутим цикл; _run_loop мутирует
    список на месте. Так история копится — и именно её сохраняет /save.
    """
    messages.append({"role": "user", "content": user_input})
    result = await _run_loop(
        messages,
        exclude=(),
        quiet=False,
        max_turns=max_turns,
    )
    ui.show_final(result)
    return result


# --- REPL-команды над сессией (тема 17) -------------------------------------
# Работают НАД разговором целиком, поэтому это команды снаружи цикла, а не tools
# модели. Возвращают (messages, sid): при resume/branch они подменяют живой
# контекст и текущую ветку. Цикл об этом не знает — крутит один messages.

def _handle_command(cmd: str, messages: list[dict], sid: str | None):
    """Обработать служебную строку (начинается с '/'). Вернуть (messages, sid)."""
    parts = cmd.split()
    name, arg = parts[0], (parts[1] if len(parts) > 1 else None)

    if name == "/save":
        sid = sid or sessions.new_id()          # первое сохранение — заводим id ветки
        path = sessions.save(messages, sid)
        ui.show_session_saved(sid, str(path), len(messages))
        return messages, sid

    if name == "/resume":
        if not arg:
            ui.show_session_list(sessions.list_ids(), current=sid)
            ui.show_session_note("укажи id: /resume <id>")
            return messages, sid
        if not sessions.exists(arg):
            ui.show_session_note(f"нет такой сессии: {arg}", ok=False)
            return messages, sid
        loaded = sessions.load(arg)              # грузим файл ОБРАТНО в контекст
        ui.show_session_loaded(arg, len(loaded))
        return loaded, arg                       # текущая ветка = та, что продолжили

    if name == "/branch":
        # Ветвление: берём состояние (из файла arg или текущее живое) и дальше
        # пишем под НОВЫМ id — исходная ветка остаётся нетронутой (дерево, не линия).
        if arg:
            if not sessions.exists(arg):
                ui.show_session_note(f"нет такой сессии: {arg}", ok=False)
                return messages, sid
            messages = sessions.load(arg)
        new_sid = sessions.new_id()
        sessions.save(messages, new_sid)         # фиксируем точку ветвления под новым id
        ui.show_session_loaded(arg or (sid or "живая сессия"), len(messages),
                               branched_to=new_sid)
        return messages, new_sid

    if name in ("/sessions", "/ls"):
        ui.show_session_list(sessions.list_ids(), current=sid)
        return messages, sid

    if name == "/help":
        ui.show_commands_help()
        return messages, sid

    ui.show_session_note(f"неизвестная команда {name}", ok=False)
    ui.show_commands_help()
    return messages, sid


def _sigint_to_steering(signum, frame) -> None:
    """Обработчик SIGINT на время работы агента (тема 19). Выполняется в главной
    нити на безопасной границе байткода — не аппаратный контекст, поэтому класть
    в очередь и печатать здесь можно. Исключение НЕ бросаем: процесс не падает,
    а Ctrl+C превращается в прерывание, которое цикл вычитает сам."""
    steering.interrupt()
    ui.show_interrupt_queued()


async def repl() -> None:
    ui.banner()
    events.bus.emit("session_start")          # тема 16: граница сессии
    # Тема 21: подключаем MCP-серверы ДО new_session — их инструменты
    # регистрируются в REGISTRY и должны попасть в каталог системного промпта.
    await mcp_runtime.connect_all()
    messages = new_session()                  # живой контекст на всю сессию
    sid: str | None = None                    # текущая ветка (None — ещё не сохраняли)
    try:
        while True:
            try:
                user_input = ui.prompt()
            except (EOFError, KeyboardInterrupt):
                break
            if not user_input:
                break
            if user_input.startswith("/"):    # служебная строка — НЕ в модель
                messages, sid = _handle_command(user_input, messages, sid)
                continue

            # Ctrl+C во время работы агента НЕ должен ронять процесс — он должен
            # стать прерыванием (тема 19). На Windows asyncio.run сам ловит SIGINT,
            # отменяет главную задачу и заново поднимает KeyboardInterrupt на уровне
            # раннера — до try/except внутри repl он не доходит. Поэтому на время
            # работы агента ставим СВОЙ обработчик сигнала: он не бросает исключение,
            # а лишь кладёт прерывание в steering-очередь. Цикл вычитает его в
            # ближайшей безопасной точке, остановится и суммирует. Вне работы агента
            # (в prompt) возвращаем стандартный обработчик — там Ctrl+C = выход.
            prev = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, _sigint_to_steering)
            try:
                await master_loop(messages, user_input)
            finally:
                signal.signal(signal.SIGINT, prev)
    finally:
        # Итог по prompt-кэшу за сессию (тема 20) — как print_summary в статье.
        ui.show_cache_summary(cache.stats.calls, cache.stats.hit,
                              cache.stats.miss, cache.stats.saved(cache.stats.hit))
        await mcp_runtime.disconnect_all()    # тема 21: закрыть сессии/процессы серверов
        events.bus.emit("session_end")        # в finally — сработает даже при вылете


if __name__ == "__main__":
    # Ctrl+C в приглашении Ты> — это выход. Мы ловим его внутри repl (break +
    # чистый session_end), но asyncio.Runner вешает СВОЙ обработчик SIGINT,
    # который вдобавок отменяет главную задачу и поверх чистого выхода заново
    # поднимает KeyboardInterrupt на уровне раннера. Глотаем его здесь, чтобы
    # выход был тихим. (Ctrl+C ВО ВРЕМЯ работы агента сюда не доходит — там свой
    # обработчик _sigint_to_steering, он исключение не бросает.)
    try:
        asyncio.run(repl())
    except KeyboardInterrupt:
        pass
