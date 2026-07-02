"""
Файловый граф зависимостей задач (durable task DAG).

Воплощаем ровно то, что разобрали в конспекте (тема 7):

  * Узел = задача, ребро = зависимость "B не стартует, пока не закрыта A".
    Это НЕ плоский список (TodoWrite), а граф: по рёбрам можно вычислить
    ГОТОВЫЕ (разблокированные) задачи — топологический порядок выполнения.

  * Источник правды — файл .agent_tasks.json на диске, а НЕ контекст модели.
    Весь граф в контекст не тащим; код читает файл и отдаёт модели срез
    (готовые задачи). Окно — стол, диск — сейф.

  * Кто что делает: СМЫСЛ ("задача 7 закрыта") решает модель — вызовом
    update_task. Саму ЗАПИСЬ в файл и вычисление готовых задач по рёбрам
    делает КОД (этот модуль), не модель.

  * Переживает падения/перезапуски: запись атомарна (пишем во временный файл
    и os.replace), поэтому сам файл не бьётся на середине записи. При старте
    код перечитывает файл и продолжает с места обрыва, а не с нуля.

  * Зазор "сделал — не успел пометить" не закрыть полностью. Поэтому add
    идемпотентен (повтор по тому же content безвреден — не плодит дубль),
    а задачи стоит дробить мелко (потеря дешевле).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

TASKS_FILE = Path(".agent_tasks.json")

# Замок для АТОМАРНОГО взятия задачи (pull-модель, тема 11).
#
# Взятие задачи = два действия: (1) ПРОЧИТАТЬ граф и найти готовую pending-задачу,
# (2) ЗАПИСАТЬ ей in_progress + владельца. Между ними — щель. Если два воркера
# сканируют граф одновременно, оба успевают прочитать "#5 свободна" ДО того, как
# кто-то записал, и берутся за одну задачу (race condition / lost update).
#
# Lock НЕ "запрещает читать" — он СКЛЕИВАЕТ пару чтение-запись в одно неразрывное
# (атомарное) действие: пока один воркер держит замок, другой ждёт у входа и,
# войдя, видит задачу уже занятой. Это ровно смысл слова "атомарно" в статье.
_claim_lock = threading.Lock()

_VALID_STATUS = ("pending", "in_progress", "done")
_MARK = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}


# --- чтение/запись файла (источник правды) ---------------------------------

def _load() -> dict[str, Any]:
    """Прочитать граф из файла. Нет файла — пустой граф."""
    if not TASKS_FILE.exists():
        return {"tasks": [], "next_id": 1}
    try:
        return json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # Файл побит (например упали посреди НЕатомарной записи в прошлом) —
        # начинаем чисто, чем падать. Atomic write ниже это и предотвращает.
        return {"tasks": [], "next_id": 1}


def _save(state: dict[str, Any]) -> None:
    """Атомарная запись: сначала во временный файл, потом os.replace.

    os.replace атомарен в пределах одной ФС (и на Windows тоже). Поэтому
    .agent_tasks.json в любой момент — это ЛИБО старая целая версия, ЛИБО
    новая целая; половинчатого побитого состояния при падении не бывает.
    """
    tmp = TASKS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, TASKS_FILE)


# --- вычисления по графу (это делает КОД, не модель) -----------------------

def _by_id(state: dict[str, Any]) -> dict[int, dict]:
    return {t["id"]: t for t in state["tasks"]}


def _is_ready(task: dict, index: dict[int, dict]) -> bool:
    """Задача готова, если она pending и ВСЕ её зависимости уже done."""
    if task["status"] != "pending":
        return False
    return all(index.get(dep, {}).get("status") == "done" for dep in task["blocked_by"])


def _ready_tasks(state: dict[str, Any]) -> list[dict]:
    index = _by_id(state)
    return [t for t in state["tasks"] if _is_ready(t, index)]


# --- рендер для модели (отдаём срез, а не сырой JSON) ----------------------

def _render(state: dict[str, Any]) -> str:
    tasks = state["tasks"]
    if not tasks:
        return "Граф задач пуст."
    index = _by_id(state)
    lines = []
    for t in tasks:
        deps = t["blocked_by"]
        dep_s = f"  (ждёт: {', '.join('#' + str(d) for d in deps)})" if deps else ""
        ready = "  ← ГОТОВА" if _is_ready(t, index) else ""
        who = f"  (взял: {t['claimed_by']})" if t.get("claimed_by") else ""
        lines.append(f"#{t['id']} {_MARK[t['status']]} {t['content']}{dep_s}{who}{ready}")
    ready = _ready_tasks(state)
    footer = (
        "\nГотовы к работе сейчас: " + ", ".join("#" + str(t["id"]) for t in ready)
        if ready else
        "\nГотовых задач нет (всё либо в работе/сделано, либо ждёт зависимостей)."
    )
    return "\n".join(lines) + "\n" + footer


# --- инструменты (handlers) -------------------------------------------------

def add_tasks(tasks: list) -> str:
    """Добавить задачи в граф (одним пакетом можно весь разбор сразу).

    Каждая задача: {"content": str, "deps": [индексы в ЭТОМ пакете]}.
    deps — это 0-базовые индексы внутри переданного списка: так модель в один
    вызов раскладывает фичу на подзадачи с зависимостями между ними. Код сам
    присваивает стабильные id и переводит индексы пакета в эти id.

    Идемпотентность: если задача с таким же content уже есть — НЕ дублируем,
    переиспользуем её id ("создать, если нет"). Поэтому повторный вызов после
    сбоя безвреден.
    """
    state = _load()
    by_content = {t["content"]: t["id"] for t in state["tasks"]}

    # Сначала раздаём id всем элементам пакета (с учётом идемпотентности),
    # чтобы потом перевести deps-индексы в реальные id.
    batch_ids: list[int] = []
    for item in tasks:
        content = (item or {}).get("content", "").strip()
        if not content:
            batch_ids.append(-1)
            continue
        if content in by_content:
            batch_ids.append(by_content[content])      # уже есть — переиспользуем
            continue
        new_id = state["next_id"]
        state["next_id"] += 1
        state["tasks"].append({
            "id": new_id,
            "content": content,
            "status": "pending",
            "blocked_by": [],
        })
        by_content[content] = new_id
        batch_ids.append(new_id)

    # Теперь проставляем зависимости (индексы пакета -> id).
    index = _by_id(state)
    for item, tid in zip(tasks, batch_ids):
        if tid == -1:
            continue
        deps_idx = (item or {}).get("deps", []) or []
        dep_ids = []
        for di in deps_idx:
            if isinstance(di, int) and 0 <= di < len(batch_ids) and batch_ids[di] not in (-1, tid):
                dep_ids.append(batch_ids[di])
        # объединяем с уже существующими, без дублей, сохраняя порядок
        existing = index[tid]["blocked_by"]
        index[tid]["blocked_by"] = list(dict.fromkeys([*existing, *dep_ids]))

    _save(state)
    return "Граф задач обновлён.\n" + _render(state)


def update_task(id: int, status: str) -> str:
    """Пометить задачу новым статусом. ЗАПИСЬ в файл делает код, не модель."""
    if status not in _VALID_STATUS:
        return f"ОШИБКА: статус должен быть одним из {_VALID_STATUS}"
    state = _load()
    index = _by_id(state)
    task = index.get(id)
    if task is None:
        return f"ОШИБКА: задачи #{id} нет в графе"
    task["status"] = status
    _save(state)
    return f"OK: задача #{id} -> {status}.\n" + _render(state)


def claim_task(worker: str) -> str:
    """АТОМАРНО взять следующую готовую задачу себе (pull-модель, тема 11).

    В отличие от push (координатор адресует задачу конкретному товарищу через
    team_send), здесь никто задачу не адресует: задачи лежат в общей куче, и
    воркер САМ берёт незаблокированную готовую — "кто первый схватил".

    Вся операция под _claim_lock, поэтому "найти готовую + пометить in_progress"
    неразрывна: два воркера не могут схватить одну и ту же задачу. Записывает,
    КТО взял (claimed_by), — это и есть пометка "я взял эту задачу".
    """
    with _claim_lock:                     # ← атомарная секция: read-modify-write целиком
        state = _load()
        ready = _ready_tasks(state)
        if not ready:
            return f"{worker}: готовых задач нет — брать нечего."
        task = ready[0]                   # берём первую готовую (порядок = порядок графа)
        task["status"] = "in_progress"
        task["claimed_by"] = worker
        _save(state)
        return (
            f"{worker} взял #{task['id']}: {task['content']}\n"
            "(атомарно помечено in_progress — другой воркер эту задачу уже не возьмёт)\n"
            + _render(state)
        )


def list_tasks() -> str:
    """Показать граф и какие задачи ГОТОВЫ к работе сейчас (считает код)."""
    return _render(_load())


def restore_note() -> str | None:
    """Сводка незавершённого графа для подгрузки при старте сессии.

    Это и есть "восстановление при рестарте": не модель вспоминает по
    контексту, а код читает файл и даёт модели текущее состояние. None, если
    графа нет или всё уже сделано — тогда ничего в контекст не вливаем.
    """
    state = _load()
    if not state["tasks"]:
        return None
    if all(t["status"] == "done" for t in state["tasks"]):
        return None
    return (
        "[Восстановлено из .agent_tasks.json — незавершённый граф задач предыдущей сессии]\n"
        + _render(state)
        + "\nПродолжи с готовых задач; уже сделанные не переделывай."
    )
