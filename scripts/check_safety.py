"""Validate a saved Luna safety response locally; never sends data to an API."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fitagent.safety_schema import parse_safety  # noqa: E402


class SafetyArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(
            1,
            "Параметры команды неверны. Укажите путь к JSON или -; дополнительные правила: --rule ИДЕНТИФИКАТОР.\n",
        )


def main():
    parser = SafetyArgumentParser(description="Проверить структурированный ответ безопасности Luna")
    parser.add_argument("file", help="Путь к JSON или - для стандартного ввода")
    parser.add_argument("--rule", action="append", default=[], help="Обязательный rule_id")
    args = parser.parse_args()
    try:
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
        result = parse_safety(text, ["system", *args.rule])
    except (OSError, UnicodeError, ValueError):
        print(
            "Проверка не пройдена. Проверьте файл, структуру, статусы и покрытие правил.",
            file=sys.stderr,
        )
        return 1
    print("Структура корректна. Статус: " + result["status"])
    return 2 if result["status"] == "block" else 0


if __name__ == "__main__":
    sys.exit(main())
