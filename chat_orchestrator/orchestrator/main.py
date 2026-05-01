"""CLI entrypoint for running the Anansi Chat Orchestrator API."""

from __future__ import annotations

import uvicorn


def main() -> None:
    """Run the FastAPI app using Uvicorn."""

    uvicorn.run("orchestrator.api.app:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":  # pragma: no cover
    main()
