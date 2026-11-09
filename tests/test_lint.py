"""Regression tests for the project linter; no third-party test runner required."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "project_lint", Path(__file__).resolve().parents[1] / "scripts" / "lint.py"
)
lint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lint)


def fixture(directory, size=600):
    path = Path(directory) / "example.py"
    path.write_text("class Example:\n" + "    pass\n" * (size - 1), encoding="utf-8")
    path.with_name("Example.md").write_text(
        "# Example\n\n- Исходный файл: `example.py`\n"
        "- Версия документа: 0.1.0\n- Дата изменения: 2026-11-09\n"
        "\n## Назначение\nТестовый класс.\n\n## Основные обязанности\nПроверка лимитов.\n",
        encoding="utf-8",
    )
    return path


def test_size_boundaries():
    with tempfile.TemporaryDirectory() as directory:
        for size, expected_errors, expected_warnings in (
            (600, 0, 0),
            (601, 0, 1),
            (800, 0, 1),
            (801, 1, 0),
        ):
            errors, warnings = lint.check_file(fixture(directory, size))
            assert len(errors) == expected_errors, errors
            assert len(warnings) == expected_warnings, warnings


def test_documentation():
    with tempfile.TemporaryDirectory() as directory:
        path = fixture(directory)
        doc = path.with_name("Example.md")
        text = doc.read_text(encoding="utf-8")
        doc.write_text(text + "\n" * 101, encoding="utf-8")
        assert any("100 строк" in error for error in lint.check_file(path)[0])
        doc.write_text(text.replace("2026-11-09", "2026-02-30"), encoding="utf-8")
        assert any("дата" in error for error in lint.check_file(path)[0])
        doc.write_text(text.replace("0.1.0", "invalid"), encoding="utf-8")
        assert any("версия" in error for error in lint.check_file(path)[0])
        doc.unlink()
        assert lint.check_file(path)[0]


def test_syntax_errors():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "broken.py"
        path.write_text("class :", encoding="utf-8")
        assert lint.check_file(path)[0]


def test_name_collision():
    with tempfile.TemporaryDirectory() as directory:
        path = fixture(directory)
        path.with_name("other.py").write_text("class Example:\n    pass\n", encoding="utf-8")
        assert any("конфликт" in error for error in lint.check_project(Path(directory))[0])


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(test)
        for test in (
            test_size_boundaries,
            test_documentation,
            test_syntax_errors,
            test_name_collision,
        )
    )


if __name__ == "__main__":
    unittest.main()
