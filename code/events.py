"""
Шина событий и хуки жизненного цикла (тема 16).

Идея статьи: наблюдаемость — СТРУКТУРНОЕ свойство обвязки, а не заплатка
«после факта». Агентный цикл НЕ знает про логи, аудит и таймеры. Он лишь
объявляет вслух каждый значимый момент именованным событием (emit). Кто и как
на это реагирует — забота хуков, подключённых СНАРУЖИ (bus.on), без правки цикла.

Тот же повторяющийся принцип: механизм зашит в код (цикл + шина), а поведение
подключается снаружи (хуки) — как политика разрешений (тема 15) живёт в конфиге.

События, которые генерирует наш цикл:
  session_start / session_end  — границы сессии
  pre_tool_use                 — ПЕРЕД вызовом (хук может вернуть {"block": True})
  post_tool_use                — после успешного вызова
  tool_error                   — если инструмент упал

Разделение труда: сам хук делает свой побочный эффект (пишет файл и т.п.).
Показ на экране — отдельный жанр в ui.py, чтобы визуально не путать с
результатом инструмента.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path

import ui

_LOG_FILE = Path(__file__).with_name(".agent_events.log")


class EventBus:
    """Простой диспетчер: событие -> список обработчиков."""

    def __init__(self) -> None:
        self._handlers: dict[str, list] = defaultdict(list)

    def on(self, event: str, handler) -> "EventBus":
        self._handlers[event].append(handler)
        return self  # позволяет цепочку .on(...).on(...)

    def emit(self, event: str, **payload) -> list:
        """Разослать событие всем подписчикам, собрать их возвраты.

        Ошибку каждого хука ГЛОТАЕМ: упавший наблюдатель не имеет права
        уронить наблюдаемого — цикл продолжит работу, просто без этого хука.
        """
        results = []
        for handler in self._handlers.get(event, []):
            try:
                r = handler(event=event, **payload)
                if r:
                    results.append(r)
            except Exception as e:  # noqa: BLE001 — намеренно широко
                ui.show_hook_error(event, str(e))
        return results


bus = EventBus()


# --- простой встроенный хук: аудит-лог каждого события ----------------------
def hook_logger(event: str, **payload) -> None:
    """Пишет каждое событие в .agent_events.log с меткой времени и превью.

    Это то, что цикл НЕ должен знать про себя: наблюдаемость снаружи.
    """
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    tool = payload.get("tool", "")
    line = f"[{ts}] {event}" + (f" tool={tool}" if tool else "")

    if "args" in payload and payload["args"]:
        first = str(list(payload["args"].values())[0])[:60]
        line += f" arg={first!r}"
    if "output" in payload:
        line += f" output_len={len(str(payload['output']))}"
    if "error" in payload:
        line += f" error={payload['error']!r}"

    with open(_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    # отдельный визуальный жанр — видно, что хук сработал (не путаем с результатом)
    ui.show_hook_fired(event, tool or line)


# Подписываем логгер на все интересные точки жизненного цикла.
bus.on("session_start", hook_logger)
bus.on("pre_tool_use", hook_logger)
bus.on("post_tool_use", hook_logger)
bus.on("tool_error", hook_logger)
bus.on("session_end", hook_logger)
