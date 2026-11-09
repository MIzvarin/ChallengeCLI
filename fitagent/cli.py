"""Console entry point and dependency wiring."""

import argparse
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from fitagent.provider import ModelClient
from fitagent.service import FitnessService
from fitagent.storage import Storage


def main():
    parser = argparse.ArgumentParser(description="FitAgent · терминальный фитнес-помощник")
    parser.add_argument("command", nargs="?", choices=["logs"], help="Последние события приложения")
    args = parser.parse_args()
    # Explicit lookup: current directory, then user config. Never walk arbitrary parents.
    load_dotenv(Path.cwd() / ".env")
    load_dotenv(Path.home() / ".config" / "fitagent" / ".env")
    directory = os.environ.get("FITAGENT_DATA_DIR") or str(
        Path.home() / ".local" / "share" / "fitagent"
    )
    store = Storage(directory)
    if args.command == "logs":
        print(store.logs() or "Событий пока нет.")
        return

    def audit(kind, details):
        store.event(kind, details, datetime.now().astimezone())

    client = ModelClient(
        os.environ.get("OPENCODE_API_KEY", ""),
        os.environ.get(
            "FITAGENT_API_URL", "https://opencode.ai/inference/go/openai/v1/chat/completions"
        ),
        os.environ.get("FITAGENT_MODEL", "deepseek-v4.1-flash"),
        store.session_id(),
        audit,
    )
    from fitagent.ui import FitAgentApp

    FitAgentApp(FitnessService(store, client)).run()
