# Taxonomaid

Hybrid auto-sorter for shared folders. Cheap, deterministic rules try first;
an LLM agent fills the gaps; the LLM's good decisions are mined back into
rules so the LLM gets called less and less over time.

## What it is

- **A daemon** that watches one or more directories and moves new files to
  the right destination based on rules.
- **A rule engine** with anchored rules, learned confidence scores, and
  coherence guards (e.g. "tax PDFs only go to a year folder if the year
  matches").
- **An LLM fallback** that uses any OpenAI-compatible endpoint - Gemini by
  default, but Ollama, vLLM, OpenAI, LocalAI, and LM Studio all work without
  code changes.
- **A pluggable notifier** that asks the user to approve low-confidence
  placements over Telegram (or any channel reachable by Apprise) and learns
  from the reply.
- **An auditor** that periodically rechecks placed files and demotes rules
  whose destinations have lost coherence.

## Where to go next

- [Getting started](getting-started.md) - install, configure, and run the
  daemon.
- [Configuration](configuration.md) - YAML schemas with annotated examples.
- [Architecture](architecture.md) - components, data flow, hexagonal
  layering, and the rule schema.
- [ADR 0001](adr/0001-hexagonal-layering.md) - why the package is split
  into `domain` / `ports` / `services` / `adapters`.
- [API reference](reference/index.md) - auto-generated from docstrings.

## Status

Feature-complete. Multi-root dispatcher, OpenAI-compatible LLM
client (Gemini Flash by default), Apprise outbound + Telegram
inbound notifier, rule engine, pattern miner with interactive
review CLI, recently-moved tracking with token-Jaccard filename
similarity (embeddings are a future swap behind the same
interface), directory auditor, systemd packaging, Docker / Compose.

See [Architecture](architecture.md) for a component tour.
