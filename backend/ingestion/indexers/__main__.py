"""Entry point for ``python -m ingestion.indexers`` (HBIM-022)."""

from __future__ import annotations

from ingestion.indexers.cli import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
