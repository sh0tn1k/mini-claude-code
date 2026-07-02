"""
Интеграция с MCP-серверами (тема 21).

Идея, которую воплощаем: внешний MCP-совместимый сервер прозрачно РАСШИРЯЕТ
реестр инструментов агента. Сервер файловой системы даёт файловые инструменты,
git-сервер — операции git и т.д. Для модели вызов такого инструмента неотличим
от вызова bash или grep — та же структура tool call, та же вставка результата,
тот же цикл. Префикс mcp__<server>__<tool> — лишь деталь МАРШРУТИЗАЦИИ.

Как заводится (ровно как в статье):
  * читаем config/mcp_config.yaml;
  * при старте подключаемся к каждому серверу через официальный Python SDK MCP
    (транспорт stdio: поднимается процесс, ClientSession, initialize());
  * вызываем list_tools(), чтобы ОБНАРУЖИТЬ предоставляемые инструменты;
  * регистрируем каждый в ОБЩЕМ tools.REGISTRY под именем mcp__<server>__<tool>,
    рядом со встроенными. Так они автоматически попадают в каталог, ищутся через
    tool_search и отдаются модели через spec_for — без правок агентного цикла.

Что НЕ так, как у встроенных: «тело» MCP-инструмента — это async-вызов на
удалённый сервер (session.call_tool), а не локальная sync-функция. Поэтому в
run_one_tool при исполнении делается единственная развилка — проверка префикса
mcp__ (см. agent.py). Всё остальное — разрешения, шина событий, UI — идентично.

MCP — необязательная зависимость: если нет config-файла или не установлены
пакеты (mcp, pyyaml), агент просто работает без MCP-серверов.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from pathlib import Path

import tools
import ui

# Живое состояние подключений на всю сессию.
_stack: AsyncExitStack | None = None          # держит открытыми процессы/сессии
_sessions: dict[str, object] = {}             # server -> ClientSession
_tool_map: dict[str, tuple[str, str]] = {}    # mcp__srv__tool -> (server, tool)

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "mcp_config.yaml"


def _mcp_placeholder(**_: object) -> str:
    """Заглушка-handler: MCP-инструмент исполняется циклом через await
    call_tool(...), а не как обычная sync-функция. В REGISTRY он нужен ради
    схемы (для модели) и флага read_only (для диспетчера); реальный вызов
    перехватывается в run_one_tool по префиксу mcp__."""
    return "ОШИБКА: MCP-инструмент должен исполняться циклом (await), а не напрямую"


async def connect_all() -> int:
    """Подключиться ко всем серверам из конфига и зарегистрировать их
    инструменты в общем REGISTRY. Возвращает число зарегистрированных
    инструментов. При любой нехватке (нет конфига/пакетов) — тихо 0."""
    global _stack

    if not _CONFIG_PATH.exists():
        return 0  # MCP не настроен — работаем без серверов, это норма

    try:
        import yaml
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as e:
        ui.show_mcp_note(f"MCP выключен: нет зависимости ({e}). "
                         f"Установи: pip install mcp pyyaml", ok=False)
        return 0

    with open(_CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    _stack = AsyncExitStack()
    count = 0
    for srv in (config.get("servers") or []):
        name = srv.get("name", "unknown")
        transport = srv.get("transport", "stdio")
        if transport != "stdio":
            ui.show_mcp_note(f"[{name}] транспорт '{transport}' не поддержан — пропуск", ok=False)
            continue
        try:
            params = StdioServerParameters(
                command=srv["command"],
                args=srv.get("args", []),
            )
            # enter_async_context держит процесс сервера и сессию открытыми до
            # disconnect_all() — закрываем их одним aclose() в конце сессии.
            read, write = await _stack.enter_async_context(stdio_client(params))
            session = await _stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            tool_list = await session.list_tools()
        except Exception as e:  # noqa: BLE001 — один битый сервер не должен ронять старт
            ui.show_mcp_note(f"[{name}] не удалось подключиться: {e}", ok=False)
            continue

        _sessions[name] = session
        for t in tool_list.tools:
            prefixed = f"mcp__{name}__{t.name}"
            _tool_map[prefixed] = (name, t.name)
            # Регистрируем в ОБЩИЙ реестр — дальше он ничем не отличается от
            # встроенного: попадёт в каталог, найдётся через tool_search,
            # уедет модели через spec_for. read_only=False (серверный инструмент
            # может писать — гоняем по очереди, консервативно и безопасно).
            tools.register(tools.Tool(
                name=prefixed,
                description=f"[{name}] {t.description or t.name}",
                parameters=t.inputSchema or {"type": "object", "properties": {}},
                handler=_mcp_placeholder,
                read_only=False,
            ))
            count += 1
        ui.show_mcp_note(f"подключён: {name} ({len(tool_list.tools)} инстр.)")

    return count


async def call_tool(prefixed_name: str, arguments: dict) -> str:
    """Направить вызов MCP-инструмента на нужный сервер и вернуть результат
    строкой (как и у встроенных — чтобы лечь в контекст без спецобработки)."""
    if prefixed_name not in _tool_map:
        return f"ОШИБКА: MCP-инструмент не найден: {prefixed_name}"
    server, tool_name = _tool_map[prefixed_name]
    session = _sessions.get(server)
    if session is None:
        return f"ОШИБКА: сервер не подключён: {server}"

    result = await session.call_tool(tool_name, arguments)
    parts: list[str] = []
    for item in (getattr(result, "content", None) or []):
        if hasattr(item, "text"):
            parts.append(item.text)
        elif hasattr(item, "data"):
            parts.append(f"[бинарные данные: {len(item.data)} байт]")
    return "\n".join(parts)[:50000] or "(нет вывода)"


async def disconnect_all() -> None:
    """Закрыть все сессии и процессы серверов. Зовём в finally repl —
    закрываем ТЕМ ЖЕ тас­ком, что открывал (иначе anyio ругается на cancel scope)."""
    global _stack
    if _stack is not None:
        try:
            await _stack.aclose()
        except Exception:  # noqa: BLE001 — на выходе из сессии глушим шум закрытия
            pass
        _stack = None
    _sessions.clear()
    _tool_map.clear()
