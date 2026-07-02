# Mini Claude Code — an agent from scratch + a harness-engineering study

A learning project: a from-scratch agent in the style of Claude Code (Python, on the
OpenAI-compatible DeepSeek API), alongside a growing **study page** (*konspekt*) on how a
minimal agent loop grows into a production-grade harness.

The project works through an article on harness engineering topic by topic: each idea is
first explored conceptually (the study page), then implemented in code (`code/`).

## 📖 Study page (GitHub Pages)

Read online: **https://sh0tn1k.github.io/mini-claude-code/**

> The page is served from the `docs/` folder. See [Publishing](#-publishing) for how Pages is set up.

The page is conceptual: 23 topics from the master loop to the roadmap
(tools & registry, subagents, context compaction, task graph, background tasks,
mailboxes, FSM, worktrees, streaming, snapshots, permissions, hooks,
sessions, async, interrupts/steering, prompt caching, MCP, enterprise upgrades).
It is written in Russian.

## 🧩 The agent (`code/`)

| Module | What it implements |
|---|---|
| `agent.py` | Master loop: iterate on the *structure* of the model's reply, dispatch, parallelism |
| `tools.py` | Tool registry, schemas, progressive disclosure |
| `team.py` | Persistent teammates with mailboxes (JSONL) |
| `task_graph.py` | Durable task graph with dependencies (`.agent_tasks.json`) |
| `background.py` | Background tasks (async notification queue, h2A) |
| `steering.py` | Real-time interrupt injection (steering) |
| `cache.py` | Prompt-caching observability (HIT/MISS from usage) |
| `permissions.py` | Permission rules via YAML (`config/permissions.yaml`) |
| `events.py` | Event bus and lifecycle hooks |
| `sessions.py` | Save / resume / branch sessions |
| `snapshots.py` | File snapshots and revert |
| `mcp_runtime.py` | Connecting external MCP servers |
| `ui.py` | Terminal output (rich) |

Future directions live in [`code/ROADMAP.md`](code/ROADMAP.md).

## 🚀 Running

Requires Python 3.11+.

```bash
cd code
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env          # then put your DeepSeek key in .env
python agent.py
```

Get a DeepSeek key at https://platform.deepseek.com and place it in `code/.env`
(this file is in `.gitignore` and never reaches the repository).

### Optional: MCP servers

From the **repository root** (the MCP config is read from the root-level `config/`):

```bash
cp config/mcp_config.yaml.example config/mcp_config.yaml   # then edit for your servers
```

Without a config, the agent starts with no MCP tools.

## 📁 Repository layout

```
.
├── code/            # the agent (Python)
│   ├── agent.py … mcp_runtime.py
│   ├── config/      # permissions.yaml
│   ├── requirements.txt
│   └── .env.example
├── config/          # mcp_config.yaml.example (MCP config is read from here)
├── docs/            # GitHub Pages
│   └── index.html   # the study page
├── konspekt.html    # working copy of the study page (source for docs/index.html)
├── LICENSE
└── README.md
```

> When you edit `konspekt.html`, re-sync the Pages copy: `cp konspekt.html docs/index.html`.

## 🌐 Publishing

1. Create a public `mini-claude-code` repository on GitHub.
2. Push the `main` branch.
3. Settings → Pages → **Deploy from a branch** → `main` / `/docs` → Save.
4. After a minute the study page will be live at the URL above.

## License

[MIT](LICENSE) © 2026 Artem Kondratiev

The agent is an educational implementation of harness-engineering principles, unaffiliated
with Anthropic; "Claude Code" is referenced only as the subject of study. The source
article (PDF/translation) is not included in the repository — it is third-party copyright.
