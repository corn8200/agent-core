#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import re
import tokenize
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

PYTHON_SUFFIXES = {".py", ".pyw"}
TEXT_SUFFIXES = {".bash", ".sh", ".zsh"}
SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "backups",
    "mcp-node",
    "node_modules",
}
SKIP_NAMES = {"uv.lock"}

HTTP_LITERAL_RE = re.compile(r"https?://api\.anthropic\.com|api\.anthropic\.com", re.I)
CLAUDE_CLI_RE = re.compile(
    r"(?<![\w.-])[\"']?(?:claude|<claude_bin>|claude_bin|\$\{?CLAUDE_BIN\}?)[\"']?"
    r"\s+(?:-[^\n;&|]*\s*)?(?:-p|--print)\b",
    re.I,
)
SHELL_HTTP_RE = re.compile(r"\b(?:curl|httpx|wget)\b[^\n;&|]*api\.anthropic\.com", re.I)
PYTHON_COMMAND_RE = re.compile(
    r"\bpython(?:3(?:\.\d+)?)?\s+(?:-c|-m)\b[^\n;&|]*(?:anthropic|api\.anthropic\.com)",
    re.I,
)

WRAPPER_IMPORTS: dict[str, dict[str, tuple[str, ...]]] = {
    "core.mac_sdk": {
        "allowlist": (
            "core/mac_sdk.py",
            # OPJUNE_PENDING_MIGRATION_2026_06_15: active Mac-local callers
            # remain on the pre-OpJune wrapper until the post-Jun-15 broker
            # path is available for agent-core.
            "core/imessage_triage/classify.py",
            "core/swarm_dispatch.py",
            "tests/test_opjune_raw_call_scanner.py",
        ),
    },
    "core.mac_sdk.query": {
        "allowlist": (
            "core/mac_sdk.py",
            # OPJUNE_PENDING_MIGRATION_2026_06_15: active Mac-local callers
            # remain on the pre-OpJune wrapper until the post-Jun-15 broker
            # path is available for agent-core.
            "core/imessage_triage/classify.py",
            "core/swarm_dispatch.py",
            "tests/test_opjune_raw_call_scanner.py",
        ),
    },
    "vps_sdk": {
        "allowlist": ("tests/test_opjune_raw_call_scanner.py",),
    },
    "anthropic_update_watcher._sdk_worker": {
        "allowlist": (
            "_sdk_worker.py",
            "tests/test_opjune_raw_call_scanner.py",
        ),
    },
    "_sdk_worker": {
        "allowlist": (
            "_sdk_worker.py",
            "tests/test_opjune_raw_call_scanner.py",
        ),
    },
    "routines_guard": {
        "allowlist": ("tests/test_opjune_raw_call_scanner.py",),
    },
}
WRAPPER_IMPORT_PREFIXES = ("vps_sdk.",)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    column: int
    pattern: str
    detail: str
    allowed: bool = False
    allowed_reason: str | None = None


class RawCallVisitor(ast.NodeVisitor):
    def __init__(self, source: str) -> None:
        self.source = source
        self.imported_modules: set[str] = set()
        self.imported_callables: set[str] = set()
        self.findings: list[tuple[int, int, str, str]] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "anthropic":
                self.imported_modules.add(alias.asname or alias.name)
            wrapper = wrapper_module_match(alias.name)
            if wrapper:
                self.findings.append(
                    (
                        node.lineno,
                        node.col_offset,
                        "wrapper-import",
                        f"import {alias.name} reaches {wrapper}",
                    )
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "anthropic":
            for alias in node.names:
                if alias.name in {"Anthropic", "AsyncAnthropic"}:
                    self.imported_callables.add(alias.asname or alias.name)
        module = node.module or ""
        candidates = [module]
        candidates.extend(f"{module}.{alias.name}" for alias in node.names if module)
        if module == "core":
            candidates.extend(f"core.{alias.name}" for alias in node.names)
        if module == "anthropic_update_watcher":
            candidates.extend(f"anthropic_update_watcher.{alias.name}" for alias in node.names)
        for candidate in candidates:
            wrapper = wrapper_module_match(candidate)
            if wrapper:
                self.findings.append(
                    (
                        node.lineno,
                        node.col_offset,
                        "wrapper-import",
                        f"from {module} import ... reaches {wrapper}",
                    )
                )
                break
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        pattern = self._classify_call(node)
        if pattern is not None:
            self.findings.append((node.lineno, node.col_offset, pattern[0], pattern[1]))
        self.generic_visit(node)

    def _classify_call(self, node: ast.Call) -> tuple[str, str] | None:
        func_name = dotted_name(node.func)

        if func_name in {"anthropic.Anthropic", "anthropic.AsyncAnthropic"}:
            return ("anthropic-client", f"{func_name}()")
        if isinstance(node.func, ast.Name) and node.func.id in self.imported_callables:
            return ("anthropic-client", f"{node.func.id}() from anthropic")
        if (
            isinstance(node.func, ast.Name)
            and node.func.id in {"Anthropic", "AsyncAnthropic"}
            and node.func.id not in self.imported_callables
        ):
            return ("anthropic-client", f"{node.func.id}()")
        if func_name and func_name.endswith(".messages.create"):
            return ("anthropic-messages-create", f"{func_name}()")

        if is_importlib_anthropic(node):
            return ("dynamic-anthropic-import", "dynamic import of anthropic")
        wrapper = importlib_wrapper_module(node)
        if wrapper:
            return ("wrapper-import", f"dynamic import reaches {wrapper}")
        if is_getattr_anthropic_client(node):
            return ("dynamic-anthropic-getattr", "getattr(..., Anthropic/AsyncAnthropic/create)")
        if call_has_anthropic_url(node):
            return ("anthropic-http", "call argument references api.anthropic.com")
        if call_shells_raw_claude(node):
            return ("claude-cli", "subprocess command invokes claude -p/--print")
        return None


def dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def string_value(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def is_importlib_anthropic(node: ast.Call) -> bool:
    func_name = dotted_name(node.func)
    if func_name in {"importlib.import_module", "__import__"}:
        return bool(node.args and string_value(node.args[0]) == "anthropic")
    return False


def importlib_wrapper_module(node: ast.Call) -> str | None:
    func_name = dotted_name(node.func)
    if func_name not in {"importlib.import_module", "__import__"}:
        return None
    if not node.args:
        return None
    value = string_value(node.args[0])
    if not value:
        return None
    return wrapper_module_match(value)


def is_getattr_anthropic_client(node: ast.Call) -> bool:
    if dotted_name(node.func) != "getattr" or len(node.args) < 2:
        return False
    attr = string_value(node.args[1])
    if attr in {"Anthropic", "AsyncAnthropic"}:
        return True
    if attr == "create":
        target = dotted_name(node.args[0]) or ""
        return target.endswith(".messages")
    return False


def call_has_anthropic_url(node: ast.Call) -> bool:
    func_name = dotted_name(node.func) or ""
    if not any(name in func_name for name in ("request", "get", "post", "put", "patch", "delete")):
        return False
    values: list[str] = []
    for arg in node.args:
        value = string_value(arg)
        if value:
            values.append(value)
    for keyword in node.keywords:
        value = string_value(keyword.value)
        if value:
            values.append(value)
    return any(HTTP_LITERAL_RE.search(value) for value in values)


def call_shells_raw_claude(node: ast.Call) -> bool:
    func_name = dotted_name(node.func) or ""
    if func_name not in {
        "subprocess.run",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.Popen",
        "os.system",
        "os.popen",
    }:
        return False
    if not node.args:
        return False
    command = command_text(node.args[0])
    return bool(command and CLAUDE_CLI_RE.search(command))


def command_text(node: ast.AST) -> str | None:
    value = string_value(node)
    if value is not None:
        return value
    if isinstance(node, (ast.List, ast.Tuple)):
        parts: list[str] = []
        for elt in node.elts:
            part = string_value(elt)
            if part is None:
                part = f"<{elt.id}>" if isinstance(elt, ast.Name) else "<expr>"
            parts.append(part)
        return " ".join(parts)
    return None


def python_findings(path: Path, rel: str, text: str) -> list[Finding]:
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return text_findings(rel, text)

    visitor = RawCallVisitor(text)
    visitor.visit(tree)
    findings = [
        classify(Finding(rel, line, column + 1, pattern, detail), text)
        for line, column, pattern, detail in visitor.findings
    ]
    findings.extend(python_token_findings(rel, text, docstring_lines(tree)))
    return sort_findings(dedupe(findings))


def docstring_lines(tree: ast.AST) -> set[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            end_lineno = getattr(node, "end_lineno", node.lineno)
            lines.update(range(node.lineno, end_lineno + 1))
            continue
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if not isinstance(first, ast.Expr) or not isinstance(first.value, ast.Constant):
            continue
        if not isinstance(first.value.value, str):
            continue
        end_lineno = getattr(first, "end_lineno", first.lineno)
        lines.update(range(first.lineno, end_lineno + 1))
    return lines


def python_token_findings(rel: str, text: str, ignored_lines: set[int]) -> list[Finding]:
    findings: list[Finding] = []
    try:
        tokens = tokenize.generate_tokens(iter(text.splitlines(keepends=True)).__next__)
        for token in tokens:
            if token.type != tokenize.STRING or token.start[0] in ignored_lines:
                continue
            try:
                literal = ast.literal_eval(token.string)
            except Exception:
                continue
            if not isinstance(literal, str):
                continue
            for pattern, detail in text_patterns(literal):
                finding = Finding(rel, token.start[0], token.start[1] + 1, pattern, detail)
                findings.append(classify(finding, text))
    except (StopIteration, tokenize.TokenError):
        pass
    return findings


def text_findings(rel: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for pattern, detail in text_patterns(line):
            column = max(1, line.lower().find("claude") + 1)
            if "anthropic" in detail.lower():
                column = max(1, line.lower().find("anthropic") + 1)
            finding = Finding(rel, line_no, column, pattern, detail)
            findings.append(classify(finding, text))
    return sort_findings(dedupe(findings))


def text_patterns(text: str) -> Iterable[tuple[str, str]]:
    if CLAUDE_CLI_RE.search(text):
        yield ("claude-cli", "command invokes claude -p/--print")
    if SHELL_HTTP_RE.search(text):
        yield ("anthropic-http", "shell command calls api.anthropic.com")
    if PYTHON_COMMAND_RE.search(text):
        yield ("python-anthropic-command", "python command references anthropic")
    if re.search(r"\banthropic\.messages\.create\b", text):
        yield ("anthropic-messages-create", "anthropic.messages.create")
    if re.search(r"\b(?:Anthropic|AsyncAnthropic)\s*\(", text):
        yield ("anthropic-client", "Anthropic/AsyncAnthropic constructor")


def classify(finding: Finding, source: str) -> Finding:
    reason = allowed_reason(finding, source)
    if reason is None:
        return finding
    return Finding(
        finding.path,
        finding.line,
        finding.column,
        finding.pattern,
        finding.detail,
        allowed=True,
        allowed_reason=reason,
    )


def allowed_reason(finding: Finding, source: str) -> str | None:
    path = finding.path
    line = source_line(source, finding.line)
    lowered = line.lower()

    if path.startswith("tests/") or "/tests/" in path:
        return "test fixture/assertion"
    if path in {"overseer_v2/opjune/claude_call.py", "overseer_v2/claude_call.py"}:
        return "OpJune invocation layer"
    if path in {"overseer_v2/opjune/egress_client.py", "opjune_egress/server.py"}:
        return "OpJune egress invocation layer"
    if path == "overseer_v2/opjune/hooks/__init__.py":
        return "OpJune deny shim"
    if path == "overseer_v2/command_worker.py" and (
        "raw_llm_dispatch_pattern" in source.lower()
        or "raw quick-answer bypass" in source.lower()
        or "never to a direct claude subprocess" in source.lower()
    ):
        return "OpJune deny shim"
    if path == "bin/run_brain.sh" and "opjune_legacy_brai" in source.lower():
        return "legacy brain raw Claude path disabled by OpJune deny shim"
    if (
        path == "bin/opjune-raw-call-scanner"
        or path == ".opjune/raw-call-scanner.py"
        or path.endswith("/.opjune/raw-call-scanner.py")
    ):
        return "scanner signature definition"
    if finding.pattern == "wrapper-import":
        registry_reason = wrapper_allowed_reason(path, finding.detail)
        if registry_reason:
            return registry_reason
    if "raw claude/anthropic invocation refused" in lowered:
        return "OpJune deny shim"
    return None


def wrapper_module_match(module: str) -> str | None:
    if module in WRAPPER_IMPORTS:
        return module
    for prefix in WRAPPER_IMPORT_PREFIXES:
        if module.startswith(prefix):
            return prefix.rstrip(".")
    return None


def wrapper_allowed_reason(path: str, detail: str) -> str | None:
    wrapper = None
    for module in WRAPPER_IMPORTS:
        if module in detail:
            wrapper = module
            break
    if wrapper is None:
        for prefix in WRAPPER_IMPORT_PREFIXES:
            if prefix.rstrip(".") in detail:
                wrapper = prefix.rstrip(".")
                break
    if wrapper is None:
        return None
    allowlist = WRAPPER_IMPORTS.get(wrapper, {}).get("allowlist", ())
    if any(path == allowed or path.endswith(f"/{allowed}") for allowed in allowlist):
        return f"wrapper registry allowlist for {wrapper}"
    return None


def source_line(source: str, line_no: int) -> str:
    lines = source.splitlines()
    if 1 <= line_no <= len(lines):
        return lines[line_no - 1]
    return ""


def dedupe(findings: Iterable[Finding]) -> list[Finding]:
    seen: set[tuple[str, int, str, bool]] = set()
    result: list[Finding] = []
    for finding in findings:
        key = (finding.path, finding.line, finding.pattern, finding.allowed)
        if key in seen:
            continue
        seen.add(key)
        result.append(finding)
    return result


def sort_findings(findings: Iterable[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda finding: (finding.path, finding.line, finding.column))


def iter_files(paths: Iterable[Path]) -> Iterable[Path]:
    for path in paths:
        if path.is_file():
            if should_scan(path):
                yield path
            continue
        if path.is_dir():
            for child in path.rglob("*"):
                if child.is_file() and should_scan(child):
                    yield child


def should_scan(path: Path) -> bool:
    if path.name in SKIP_NAMES:
        return False
    if any(part in SKIP_DIRS for part in path.parts):
        return False
    if path.suffix in PYTHON_SUFFIXES | TEXT_SUFFIXES:
        return True
    return path.parent.name == "bin" and path.suffix == ""


def relpath(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def scan(paths: Iterable[Path], *, root: Path) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    scanned = 0
    for path in sorted(iter_files(paths)):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        scanned += 1
        rel = relpath(path, root)
        if path.suffix in PYTHON_SUFFIXES:
            findings.extend(python_findings(path, rel, text))
        else:
            findings.extend(text_findings(rel, text))
    return sort_findings(dedupe(findings)), scanned


def render_human(violations: list[Finding], allowed: list[Finding], scanned: int) -> str:
    lines: list[str] = []
    if violations:
        lines.append(
            "raw-call scanner: "
            f"{len(violations)} violation(s), "
            f"{len(allowed)} allowed occurrence(s), "
            f"{scanned} file(s) scanned"
        )
        for finding in violations:
            label = (
                "VIOLATION_WRAPPER_IMPORT"
                if finding.pattern == "wrapper-import"
                else "VIOLATION_RAW_CALL"
            )
            lines.append(
                f"{label} {finding.path}:{finding.line}:{finding.column} "
                f"{finding.pattern} - {finding.detail}"
            )
    else:
        lines.append(
            "raw-call scanner: "
            f"no violations, {len(allowed)} allowed occurrence(s), {scanned} file(s) scanned"
        )
    for finding in allowed:
        lines.append(
            f"ALLOWED {finding.path}:{finding.line}:{finding.column} "
            f"{finding.pattern} - {finding.allowed_reason}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Detect direct Anthropic/Claude raw-call patterns outside OpJune-approved layers."
        )
    )
    parser.add_argument("paths", nargs="*", default=["."], help="Files or directories to scan.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args(argv)

    root = Path.cwd()
    findings, scanned = scan([Path(path) for path in args.paths], root=root)
    violations = [finding for finding in findings if not finding.allowed]
    allowed = [finding for finding in findings if finding.allowed]

    if args.json:
        payload = {
            "ok": not violations,
            "scanned_files": scanned,
            "violations": [asdict(finding) for finding in violations],
            "allowed": [asdict(finding) for finding in allowed],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_human(violations, allowed, scanned))
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
