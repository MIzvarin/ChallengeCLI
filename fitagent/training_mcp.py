"""Transport and tool contracts for the local FitAgent training MCP server."""

import json
import re
from time import monotonic

from fitagent import training
from fitagent.memory_schema import object_schema, string_schema
from fitagent.plan_mcp import request_tool, serve_tools
from fitagent.safety_schema import SAFETY_ERROR

REQUIREMENTS = {**string_schema(2000), "minLength": 1}
DRAFT_ID = {**string_schema(32), "pattern": "^[0-9a-f]{32}$"}
TOOLS = (
    {
        "name": training.CREATE,
        "description": "Предложить черновик недели или уточнение. Передайте draft_id для продолжения.",
        "inputSchema": {
            **object_schema({"requirements": REQUIREMENTS, "draft_id": DRAFT_ID}),
            "required": ["requirements"],
        },
    },
    {
        "name": training.CONFIRM,
        "description": "Записать показанный черновик в память после явного согласия пользователя.",
        "inputSchema": object_schema({"draft_id": DRAFT_ID}),
    },
    {
        "name": training.TODAY,
        "description": "Вернуть статус training, rest, paused или unknown и упражнения на сегодня.",
        "inputSchema": object_schema({}),
    },
)


def tool_value(service, name, arguments):
    if not isinstance(arguments, dict):
        raise ValueError(training.ARGUMENT_ERROR)
    if name == training.TODAY and not arguments:
        return training.today_training(service)
    draft_id = arguments.get("draft_id")
    if "draft_id" in arguments and (
        not isinstance(draft_id, str) or re.fullmatch(DRAFT_ID["pattern"], draft_id) is None
    ):
        raise ValueError(training.ARGUMENT_ERROR)
    if name == training.CONFIRM and set(arguments) == {"draft_id"}:
        return training.confirm_program(service, draft_id)
    if name == training.CREATE and not arguments.keys() - {"requirements", "draft_id"}:
        requirements = arguments.get("requirements")
        if isinstance(requirements, str) and 1 <= len(requirements.strip()) <= 2000:
            return training.create_program(service, requirements.strip(), draft_id)
    raise ValueError(training.ARGUMENT_ERROR)


def run_tool(service, name, arguments):
    started = monotonic()
    service.audit("mcp_started", {"operation": name})
    status = "mcp_failed"
    try:
        value = tool_value(service, name, arguments)
        status = "mcp_finished"
        return value
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) else ""
        if reason in (
            training.ARGUMENT_ERROR,
            training.PROFILE_ERROR,
            training.STALE,
            training.HISTORY_ERROR,
            training.NOT_READY,
            SAFETY_ERROR,
        ) or reason.startswith(
            (
                "Не могу выполнить эту просьбу.\nПричина:",
                "Не могу показать подготовленный ответ.\nПричина:",
            )
        ):
            raise ValueError(reason) from None
        raise ValueError(training.ERROR) from None
    finally:
        service.audit(
            status, {"operation": name, "duration_ms": round((monotonic() - started) * 1000)}
        )


def serve(service, source=None, sink=None):
    def invoke(name, params):
        try:
            value = run_tool(service, name, params.get("arguments", {}))
            message = json.dumps(value, ensure_ascii=False)
            failed = False
        except ValueError as exc:
            message = str(exc)
            failed = True
        return {"content": [{"type": "text", "text": message}], "isError": failed}

    serve_tools("fitagent-training", TOOLS, invoke, source, sink)


async def request_training(directory, name, arguments):
    try:
        text = await request_tool(
            directory, name, arguments, training.ERROR, module="fitagent.training_mcp"
        )
        return json.loads(text)
    except (OSError, json.JSONDecodeError):
        raise ValueError(training.ERROR) from None


if __name__ == "__main__":
    from fitagent.cli import main

    main(["training-mcp"])
