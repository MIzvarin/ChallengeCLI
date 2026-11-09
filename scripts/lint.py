"""Run Ruff and project-specific class/documentation checks (Python 3.9+)."""

import ast
import os
import re
import subprocess
import sys
import tokenize
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORED = {".git", ".venv", "venv", "node_modules", "__pycache__", ".opencode"}


def check_file(path):
    """Return (errors, warnings) without executing the inspected Python file."""
    errors, warnings = [], []
    try:
        with tokenize.open(path) as stream:
            tree = ast.parse(stream.read(), filename=str(path))
    except (OSError, SyntaxError, UnicodeError) as exc:
        return [f"{path}: {exc}"], warnings

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        start = min([node.lineno] + [item.lineno for item in node.decorator_list])
        size = node.end_lineno - start + 1
        label = f"{path}:{node.lineno}: {node.name}"
        if size > 800:
            errors.append(f"{label}: {size} строк, максимум 800")
        elif size > 600:
            warnings.append(f"{label}: {size} строк, рекомендуется не более 600")
        doc = path.with_name(f"{node.name}.md")
        try:
            text = doc.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            errors.append(f"{label}: недоступна документация {doc.name}: {exc}")
            continue
        if len(text.splitlines()) > 100:
            errors.append(f"{doc}: документация превышает 100 строк")
        if not re.search(r"^- Версия документа: \d+\.\d+\.\d+\s*$", text, re.M):
            errors.append(f"{doc}: требуется версия документа MAJOR.MINOR.PATCH")
        changed = re.search(r"^- Дата изменения: (\d{4}-\d{2}-\d{2})\s*$", text, re.M)
        try:
            if changed is None:
                raise ValueError("missing date")
            date.fromisoformat(changed.group(1))
        except ValueError:
            errors.append(f"{doc}: требуется корректная дата изменения YYYY-MM-DD")
        if f"`{path.name}`" not in text:
            errors.append(f"{doc}: требуется ссылка на исходный файл `{path.name}`")
        for heading in (f"# {node.name}", "## Назначение", "## Основные обязанности"):
            if heading not in text.splitlines():
                errors.append(f"{doc}: отсутствует заголовок {heading}")
    return errors, warnings


def check_project(root):
    errors, warnings = [], []
    names = {}
    for directory, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(item for item in directories if item not in IGNORED)
        for filename in sorted(files):
            path = Path(directory) / filename
            if path.suffix != ".py" or path.is_symlink():
                continue
            file_errors, file_warnings = check_file(path)
            errors.extend(file_errors)
            warnings.extend(file_warnings)
            try:
                with tokenize.open(path) as stream:
                    tree = ast.parse(stream.read())
            except (OSError, SyntaxError, UnicodeError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                key = (path.parent, node.name)
                if key in names:
                    errors.append(f"{path}: конфликт документации {node.name}.md с {names[key]}")
                names[key] = path
    return errors, warnings


def main():
    failed = False
    for arguments in (["check", "."], ["format", "--check", "."]):
        result = subprocess.run([sys.executable, "-m", "ruff", *arguments], cwd=ROOT)
        failed = failed or result.returncode != 0
    errors, warnings = check_project(ROOT)
    for warning in warnings:
        print(f"WARNING: {warning}")
    for error in errors:
        print(f"ERROR: {error}")
    if failed or errors:
        print("LINT FAILED")
        return 1
    print("LINT OK" + (" (с предупреждениями)" if warnings else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
