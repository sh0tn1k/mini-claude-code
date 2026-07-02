# Mini Claude Code — агент с нуля + конспект по harness engineering

Учебный проект: с нуля собранный агент в стиле Claude Code (Python, OpenAI-совместимый
API DeepSeek) и растущий **конспект** о том, как из минимального агентного цикла
вырастает промышленная обвязка.

Проект разбирает статью о harness engineering тема за темой: каждая идея сначала
разбирается концептуально (конспект), затем воплощается в коде (`code/`).

## 📖 Конспект (GitHub Pages)

Читать онлайн: **https://sh0tn1k.github.io/mini-claude-code/**

> Страница отдаётся из папки `docs/`. Как включить Pages — см. раздел [Публикация](#публикация).

Конспект — концептуальный: 23 темы от мастер-лупа до дорожной карты
(инструменты и реестр, субагенты, сжатие контекста, граф задач, фоновые задачи,
почтовые ящики, FSM, worktree, потоковая выдача, снимки, разрешения, хуки,
сессии, асинхронность, прерывания, prompt caching, MCP, корпоративные улучшения).

## 🧩 Код агента (`code/`)

| Модуль | Что реализует |
|---|---|
| `agent.py` | Мастер-луп: цикл по структуре ответа модели, диспетчеризация, параллелизм |
| `tools.py` | Реестр инструментов, схемы, постепенное раскрытие |
| `team.py` | Постоянные товарищи с почтовыми ящиками (JSONL) |
| `task_graph.py` | Durable-граф задач с зависимостями (`.agent_tasks.json`) |
| `background.py` | Фоновые задачи (асинхронная очередь h2A) |
| `steering.py` | Вставка прерываний в реальном времени |
| `cache.py` | Наблюдаемость prompt caching (HIT/MISS по usage) |
| `permissions.py` | Разрешения по YAML-правилам (`config/permissions.yaml`) |
| `events.py` | Шина событий и хуки жизненного цикла |
| `sessions.py` | Сохранение / продолжение / ветвление сессий |
| `snapshots.py` | Снимки файлов и revert |
| `mcp_runtime.py` | Подключение внешних MCP-серверов |
| `ui.py` | Терминальный вывод (rich) |

Направления дальнейшего развития — в [`code/ROADMAP.md`](code/ROADMAP.md).

## 🚀 Запуск

Нужен Python 3.11+.

```bash
cd code
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env          # затем впиши свой ключ DeepSeek в .env
python agent.py
```

Ключ DeepSeek берётся на https://platform.deepseek.com и кладётся в `code/.env`
(этот файл в `.gitignore` и в репозиторий не попадает).

### Опционально: MCP-серверы

Из **корня репозитория** (конфиг читается из корневого `config/`):

```bash
cp config/mcp_config.yaml.example config/mcp_config.yaml   # и отредактируй под свои серверы
```

Без конфига агент запускается без MCP-инструментов.

## 📁 Структура репозитория

```
.
├── code/            # агент (Python)
│   ├── agent.py … mcp_runtime.py
│   ├── config/      # permissions.yaml
│   ├── requirements.txt
│   └── .env.example
├── config/          # mcp_config.yaml.example (конфиг MCP читается отсюда)
├── docs/            # GitHub Pages
│   └── index.html   # конспект
├── konspekt.html    # рабочая копия конспекта (источник для docs/index.html)
├── LICENSE
└── README.md
```

> При правке `konspekt.html` не забудь пересинхронизировать страницу Pages:
> `cp konspekt.html docs/index.html`.

## 🌐 Публикация

1. Создай публичный репозиторий `mini-claude-code` на GitHub.
2. Запушь ветку `main`.
3. Settings → Pages → **Deploy from a branch** → `main` / `/docs` → Save.
4. Через минуту конспект будет доступен по адресу выше.

## Лицензия

[MIT](LICENSE) © 2026 Artem Kondratiev

Код агента — учебная реализация принципов harness engineering, не связанная с
Anthropic; «Claude Code» упоминается как предмет изучения. Исходная статья (PDF/перевод)
в репозиторий не включена — это чужой копирайт.
