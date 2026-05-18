"""Entry point for ``python -m taxonomaid``."""

from __future__ import annotations

from taxonomaid.cli import app


def main() -> None:
    """Run the Typer CLI."""
    app()


if __name__ == "__main__":
    main()
