# Getting started

## Prerequisites

- Linux (developed against `linux` 7.x kernels).
- [`uv`](https://docs.astral.sh/uv/) for Python and dependency management.
  `uv` will fetch the right Python interpreter (3.12) for you.

## Install for development

```bash
git clone https://github.com/gdanielski/Taxonomaid.git
cd Taxonomaid
uv sync --all-groups
```

This creates a `.venv/` with Python 3.12 and every runtime + dev
dependency. After it completes, `uv run taxonomaid --help` should print
the CLI overview.

## Configure

Copy the example configs and supply your secrets via environment
variables. The CLI auto-loads a `.env` file from the current working
directory and from the parent of `--config-dir`, so a single file at
the repo root is the simplest setup:

```bash
mkdir -p config
cp config/watches.example.yaml   config/watches.yaml
cp config/llm.example.yaml       config/llm.yaml
cp config/notifier.example.yaml  config/notifier.yaml

cat > .env <<'EOF'
GEMINI_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
EOF
```

`.env` is gitignored. Variables already set in your shell win over
values in `.env`. If you'd rather not use a file, an explicit
`export GEMINI_API_KEY=...` works the same way - the YAML files
reference `${GEMINI_API_KEY}` via the loader's env-var interpolation.

Validate the config without starting the daemon:

```bash
uv run taxonomaid doctor
```

A successful run prints `config OK` with the number of watches, the
selected LLM model, and the resolved data directory. Any validation
failure exits non-zero with a human-readable message.

## Run

```bash
uv run taxonomaid run
```

This starts the dispatcher loop. Send `SIGTERM` (or `Ctrl-C` for an
interactive run) to shut down cleanly.

## Run the full quality gate locally

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run bandit -r src/taxonomaid -c pyproject.toml
uv run lint-imports
uv run pytest -m "unit or integration" --cov
uv run mkdocs build --strict
```

`pre-commit` wraps the fast subset of the above so commits stay green:

```bash
uv run pre-commit install
uv run pre-commit run --all-files
```
