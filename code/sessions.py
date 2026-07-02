"""
Сохранение, продолжение и ветвление сессий (тема 17).

Ключевая мысль: никакого отдельного «журнала» не заводим. Вся история диалога
и так живёт в ОДНОМ списке messages — иначе модель не могла бы работать (на
каждом витке мы шлём ей его целиком). Значит:

    сохранить сессию  = сериализовать messages на диск (json.dump)
    продолжить (resume) = загрузить его обратно в messages
    ветвление (branch)  = загрузить старое состояние, но дальше писать под НОВЫМ
                          id — исходный файл остаётся нетронутым, история
                          становится ДЕРЕВОМ, а не прямой линией.

Всё это — операции НАД разговором, поэтому они живут снаружи цикла (REPL-команды),
а не как tools модели: модель живёт ВНУТРИ messages и контейнером управлять не
должна. Цикл о ветках вообще не знает — он крутит один messages, не подозревая,
что он «ветка №2». Магия только в том, КУДА сохраняем и ОТКУДА грузим.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

_DIR = Path(__file__).with_name(".agent_sessions")


def _ensure_dir() -> None:
    _DIR.mkdir(exist_ok=True)


def new_id() -> str:
    """Свежий id сессии — по времени, читаемый и сортируемый."""
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _path(sid: str) -> Path:
    return _DIR / f"{sid}.json"


def save(messages: list[dict], sid: str) -> Path:
    """Сбросить messages в файл ветки sid. Atomic write: пишем во временный
    файл и подменяем — чтобы файл не побился при падении на середине записи."""
    _ensure_dir()
    path = _path(sid)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)     # атомарная подмена
    return path


def load(sid: str) -> list[dict]:
    """Прочитать messages ветки sid обратно в память."""
    with open(_path(sid), "r", encoding="utf-8") as f:
        return json.load(f)


def exists(sid: str) -> bool:
    return _path(sid).exists()


def list_ids() -> list[tuple[str, int]]:
    """Список сохранённых сессий: (id, число сообщений), новые сверху."""
    if not _DIR.exists():
        return []
    out = []
    for p in sorted(_DIR.glob("*.json"), reverse=True):
        try:
            with open(p, "r", encoding="utf-8") as f:
                n = len(json.load(f))
        except (OSError, json.JSONDecodeError):
            n = -1
        out.append((p.stem, n))
    return out
