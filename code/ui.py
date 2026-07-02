"""
Отрисовка консоли через rich.

Каждый визуальный блок — это отдельный "жанр" информации, чтобы взглядом
не путать одно с другим:

  • контекст → модели   — таблица (что лежит в messages перед вызовом)
  • 💭 мысли модели      — голубая панель
  • 🔧 вызов инструмента — жёлтая строка (имя + аргументы)
  • 📄 результат          — серая панель (обрезаем длинное)
  • ✅ финальный ответ    — зелёная рамка

Размеры считаем в символах (грубый прокси токенов) — чтобы видеть,
как контекст пухнет от витка к витку.
"""

from __future__ import annotations

import json
import sys

# Windows-консоль по умолчанию может быть в cp1251 и падать на → и эмодзи.
# Принудительно переводим вывод в UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

console = Console()

_MAX_RESULT_LINES = 12   # сколько строк результата показывать
_MAX_PREVIEW = 60        # длина превью в таблице контекста


def _short(s: str, n: int = _MAX_PREVIEW) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def turn_header(turn: int) -> None:
    console.print()
    console.print(Rule(f"[bold white]виток {turn}[/]", style="grey42"))


def show_context(messages: list[dict]) -> None:
    """Компактная таблица того, что уходит модели на этом витке."""
    table = Table(
        title="контекст → модели",
        title_style="bold grey70",
        title_justify="left",
        show_edge=False,
        pad_edge=False,
        expand=False,
    )
    table.add_column("#", style="grey50", width=3, justify="right")
    table.add_column("роль", width=10)
    table.add_column("превью", overflow="fold")
    table.add_column("симв.", style="grey50", justify="right", width=6)

    role_color = {
        "system": "magenta",
        "user": "cyan",
        "assistant": "green",
        "tool": "yellow",
    }

    total = 0
    for i, m in enumerate(messages):
        role = m.get("role", "?")
        # собираем человекочитаемое превью под каждый тип сообщения
        if m.get("tool_calls"):
            calls = ", ".join(
                f"{c['function']['name']}({_short(c['function']['arguments'], 30)})"
                for c in m["tool_calls"]
            )
            preview = f"[italic]вызовы:[/] {calls}"
            size = len(json.dumps(m["tool_calls"]))
        elif role == "tool":
            preview = f"[italic]результат:[/] {_short(m.get('content', ''))}"
            size = len(m.get("content", "") or "")
        else:
            preview = _short(m.get("content", ""))
            size = len(m.get("content", "") or "")
        total += size
        table.add_row(str(i), f"[{role_color.get(role,'white')}]{role}[/]", preview, str(size))

    console.print(table)
    console.print(
        Text(f"  итого в контексте: {len(messages)} сообщ. ≈ {total} символов", style="grey42")
    )


def show_thoughts(text: str) -> None:
    console.print(Panel(text, title="💭 мысли модели", title_align="left",
                        border_style="bright_blue", padding=(0, 1)))


def stream_start() -> None:
    """Начало потоковой выдачи текста модели (тема 13): печатаем заголовок один
    раз, перед первым видимым токеном. Дальше stream_delta льёт токены подряд."""
    console.print()
    console.print(Text("💭 мысли модели (поток):", style="bright_blue"))


def stream_delta(text: str) -> None:
    """Один кусок текста, пришедший из потока. Печатаем сразу, без перевода строки.

    markup/highlight выключены: в токенах бывают '[' и ']', их нельзя трактовать
    как rich-разметку; soft_wrap — чтобы длинные строки не рвались по колонкам."""
    console.print(text, end="", markup=False, highlight=False, soft_wrap=True)


def stream_end() -> None:
    """Финальный перевод строки после того, как поток видимого текста иссяк."""
    console.print()


def show_cache_turn(hit: int, miss: int, saved: int) -> None:
    """Одна приглушённая строка про кэш на этом ходу (тема 20). MISS — префикс
    считался/писался заново; HIT — обслужен из кэша, показываем экономию."""
    if hit > 0:
        console.print(
            f"  [grey50]⛃ cache HIT → {hit} токенов из кэша "
            f"(сэкономлено ≈{saved})[/]")
    elif miss > 0:
        console.print(f"  [grey50]⛃ cache MISS → {miss} токенов посчитано заново[/]")


def show_cache_summary(calls: int, hit: int, miss: int, saved: int) -> None:
    """Итог по кэшу за сессию — печатаем в session_end."""
    if not calls:
        return
    console.print(
        f"  [grey42]⛃ кэш за сессию: {calls} вызов(ов) · "
        f"из кэша={hit} · заново={miss} · экономия ≈{saved} токенов[/]")


def show_permission_denied(name: str, subject: str, reason: str) -> None:
    """Шлюз разрешений заблокировал вызов наглухо (always_deny) — отдельный жанр.

    Красная рамка, чтобы взгляд сразу отличал «политика запретила» от обычного
    результата инструмента. Модель этот текст увидит как результат и подстроится.
    """
    body = (f"[bold]🚫 {name}[/]  {_short(subject, 70)}\n"
            f"[italic]причина:[/] {reason}")
    console.print(Panel(body, title="⛔ разрешения · ЗАПРЕЩЕНО (always_deny)",
                        title_align="left", border_style="bold red", padding=(0, 1)))


def show_permission_ask(name: str, subject: str, reason: str) -> bool:
    """Шлюз требует подтверждения человека (ask_user) — отдельный жанр + пауза.

    Оранжевая рамка + запрос [y/N]. Само РЕШЕНИЕ принимает человек — это тот
    самый шов «структура ставит на паузу, смысл решает наверху»."""
    body = (f"[bold]⏸ {name}[/]  {_short(subject, 70)}\n"
            f"[italic]причина:[/] {reason}")
    console.print(Panel(body, title="🔐 разрешения · нужно подтверждение (ask_user)",
                        title_align="left", border_style="bold orange3", padding=(0, 1)))
    try:
        ans = console.input("   Разрешить? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = "n"
    return ans in ("y", "yes", "д", "да")


def show_hook_fired(event: str, detail: str) -> None:
    """Хук сработал на событии жизненного цикла (тема 16) — отдельный жанр.

    Тусклая одна строка: наблюдаемость идёт ПАРАЛЛЕЛЬНО работе агента, не
    мешая читать основной поток. Цикл лишь объявил событие; среагировал хук.
    """
    console.print(Text.assemble(
        ("  ⚡ hook ", "grey50"),
        (event, "bold grey62"),
        (f"  {_short(detail, 60)}" if detail else "", "grey50"),
    ))


def show_hook_error(event: str, err: str) -> None:
    """Хук упал — но агента не роняем (emit глотает ошибку). Показываем тускло."""
    console.print(Text(f"  ⚡ hook error on '{event}': {_short(err, 80)}", style="red3"))


def show_hook_block(event: str, tool: str, reason: str) -> None:
    """Хук pre_tool_use вернул block=True — enforcement поверх разрешений."""
    body = f"[bold]⛔ {tool}[/]\n[italic]хук заблокировал на[/] {event}: {reason}"
    console.print(Panel(body, title="⚡ хук · вызов ЗАБЛОКИРОВАН (block=True)",
                        title_align="left", border_style="bold red", padding=(0, 1)))


def show_tool_call(name: str, args: dict) -> None:
    arg_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
    console.print(Text.assemble(
        ("  🔧 ", "bold yellow"),
        (name, "bold yellow"),
        (f"({arg_str})", "yellow"),
    ))


def show_tool_result(name: str, result: str) -> None:
    lines = (result or "").splitlines()
    shown = "\n".join(lines[:_MAX_RESULT_LINES])
    if len(lines) > _MAX_RESULT_LINES:
        shown += f"\n[grey50]… ещё {len(lines) - _MAX_RESULT_LINES} строк[/]"
    console.print(Panel(shown or "(пусто)", title=f"📄 результат · {name}",
                        title_align="left", border_style="grey42", padding=(0, 1)))


def show_plan(todos: list) -> None:
    """План (TodoWrite) — отдельный жанр, не как обычный результат инструмента."""
    mark = {"pending": "○", "in_progress": "▸", "done": "✓"}
    style = {"pending": "grey70", "in_progress": "bold yellow", "done": "green"}
    lines = []
    for t in todos:
        st = t.get("status", "pending")
        content = t.get("content", "")
        if st == "done":
            content = f"[strike]{content}[/strike]"
        lines.append(f"[{style.get(st, 'white')}]{mark.get(st, '[ ]')} {content}[/]")
    body = "\n".join(lines) or "(пусто)"
    console.print(Panel(body, title="📋 план (TodoWrite)", title_align="left",
                        border_style="magenta", padding=(0, 1)))


def show_subagent_spawn(task: str) -> None:
    """Спавн субагента — отдельный жанр. Показываем ТОЛЬКО задачу, что ему отдали.

    Внутреннюю работу субагента намеренно не показываем (он крутится «тихо»):
    его смысл — изоляция, родителя интересует лишь итог, а не процесс.
    """
    console.print(Panel(task, title="🤖 спавн субагента · задача", title_align="left",
                        border_style="cyan", padding=(0, 1)))


def show_subagent_result(result: str) -> None:
    """Ответ субагента — то единственное, что уходит в контекст основной модели."""
    lines = (result or "").splitlines()
    shown = "\n".join(lines[:_MAX_RESULT_LINES])
    if len(lines) > _MAX_RESULT_LINES:
        shown += f"\n[grey50]… ещё {len(lines) - _MAX_RESULT_LINES} строк[/]"
    console.print(Panel(shown or "(пусто)", title="🤖 ответ субагента → основной модели",
                        title_align="left", border_style="bold cyan", padding=(0, 1)))


def show_tool_search(query: str, found: list[str]) -> None:
    """Раскрытие инструментов (tool_search) — отдельный жанр.

    Показываем запрос и что именно раскрылось: эти схемы появятся в tools[]
    со следующего витка (тот самый «+1 раунд» постепенного раскрытия).
    """
    body = f"[italic]запрос:[/] {query}\n[italic]раскрыто:[/] " + (
        ", ".join(found) if found else "[grey50](ничего не найдено)[/]")
    console.print(Panel(body, title="🔎 раскрытие инструментов (tool_search)",
                        title_align="left", border_style="orange3", padding=(0, 1)))


def show_skill_load(name: str, body: str) -> None:
    """Подгрузка навыка (load_skill) — отдельный жанр (текст-инструкция в контекст)."""
    lines = (body or "").splitlines()
    shown = "\n".join(lines[:_MAX_RESULT_LINES])
    if len(lines) > _MAX_RESULT_LINES:
        shown += f"\n[grey50]… ещё {len(lines) - _MAX_RESULT_LINES} строк[/]"
    console.print(Panel(shown or "(пусто)", title=f"📖 навык подгружен · {name}",
                        title_align="left", border_style="bright_magenta", padding=(0, 1)))


def show_compaction(level: str, before: int, after: int, detail: str = "") -> None:
    """Сжатие контекста — отдельный жанр. Видно, на сколько ужались (символы ≈ токены)."""
    saved = before - after
    pct = (saved / before * 100) if before else 0
    body = (f"[italic]уровень:[/] {level}\n"
            f"[italic]было:[/] {before} симв.  →  [italic]стало:[/] {after} симв.  "
            f"([bold]−{saved}[/], −{pct:.0f}%)")
    if detail:
        body += f"\n[grey50]{detail}[/]"
    console.print(Panel(body, title="🗜 сжатие контекста (compaction)",
                        title_align="left", border_style="red3", padding=(0, 1)))


def show_background_note(note: str) -> None:
    """Фоновое уведомление (h2A) — отдельный жанр. Пришло из очереди готовых
    задач и вставлено в контекст как user-сообщение на границе хода."""
    lines = (note or "").splitlines()
    shown = "\n".join(lines[:_MAX_RESULT_LINES])
    if len(lines) > _MAX_RESULT_LINES:
        shown += f"\n[grey50]… ещё {len(lines) - _MAX_RESULT_LINES} строк[/]"
    console.print(Panel(shown or "(пусто)", title="⏳ фоновая задача завершилась → в контекст",
                        title_align="left", border_style="bright_yellow", padding=(0, 1)))


def show_interrupt(note: str) -> None:
    """Прерывание (тема 19), вычитанное из steering-очереди и вставленное в
    контекст как user-сообщение на безопасной границе хода — отдельный жанр.
    Красная рамка: это перенаправление посреди задачи, не обычная реплика."""
    lines = (note or "").splitlines()
    shown = "\n".join(lines[:_MAX_RESULT_LINES])
    if len(lines) > _MAX_RESULT_LINES:
        shown += f"\n[grey50]… ещё {len(lines) - _MAX_RESULT_LINES} строк[/]"
    console.print(Panel(shown or "(пусто)", title="⎋ прерывание → в контекст",
                        title_align="left", border_style="bold red", padding=(0, 1)))


def show_interrupt_queued() -> None:
    """Ctrl+C во время работы агента: прерывание поставлено в очередь, агент
    остановится на ближайшей чистой границе (не роняем его прямо сейчас)."""
    console.print(Text(
        "\n  ⎋ прерывание поставлено в очередь — агент остановится после текущего шага",
        style="bold red3"))


def _session_header(sid: str | None) -> Text:
    """Строка-статус текущей ветки — общий верх для всех сессионных выводов."""
    if sid:
        return Text.assemble(("  ветка: ", "grey50"), (sid, "bold bright_cyan"))
    return Text("  ветка: (не сохранена)", style="grey50")


def show_session_saved(sid: str, path: str, n: int) -> None:
    """Сессия сохранена (тема 17) — отдельный жанр, не путать с результатом тула."""
    body = Text.assemble(
        ("id ветки  ", "grey50"), (sid + "\n", "bold"),
        ("сообщений ", "grey50"), (f"{n}\n", "white"),
        ("файл      ", "grey50"), (path, "grey62"),
    )
    console.print(Panel(body, title="💾 сессия сохранена", title_align="left",
                        border_style="bright_blue", padding=(0, 1)))


def show_session_loaded(sid: str, n: int, branched_to: str | None = None) -> None:
    if branched_to:
        body = Text.assemble(
            ("взято из  ", "grey50"), (f"{sid} · {n} сообщ.\n", "white"),
            ("новая ветка ", "grey50"), (branched_to + "\n", "bold bright_magenta"),
            ("исходная ветка нетронута — история стала деревом", "italic grey62"),
        )
        console.print(Panel(body, title="🌿 сессия · ветвление (branch)", title_align="left",
                            border_style="bright_magenta", padding=(0, 1)))
    else:
        body = Text.assemble(
            ("ветка     ", "grey50"), (f"{sid}\n", "bold"),
            ("загружено ", "grey50"), (f"{n} сообщ. в живой контекст", "white"),
        )
        console.print(Panel(body, title="↩ сессия продолжена (resume)", title_align="left",
                            border_style="green", padding=(0, 1)))


def show_session_list(items: list, current: str | None = None) -> None:
    """Список сохранённых сессий таблицей; текущая ветка помечена ▸."""
    if not items:
        console.print(Panel("[grey50](сохранённых сессий пока нет — сделай /save)[/]",
                            title="🗂 сохранённые сессии", title_align="left",
                            border_style="grey42", padding=(0, 1)))
        return
    table = Table(show_edge=False, pad_edge=False, expand=False, box=None)
    table.add_column("", width=2)                       # маркер текущей ветки
    table.add_column("id ветки", style="bold")
    table.add_column("сообщ.", style="grey62", justify="right")
    for sid, n in items:
        mark = "[bright_cyan]▸[/]" if sid == current else " "
        style = "bright_cyan" if sid == current else "white"
        table.add_row(mark, f"[{style}]{sid}[/]", str(n) if n >= 0 else "[red3]?[/]")
    console.print(Panel(table, title="🗂 сохранённые сессии", title_align="left",
                        border_style="grey42", padding=(0, 1)))


def show_session_note(msg: str, ok: bool = True) -> None:
    """Короткая служебная реплика REPL-команды (ошибка/подсказка)."""
    style = "grey62" if ok else "red3"
    console.print(Text(f"  · {msg}", style=style))


def show_mcp_note(msg: str, ok: bool = True) -> None:
    """Строка о подключении MCP-сервера на старте (тема 21)."""
    style = "grey58" if ok else "orange3"
    console.print(Text(f"  ⛓ MCP: {msg}", style=style))


def show_commands_help() -> None:
    """Справка по REPL-командам — операции НАД сессией, живут вне цикла модели."""
    rows = [
        ("/save",         "сохранить текущую ветку на диск"),
        ("/resume [id]",  "загрузить сессию (без id — список)"),
        ("/branch [id]",  "ответвиться: дальше писать под новым id, исходная цела"),
        ("/sessions, /ls", "показать сохранённые сессии"),
        ("/help",         "эта справка"),
    ]
    table = Table(show_edge=False, pad_edge=False, expand=False, box=None)
    table.add_column("команда", style="bold bright_cyan", no_wrap=True)
    table.add_column("что делает", style="grey70")
    for cmd, desc in rows:
        table.add_row(cmd, desc)
    console.print(Panel(table, title="⌨ команды сессии (не уходят в модель)",
                        title_align="left", border_style="cyan", padding=(0, 1)))


def show_final(text: str) -> None:
    console.print()
    console.print(Panel(text, title="✅ финальный ответ", title_align="left",
                        border_style="bold green", padding=(1, 2)))


def banner() -> None:
    console.print(Panel.fit(
        "[bold]Агент на DeepSeek[/]\nпустая строка — выход · Ctrl+C во время работы — прервать и перенаправить",
        border_style="green"))


def prompt() -> str:
    return console.input("\n[bold cyan]Ты>[/] ").strip()
