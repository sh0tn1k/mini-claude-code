# Roadmap — how to improve the agent further

The harness is complete: from a minimal agent loop to a production-grade multi-agent
system (streaming, parallel execution, prompt caching, Redis mailboxes, permissions,
session persistence, MCP runtime). These are directions for growth — NOT yet
implemented. Guiding principle: improvements go "on the edges," while the core
(the agent loop) stays untouched.

## 1. Parallel subagent launch
Right now subagents in `team.py` run sequentially. Refactoring spawn onto
`asyncio.gather` would let the lead run several research subagents at once
(the way Claude Code itself does) — research time shrinks proportionally to the
number of parallel agents.

## 2. Vector memory store
Long-term memory is flat markdown, injected in full into every session.
Replacing it with a lightweight vector store (e.g. ChromaDB) would allow
retrieving semantically relevant memories instead of the whole summary — the
context stays focused as the project grows.

## 3. Precise token accounting
`cache.py` counts tokens per session but does not break the cost down by task or
tool type. A per-operation cost log would show which calls are the most
expensive and where to optimize.

## 4. Webhook event bus
`events.py` runs hooks only in-process. Adding delivery of events to an external
HTTP endpoint would give integration with Slack / Datadog / PagerDuty without
changing the agent loop.

## 5. Evaluation framework (LLM-as-a-judge)
Tests verify that the harness works correctly, but not how well the agent solves
real tasks. An LLM-as-a-judge evaluation layer (accuracy, tool-use efficiency,
plan adherence) would turn the repository from a working system into one suitable
for benchmarking.
