# ADR 0001 - Hexagonal layering with ports and adapters

- **Status**: Accepted
- **Date**: 2026-05-17
- **Deciders**: Daniel Danielski

## Context

Taxonomaid integrates a long list of external systems: a filesystem
watcher, an LLM provider, a notification channel, a JSONL audit log, and
several scheduled subsystems (miner, auditor). Each one has multiple
possible backends:

- LLM: Gemini (default), OpenAI, Ollama, vLLM, LocalAI, LM Studio.
- Notifier outbound: Telegram, Slack, Discord, ntfy, email, Pushover.
- Notifier inbound: Telegram in Phase 1; Slack / Discord later.
- Decision log: JSONL initially; SQLite if scale demands it later.

The same orchestration code (the dispatcher, the rule engine, the miner)
needs to work against fakes in tests and against real backends in
production - without rewriting the orchestration when a backend changes.

## Decision

Adopt a **hexagonal (ports-and-adapters)** structure:

- `taxonomaid.domain` holds pure types only: frozen dataclasses, enums,
  error hierarchy. Zero internal dependencies.
- `taxonomaid.ports` defines `typing.Protocol` interfaces for every
  external dependency.
- `taxonomaid.services` contains orchestration. Imports only `ports` and
  `domain`. Never imports `adapters` or any third-party library that
  represents a backend.
- `taxonomaid.adapters` contains concrete implementations of ports.
  Imports only `ports` and `domain`.
- `taxonomaid.bootstrap.build_app` is the single composition root and
  the **only** module allowed to import both ports and adapters.

The layering is mechanically enforced via `import-linter` contracts
declared in `pyproject.toml`. Violations fail CI.

## Consequences

**Pros**

- Backend swaps are config changes, not code changes (LLM provider, both
  ends of the notifier, decision-log storage).
- Services are trivially unit-testable against fakes implementing the
  same `Protocol`s. Integration tests use the real composition root with
  fake adapters.
- The composition root is the single place to inspect when reasoning
  about lifecycle and dependency wiring.

**Cons**

- More files than a flat layout. Mitigated by keeping each layer small
  and curated through `__init__.py`.
- Discipline cost: contributors must add a port and an adapter when
  introducing a new dependency, rather than `import some_lib` directly
  in a service. This is enforced by the `import-linter` contracts so the
  cost falls on the contributor at PR time, not on the reviewer.

## Alternatives considered

- **Flat package with classes that import third-party libs directly.**
  Rejected: would have made the LLM-provider abstraction (covering
  Gemini and self-hosted Ollama through one config) impossible without
  further refactoring later, and would have made unit tests hard to
  write without monkeypatching.
- **Hand-written interface modules without `import-linter` enforcement.**
  Rejected: layering rot is silent; we want the rule visible at PR time.
