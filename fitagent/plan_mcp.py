"""Local MCP stdio server and the TUI's single-tool client."""

import asyncio
import json
import os
import sys
from time import monotonic

from fitagent.meal_plan import PROFILE_ERROR, create_plan, plan_from_context
from fitagent.nutrition import NUTRITION_ERROR
from fitagent.nutrition_targets import TARGET_RANGE_ERROR
from fitagent.safety_schema import SAFETY_ERROR
from fitagent.vkusvill import PROTOCOL

NAME = "create_nutrition_plan"
ERROR = (
    "Не удалось составить программу питания. Проверьте подключение, ключ Luna и повторите /plan."
)
TOOL = {
    "name": NAME,
    "description": "Составить программу питания по текущей цели и памяти FitAgent.",
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"requirements": {"type": "string", "maxLength": 2000}},
    },
}


def tool_call(service, arguments, turn=None):
    started = monotonic()
    details = {"operation": NAME}
    service.audit("mcp_started", details)
    invalid = "Укажите условия программы текстом до 2000 символов после /plan."
    failed = True
    try:
        if not isinstance(arguments, dict) or arguments.keys() - {"requirements"}:
            raise ValueError(invalid)
        requirements = arguments.get("requirements", "")
        if not isinstance(requirements, str) or len(requirements) > 2000:
            raise ValueError(invalid)
        if turn is None:
            message = create_plan(service, requirements)
        else:
            message = plan_from_context(service, turn["payload"], turn["memory"], shop=turn["shop"])
        failed = False
    except Exception as exc:
        message = ERROR
        if isinstance(exc, ValueError):
            reason = str(exc)
            if reason in {
                invalid,
                PROFILE_ERROR,
                SAFETY_ERROR,
                NUTRITION_ERROR,
                TARGET_RANGE_ERROR,
            } or reason.startswith(
                (
                    "Не могу выполнить эту просьбу.\nПричина:",
                    "Не могу показать подготовленный ответ.\nПричина:",
                )
            ):
                message = reason
    service.audit(
        "mcp_failed" if failed else "mcp_finished",
        {**details, "duration_ms": round((monotonic() - started) * 1000)},
    )
    return {"content": [{"type": "text", "text": message}], "isError": failed}


def serve(service, source=None, sink=None, *, from_chat=False):
    source = sys.stdin if source is None else source
    sink = sys.stdout if sink is None else sink
    initialized = False
    for line in source:
        identifier = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                raise ValueError
            method = request["method"]
            if "id" not in request:
                continue
            identifier = request["id"]
            params = request.get("params", {})
            if method == "initialize":
                result = {
                    "protocolVersion": PROTOCOL,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fitagent", "version": "0.1.0"},
                }
                initialized = True
            elif method == "ping":
                result = {}
            elif initialized and method == "tools/list":
                result = {"tools": [TOOL]}
            elif initialized and method == "tools/call" and params.get("name") == NAME:
                turn = params.get("_meta", {}).get("fitagent/turn") if from_chat else None
                result = tool_call(service, params.get("arguments", {}), turn)
            else:
                reply = {"error": {"code": -32601, "message": "Метод или инструмент недоступен."}}
                result = None
            if result is not None:
                reply = {"result": result}
        except (ValueError, KeyError, TypeError, AttributeError):
            reply = {"error": {"code": -32600, "message": "Проверьте формат запроса MCP."}}
        print(
            json.dumps({"jsonrpc": "2.0", "id": identifier, **reply}, ensure_ascii=False),
            file=sink,
            flush=True,
        )


async def request_plan(directory, requirements, *, turn=None):
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "fitagent.plan_mcp",
        *(["--chat-turn"] if turn is not None else []),
        env=dict(os.environ, FITAGENT_DATA_DIR=str(directory)),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=1024 * 1024,
    )

    async def send(method, params, identifier=None):
        packet = {"jsonrpc": "2.0", "method": method, "params": params}
        if identifier is not None:
            packet["id"] = identifier
        proc.stdin.write(json.dumps(packet).encode() + b"\n")
        await proc.stdin.drain()
        if identifier is not None:
            response = json.loads(await proc.stdout.readline())
            if response.get("id") != identifier or response.get("jsonrpc") != "2.0":
                raise ValueError
            return response["result"]

    try:
        hello = await send(
            "initialize",
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "fitagent-tui", "version": "0.1.0"},
            },
            1,
        )
        if hello["protocolVersion"] != PROTOCOL:
            raise ValueError
        await send("notifications/initialized", {})
        params = {"name": NAME, "arguments": {"requirements": requirements}}
        if turn is not None:
            params["_meta"] = {"fitagent/turn": turn}
        result = await send("tools/call", params, 2)
        message = result["content"][0]["text"]
    except (ValueError, KeyError, IndexError, TypeError, OSError):
        raise ValueError(ERROR) from None
    finally:
        proc.stdin.close()
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), 1)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
    if result.get("isError"):
        raise ValueError(message)
    return message


if __name__ == "__main__":
    from fitagent.cli import main

    main(["mcp"], from_chat=sys.argv[1:] == ["--chat-turn"])
