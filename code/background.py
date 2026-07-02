"""
Фоновое выполнение (асинхронная очередь h2A).

Воплощаем ровно то, что разобрали в конспекте (тема 8):

  * Обычно цикл рассуждения и выполнение долгой операции (I/O) СКЛЕЕНЫ: код
    блокируется на время команды (subprocess.run), и весь агент стоит.
  * Здесь мы их РАЗЪЕДИНЯЕМ: долгая команда уходит в отдельный daemon-поток
    (launch), а нить цикла остаётся свободной и может взять следующий ход.
  * Поток по завершении кладёт результат в ОБЩУЮ очередь уведомлений.
  * Цикл после каждого хода ВЫЧИТЫВАЕТ очередь (drain) и вставляет готовое как
    user-сообщение. Модель видит его только на ближайшей границе хода —
    единственный канал доставки это сообщение в messages[]. «Очередь» здесь не
    задержка, а сам механизм доставки: быстрее для пошаговой модели не бывает.

Кто решает «в фон или нет» — МОДЕЛЬ (флаг background у bash), по описанию
инструмента. Обвязка лишь даёт возможность и не проверяет её уместность:
ошибётся (пустит в фон нужное немедленно) — не поломка, а лишь «делать нечего,
жду». Это фича главного цикла; субагенты (quiet) очередь не трогают.
"""

from __future__ import annotations

import queue
import subprocess
import threading

# Общая очередь уведомлений: сюда фоновые потоки кладут готовые результаты,
# отсюда цикл их вычитывает. queue.Queue потокобезопасна — блокировки не нужны.
_notifications: "queue.Queue[str]" = queue.Queue()

_lock = threading.Lock()
_next_id = 1
_pending = 0          # сколько фоновых задач ещё крутится (для «не выходить, пока фон жив»)


def _run(bg_id: int, command: str) -> None:
    """Тело daemon-потока: выполнить команду и положить итог в очередь."""
    global _pending
    try:
        r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=600)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        note = (f"[фоновая задача #{bg_id} завершилась] `{command}`\n"
                f"(код выхода {r.returncode})\n{out or '(пустой вывод)'}")
    except subprocess.TimeoutExpired:
        note = f"[фоновая задача #{bg_id}] `{command}` — ОШИБКА: таймаут 600с"
    except Exception as e:  # noqa: BLE001 — в поток ничего не должно «протечь» наружу
        note = f"[фоновая задача #{bg_id}] `{command}` — ОШИБКА: {e}"
    _notifications.put(note)
    with _lock:
        _pending -= 1


def launch(command: str) -> str:
    """Запустить команду в фоне. Возвращает СРАЗУ — нить цикла не блокируется."""
    global _next_id, _pending
    with _lock:
        bg_id = _next_id
        _next_id += 1
        _pending += 1
    threading.Thread(target=_run, args=(bg_id, command), daemon=True).start()
    return (f"OK: команда запущена в фоне как задача #{bg_id}. "
            f"Результат придёт отдельным сообщением позже — можешь пока делать "
            f"другую независимую работу, не жди.")


def drain() -> list[str]:
    """Забрать ВСЕ готовые уведомления, не блокируя (для начала каждого хода)."""
    out: list[str] = []
    while True:
        try:
            out.append(_notifications.get_nowait())
        except queue.Empty:
            break
    return out


def has_pending() -> bool:
    """Есть ли ещё крутящиеся фоновые задачи."""
    with _lock:
        return _pending > 0


def wait_one(timeout: float | None = None) -> str | None:
    """Дождаться одного готового уведомления (БЛОКИРУЕТ).

    Нужно для случая «модель хочет закончить, но фон ещё крутится»: здесь делать
    больше нечего (это и есть "фон бесполезен, когда нет независимой работы"),
    поэтому честно ждём результат, а не теряем его.
    """
    try:
        return _notifications.get(timeout=timeout)
    except queue.Empty:
        return None
