"""Entry point: python -m src.main"""

from __future__ import annotations

import logging

import uvicorn

from .api.server import create_app
from .env import load_env

HOST = "127.0.0.1"
PORT = 8000


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    # Before create_app, which builds the advisor and resolves its key.
    load_env()
    uvicorn.run(create_app(), host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
