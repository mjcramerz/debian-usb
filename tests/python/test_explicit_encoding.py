from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOTS = (
    ROOT / "src/python",
    ROOT / "scripts",
    ROOT / "tests/python",
)
SHELL_SOURCE_ROOTS = (
    ROOT / "config-hooks",
    ROOT / "scripts",
    ROOT / "tests/shell",
)
SHELL_SOURCE_FILES = (ROOT / "secrets.sh",)
SUBPROCESS_TEXT_CALLS = {"run", "Popen", "check_output", "check_call", "call"}


def _dotted_name(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _keywords(node: ast.Call) -> dict[str, ast.AST]:
    return {item.arg: item.value for item in node.keywords if item.arg is not None}


def _string_literal(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _may_enable_text_mode(node: ast.AST | None) -> bool:
    if node is None:
        return False
    return not (isinstance(node, ast.Constant) and node.value in {False, None})


def _encoding_is_missing(node: ast.Call, positional_index: int) -> bool:
    keywords = _keywords(node)
    if "encoding" in keywords:
        value = keywords["encoding"]
        return isinstance(value, ast.Constant) and value.value is None
    if len(node.args) > positional_index:
        value = node.args[positional_index]
        return isinstance(value, ast.Constant) and value.value is None
    return True


def _implicit_encoding(node: ast.Call) -> str | None:
    name = _dotted_name(node.func)
    leaf = name.rsplit(".", 1)[-1]
    keywords = _keywords(node)

    if leaf == "read_text" and _encoding_is_missing(node, 0):
        return "read_text"
    if leaf == "write_text" and _encoding_is_missing(node, 1):
        return "write_text"

    if leaf == "open":
        if name in {"open", "builtins.open", "io.open", "os.fdopen"}:
            mode_index = 1
            encoding_index = 3
            default_mode = "r"
        elif name in {"gzip.open", "bz2.open"}:
            mode_index = 1
            encoding_index = 3
            default_mode = "rb"
        elif name == "lzma.open":
            mode_index = 1
            encoding_index = 999
            default_mode = "rb"
        else:
            mode_index = 0
            encoding_index = 2
            default_mode = "r"
        mode_node = keywords.get("mode")
        if mode_node is None and len(node.args) > mode_index:
            mode_node = node.args[mode_index]
        mode = _string_literal(mode_node)
        if mode_node is None:
            mode = default_mode
        if mode is None:
            if _encoding_is_missing(node, encoding_index):
                return f"{name} with dynamic mode"
        elif "b" not in mode and _encoding_is_missing(node, encoding_index):
            return f"{name} in text mode"

    if name == "io.TextIOWrapper" and _encoding_is_missing(node, 1):
        return "io.TextIOWrapper"

    if name.startswith("subprocess.") and leaf in SUBPROCESS_TEXT_CALLS:
        text_mode = (
            _may_enable_text_mode(keywords.get("text"))
            or _may_enable_text_mode(keywords.get("universal_newlines"))
            or _may_enable_text_mode(keywords.get("encoding"))
            or _may_enable_text_mode(keywords.get("errors"))
        )
        if text_mode and _encoding_is_missing(node, 999):
            return f"{name} in text mode"

    if name in {
        "tempfile.NamedTemporaryFile",
        "tempfile.TemporaryFile",
        "tempfile.SpooledTemporaryFile",
    }:
        mode_node = keywords.get("mode") or (node.args[0] if node.args else None)
        mode = _string_literal(mode_node) or "w+b"
        if "b" not in mode and _encoding_is_missing(node, 999):
            return f"{name} in text mode"

    return None


def _tree_violations(tree: ast.AST, path: Path, *, line_offset: int = 0) -> list[str]:
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        issue = _implicit_encoding(node)
        if issue is not None:
            relative_path = path.relative_to(ROOT)
            violations.append(f"{relative_path}:{line_offset + node.lineno}: {issue}")
    return violations


def _embedded_python_heredocs(path: Path) -> list[tuple[int, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    heredocs: list[tuple[int, str]] = []
    index = 0
    while index < len(lines):
        if "<<'PY'" not in lines[index]:
            index += 1
            continue
        source_line = index + 2
        delimiter_index = index + 1
        while delimiter_index < len(lines) and lines[delimiter_index] != "PY":
            delimiter_index += 1
        if delimiter_index >= len(lines):
            relative_path = path.relative_to(ROOT)
            raise AssertionError(f"unterminated Python heredoc: {relative_path}:{index + 1}")
        source = "\n".join(lines[index + 1 : delimiter_index]) + "\n"
        heredocs.append((source_line, source))
        index = delimiter_index + 1
    return heredocs


class ExplicitEncodingTests(unittest.TestCase):
    def test_repository_python_text_io_declares_encoding(self) -> None:
        source_files = list(ROOT.glob("*.py"))
        for source_root in SOURCE_ROOTS:
            source_files.extend(source_root.rglob("*.py"))

        violations: list[str] = []
        for path in sorted(set(source_files)):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            violations.extend(_tree_violations(tree, path))

        self.assertEqual(violations, [], "implicit text encodings:\n" + "\n".join(violations))

    def test_embedded_python_text_io_declares_encoding(self) -> None:
        shell_files = list(SHELL_SOURCE_FILES)
        for source_root in SHELL_SOURCE_ROOTS:
            shell_files.extend(source_root.rglob("*.sh"))

        violations: list[str] = []
        for path in sorted(set(shell_files)):
            for source_line, source in _embedded_python_heredocs(path):
                tree = ast.parse(source, filename=f"{path}:{source_line}")
                violations.extend(_tree_violations(tree, path, line_offset=source_line - 1))

        self.assertEqual(
            violations,
            [],
            "implicit embedded-Python encodings:\n" + "\n".join(violations),
        )


if __name__ == "__main__":
    unittest.main()
