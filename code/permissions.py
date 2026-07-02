"""
Шлюз разрешений (тема 15).

Идея статьи: у Claude Code трёхуровневая модель разрешений, и политика живёт
в КОНФИГЕ (permissions.yaml), а не в коде. Поменять «что требует подтверждения»
= правка YAML, а не перевыкладка кода. Этот модуль — сам шлюз: он читает
правила и оценивает КАЖДЫЙ вызов инструмента ПЕРЕД исполнением.

Три списка правил (regex) проверяются ПО ПОРЯДКУ, первое совпадение выигрывает:
  1. always_deny  — блок без исключений (стоит первым: запрет сильнее разрешения,
                    иначе опасная команда могла бы проскочить общий allow-шаблон).
  2. always_allow — тихий пропуск, без уведомления.
  3. ask_user     — пауза и запрос подтверждения у человека.
  По умолчанию (ни одно не совпало) — разрешить.

Разделение труда «структура vs смысл»: шлюз — чистая СТРУКТУРА (тупое
сопоставление regex, никакого понимания намерения). Решение по существу
(нажать «да») в режиме ask остаётся за ЧЕЛОВЕКОМ. Код лишь ставит на паузу
в нужной точке.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_RULES_PATH = Path(__file__).with_name("config") / "permissions.yaml"


def load_rules(path: Path = _RULES_PATH) -> dict:
    """Прочитать правила из YAML. Если файла нет — пустая политика (всё разрешено)."""
    if not path.exists():
        return {"always_deny": [], "always_allow": [], "ask_user": []}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for k in ("always_deny", "always_allow", "ask_user"):
        data.setdefault(k, [])
    return data


# Загружаем один раз при импорте — политика фиксируется на старте процесса.
RULES = load_rules()


@dataclass
class Verdict:
    level: str      # "deny" | "allow" | "ask" | "default"
    reason: str


def _subject(tool_name: str, args: dict) -> str:
    """Строка, по которой матчим правила.

    Для bash берём саму команду (шаблоны вроде '^bash rm ' ждут её в начале
    после имени инструмента). Для остального — имя инструмента + значения
    аргументов (чтобы '\\.env' поймал доступ к .env по пути в любом инструменте).
    """
    if tool_name == "Bash":
        return f"bash {args.get('command', '')}"
    vals = " ".join(str(v) for v in args.values())
    return f"{tool_name} {vals}".rstrip()


def check_permission(tool_name: str, args: dict, rules: dict | None = None) -> Verdict:
    """Оценить вызов ДО исполнения. Возвращает вердикт, но НЕ спрашивает человека
    и ничего не печатает — это ответственность обвязки (см. agent.py/ui.py)."""
    if rules is None:
        rules = RULES
    subject = _subject(tool_name, args)

    # Уровень 1: always_deny — первым, без исключений.
    for rule in rules.get("always_deny", []):
        if re.search(rule["pattern"], subject, re.IGNORECASE):
            return Verdict("deny", rule.get("reason", "запрещено политикой"))

    # Уровень 2: always_allow — тихий пропуск.
    for rule in rules.get("always_allow", []):
        if re.search(rule["pattern"], subject, re.IGNORECASE):
            return Verdict("allow", rule.get("reason", "разрешено политикой"))

    # Уровень 3: ask_user — нужна пауза и подтверждение человека.
    for rule in rules.get("ask_user", []):
        if re.search(rule["pattern"], subject, re.IGNORECASE):
            return Verdict("ask", rule.get("reason", "требует подтверждения"))

    # По умолчанию — разрешить (правило не совпало).
    return Verdict("default", "правило не совпало")
