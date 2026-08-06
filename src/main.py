"""Entry point: python -m src.main"""

from __future__ import annotations

import logging

import uvicorn

from .api.server import create_app
from .env import load_env

import os

# A host assigns the port; 8000 is only the local default. Read here rather than baked in,
# because a container that ignores $PORT binds somewhere the platform is not listening and
# looks exactly like a crashed app.
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    # Before create_app, which builds the advisor and resolves its key.
    load_env()
    logger = logging.getLogger("neociv")
    logger.info("serving on %s:%d", HOST, PORT)
    uvicorn.run(create_app(), host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
