"""Console entry point and dependency wiring."""

import argparse
import asyncio
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from fitagent.nutrition import NutritionShopping
from fitagent.provider import ModelClient
from fitagent.service import FitnessService
from fitagent.storage import Storage
from fitagent.vkusvill import VkusvillClient


def build_service(store):
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
    nutrition = NutritionShopping(client, VkusvillClient(audit=audit), audit=audit)

    def chat_plan(turn):
        from fitagent.plan_mcp import request_plan

        return asyncio.run(request_plan(store.directory, "", turn=turn))

    return FitnessService(store, client, nutrition=nutrition, chat_plan=chat_plan)


def main(argv=None, *, from_chat=False):
    parser = argparse.ArgumentParser(description="FitAgent · терминальный фитнес-помощник")
    parser.add_argument(
        "command",
        nargs="?",
        choices=["logs", "mcp"],
        help="logs — журнал, mcp — локальный сервер по stdio",
    )
    args = parser.parse_args(argv)
    try:
        # Explicit lookup: current directory, then user config. Never walk arbitrary parents.
        load_dotenv(Path.cwd() / ".env")
        load_dotenv(Path.home() / ".config" / "fitagent" / ".env")
        directory = os.environ.get("FITAGENT_DATA_DIR") or str(
            Path.home() / ".local" / "share" / "fitagent"
        )
        store = Storage(directory)
        service = build_service(store) if args.command != "logs" else None
    except Exception:
        parser.exit(
            1,
            "Не удалось запустить FitAgent. Проверьте доступ к папке данных и настройки API в .env.\n",
        )
    if args.command == "mcp":
        from fitagent.plan_mcp import serve

        serve(service, from_chat=from_chat)
        return
    if args.command == "logs":
        try:
            print(store.activity_logs() or "Событий пока нет.")
        except Exception:
            parser.exit(
                1,
                "Не удалось загрузить журнал. Повторите попытку; проверьте доступ к папке данных.\n",
            )
        return

    from fitagent.ui import FitAgentApp

    FitAgentApp(service).run()
