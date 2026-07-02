"""
Постоянные товарищи по команде с JSONL-почтовыми ящиками + FSM-протокол.

Воплощаем ровно то, что разобрали в конспекте (темы 9 и 10):

  ТЕМА 9 — постоянные товарищи с ящиками. В отличие от субагента (односторонний,
  одноразовый, безымянный, контекст стирается после return) у товарища есть:
    * ИМЯ/АДРЕС — ему может написать кто угодно, а не только «родитель»;
    * ЯЩИК — файл <имя>.jsonl, куда сообщения ДОПИСЫВАЮТСЯ (append-only) и
      который ПЕРЕЖИВАЕТ сессии. Отсюда же его цена: файл растёт (раздувание).
    * обмен АСИНХРОННЫЙ — отправитель дописал строку и ушёл, адресат вычитает
      на своём витке (та же логика доставки, что у h2A, но очередь персональная
      и двусторонняя).

  ТЕМА 10 — FSM-протокол поверх ящиков (дисциплина). Голый ящик = хаос, потеря
  писем, невидимые дедлоки. Поэтому у каждого агента есть СОСТОЯНИЕ, а таблицы
    * TRANSITIONS — какие переходы состояний допустимы;
    * ACCEPTS — какие типы писем состояние принимает ПРЯМО СЕЙЧАС
  задают протокол. Состояние — это СТРУКТУРА (ярлык), а не смысл: сравнить ярлык
  дёшево, поэтому протокол обслуживает КОД (этот модуль), а не модель:
    * недопустимый переход — код отказывает;
    * письмо, не принимаемое текущим состоянием, НЕ теряется — лежит в ящике и
      доставляется детерминированно, когда состояние станет подходящим;
    * дедлок (A ждёт B, B ждёт A) КОД замечает обходом графа waiting_on —
      ровно потому, что состояния читаемы.

  Кто что делает: СМЫСЛ («задача сделана», «перехожу в review») решает модель
  вызовом инструмента. Проверку правил, запись в ящик и детекцию тупиков делает
  КОД. Модель даёт содержание — обвязка даёт дисциплину.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

TEAM_DIR = Path(".agent_team")
STATE_FILE = TEAM_DIR / "_state.json"        # реестр агентов (состояния, курсоры) — атомарно
# Сами ящики: TEAM_DIR/<имя>.jsonl — append-only лог писем.

# --- FSM: состояния и таблицы (ЭТО СТРУКТУРА — её проверяет код) -------------

# Допустимые переходы состояний. DONE терминально — из него никуда.
TRANSITIONS: dict[str, set[str]] = {
    "IDLE":           {"WORKING", "WAITING_REVIEW", "BLOCKED", "DONE"},
    "WORKING":        {"IDLE", "WAITING_REVIEW", "BLOCKED", "DONE"},
    "WAITING_REVIEW": {"WORKING", "IDLE", "DONE"},
    "BLOCKED":        {"WORKING", "IDLE", "DONE"},
    "DONE":           set(),
}

# Какие ТИПЫ писем состояние принимает ПРЯМО СЕЙЧАС. Остальные не теряются —
# ждут в ящике до состояния, где они допустимы (в этом и детерминизм).
ACCEPTS: dict[str, set[str]] = {
    "IDLE":           {"task", "question", "review_request", "review", "reply", "cancel"},
    "WORKING":        {"cancel"},                 # занят — прочее ждёт возврата в IDLE
    "WAITING_REVIEW": {"review", "cancel"},       # ждёт именно ревью
    "BLOCKED":        {"reply", "cancel"},         # ждёт именно ответа, который разблокирует
    "DONE":           set(),                        # завершён — не принимает ничего
}

MSG_TYPES = ("task", "question", "review_request", "review", "reply", "cancel")


# --- чтение/запись реестра состояний (атомарно, как в task_graph) -----------

def _load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"agents": {}, "next_msg_id": 1}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"agents": {}, "next_msg_id": 1}


def _save_state(state: dict[str, Any]) -> None:
    """Атомарная запись реестра: временный файл + os.replace (не бьётся при падении)."""
    TEAM_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


# --- ящик: append-only JSONL ------------------------------------------------

def _mailbox_path(name: str) -> Path:
    return TEAM_DIR / f"{name}.jsonl"


def _append_message(msg: dict[str, Any]) -> None:
    """ДОПИСАТЬ письмо в ящик получателя (одна JSON-строка). Файл только растёт —
    это и есть персистентность из темы 9 (и её цена: раздувание)."""
    TEAM_DIR.mkdir(parents=True, exist_ok=True)
    path = _mailbox_path(msg["to"])
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(msg, ensure_ascii=False) + "\n")


def _read_mailbox(name: str) -> list[dict[str, Any]]:
    """Прочитать весь лог ящика (append-only → читаем построчно)."""
    path = _mailbox_path(name)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# --- детекция дедлоков (это делает КОД по читаемым состояниям) ---------------

def _deadlocks(state: dict[str, Any]) -> list[list[str]]:
    """Найти циклы ожидания среди BLOCKED-агентов по рёбрам waiting_on.

    A в BLOCKED с waiting_on=B, B в BLOCKED с waiting_on=A → цикл A→B→A.
    Возможно именно потому, что состояние и waiting_on — читаемые ярлыки."""
    agents = state["agents"]
    # ребро есть только если агент BLOCKED и ждёт конкретного адресата
    edge: dict[str, str] = {
        n: a["waiting_on"]
        for n, a in agents.items()
        if a["state"] == "BLOCKED" and a.get("waiting_on") in agents
    }
    cycles: list[list[str]] = []
    seen: set[str] = set()
    for start in edge:
        if start in seen:
            continue
        path: list[str] = []
        node = start
        local: dict[str, int] = {}
        while node in edge and node not in local:
            local[node] = len(path)
            path.append(node)
            node = edge[node]
        if node in local:                       # замкнулись на уже пройденном → цикл
            cycle = path[local[node]:]
            if sorted(cycle) not in [sorted(c) for c in cycles]:
                cycles.append(cycle)
        seen.update(path)
    return cycles


# --- рендер команды для модели ----------------------------------------------

def _pending_deliverable(name: str, agent: dict[str, Any]) -> tuple[int, int]:
    """(доступно_сейчас, отложено) непрочитанных писем для агента в его состоянии."""
    consumed = set(agent.get("consumed", []))
    accepts = ACCEPTS[agent["state"]]
    ready = deferred = 0
    for m in _read_mailbox(name):
        if m["id"] in consumed:
            continue
        if m["type"] in accepts:
            ready += 1
        else:
            deferred += 1
    return ready, deferred


def _render(state: dict[str, Any]) -> str:
    agents = state["agents"]
    if not agents:
        return "Команда пуста (нет зарегистрированных товарищей)."
    lines = []
    for name in sorted(agents):
        a = agents[name]
        ready, deferred = _pending_deliverable(name, a)
        wait = f" ждёт: {a['waiting_on']}" if a["state"] == "BLOCKED" and a.get("waiting_on") else ""
        box = f"  [ящик: {ready} к прочтению" + (f", {deferred} отложено" if deferred else "") + "]"
        role = f" ({a['role']})" if a.get("role") else ""
        lines.append(f"• {name}{role} — состояние {a['state']}{wait}{box}")
    out = "\n".join(lines)
    cycles = _deadlocks(state)
    if cycles:
        out += "\n⚠ ДЕДЛОК: " + "; ".join(" → ".join([*c, c[0]]) for c in cycles) + \
               " — взаимное ожидание, разорви его (смени состояние или ответь письмом)."
    return out


# --- инструменты (handlers) -------------------------------------------------

def team_register(name: str, role: str = "") -> str:
    """Завести постоянного товарища: имя-адрес + ящик, стартовое состояние IDLE."""
    name = (name or "").strip()
    if not name:
        return "ОШИБКА: нужно имя товарища"
    state = _load_state()
    if name in state["agents"]:
        return f"Товарищ '{name}' уже в команде.\n" + _render(state)
    state["agents"][name] = {
        "role": (role or "").strip(),
        "state": "IDLE",
        "waiting_on": None,
        "consumed": [],           # id уже вычитанных писем (курсор поверх append-only лога)
    }
    _save_state(state)
    _mailbox_path(name).touch()   # создаём пустой ящик
    return f"OK: товарищ '{name}' в команде (состояние IDLE).\n" + _render(state)


def team_send(to: str, type: str, content: str, sender: str = "coordinator") -> str:
    """Дописать письмо в ящик получателя. Код проверяет адрес и тип; доставку
    решает состояние получателя (принимает сейчас — или письмо ждёт в ящике)."""
    state = _load_state()
    if to not in state["agents"]:
        return f"ОШИБКА: нет товарища с адресом '{to}'. Есть: {', '.join(state['agents']) or '(никого)'}"
    if type not in MSG_TYPES:
        return f"ОШИБКА: тип письма должен быть одним из {MSG_TYPES}"
    msg = {
        "id": state["next_msg_id"],
        "from": sender,
        "to": to,
        "type": type,
        "content": content or "",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    state["next_msg_id"] += 1
    _save_state(state)
    _append_message(msg)          # append-only: письмо легло в лог навсегда
    recv_state = state["agents"][to]["state"]
    if type in ACCEPTS[recv_state]:
        note = f"'{to}' в состоянии {recv_state} — примет его при чтении ящика."
    else:
        note = (f"'{to}' в состоянии {recv_state} этот тип сейчас НЕ принимает — "
                f"письмо не потеряно, лежит в ящике и дойдёт, когда состояние станет подходящим.")
    return f"OK: письмо #{msg['id']} ({type}) → '{to}'. {note}"


def team_inbox(name: str) -> str:
    """Прочитать ДОСТУПНЫЕ сейчас письма товарища (тип принимается его состоянием).

    Недоступные не показываем и не помечаем прочитанными — они детерминированно
    ждут в ящике до подходящего состояния. Прочитанные фиксируем в курсоре,
    чтобы не выдавать их дважды."""
    state = _load_state()
    if name not in state["agents"]:
        return f"ОШИБКА: нет товарища '{name}'"
    agent = state["agents"][name]
    accepts = ACCEPTS[agent["state"]]
    consumed = set(agent.get("consumed", []))
    delivered: list[dict] = []
    deferred = 0
    for m in _read_mailbox(name):
        if m["id"] in consumed:
            continue
        if m["type"] in accepts:
            delivered.append(m)
            consumed.add(m["id"])
        else:
            deferred += 1
    agent["consumed"] = sorted(consumed)
    _save_state(state)
    if not delivered:
        tail = f" ({deferred} писем ждут другого состояния)" if deferred else ""
        return f"Ящик '{name}': новых доступных писем нет (состояние {agent['state']}).{tail}"
    lines = [f"Ящик '{name}' (состояние {agent['state']}):"]
    for m in delivered:
        lines.append(f"  #{m['id']} [{m['type']}] от {m['from']} ({m['ts']}): {m['content']}")
    if deferred:
        lines.append(f"  … ещё {deferred} писем отложены (тип не принимается в текущем состоянии).")
    return "\n".join(lines)


def team_set_state(name: str, state: str, waiting_on: str = "") -> str:
    """Сменить состояние товарища. Код ПРОВЕРЯЕТ допустимость перехода по
    таблице TRANSITIONS (это структура, не смысл) и требует waiting_on для BLOCKED."""
    reg = _load_state()
    if name not in reg["agents"]:
        return f"ОШИБКА: нет товарища '{name}'"
    if state not in TRANSITIONS:
        return f"ОШИБКА: неизвестное состояние '{state}'. Есть: {', '.join(TRANSITIONS)}"
    agent = reg["agents"][name]
    cur = agent["state"]
    if state != cur and state not in TRANSITIONS[cur]:
        allowed = ", ".join(TRANSITIONS[cur]) or "(терминальное — никуда)"
        return f"ОШИБКА: недопустимый переход {cur} → {state}. Из {cur} можно: {allowed}"
    if state == "BLOCKED":
        target = (waiting_on or "").strip()
        if not target:
            return "ОШИБКА: для BLOCKED укажи waiting_on — кого именно ждём"
        if target not in reg["agents"]:
            return f"ОШИБКА: ждать некого — нет товарища '{target}'"
        agent["waiting_on"] = target
    else:
        agent["waiting_on"] = None
    agent["state"] = state
    _save_state(reg)
    return f"OK: '{name}': {cur} → {state}.\n" + _render(reg)


def team_status() -> str:
    """Показать всю команду: состояния, кто кого ждёт, ящики и ДЕДЛОКИ (считает код)."""
    return _render(_load_state())


def restore_note() -> str | None:
    """Сводка живой команды для подгрузки при старте сессии (как у task_graph).

    Товарищи персистентны: если с прошлой сессии остались незавершённые (не все
    DONE) — код напоминает модели их состояние, а не модель вспоминает по контексту.
    """
    state = _load_state()
    agents = state["agents"]
    if not agents:
        return None
    if all(a["state"] == "DONE" for a in agents.values()):
        return None
    return (
        "[Восстановлено из .agent_team/ — постоянные товарищи предыдущей сессии]\n"
        + _render(state)
        + "\nПродолжай с их текущих состояний."
    )
