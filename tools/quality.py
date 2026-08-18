#!/usr/bin/env python3
"""Deterministic repository quality gates and machine-readable evidence receipts."""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import tokenize
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

QUALITY_VERSION = "1.0.0"
RECEIPT_SCHEMA = "fdir/repository-quality-receipt/1"
FAILURE_RECEIPT_SCHEMA = "fdir/quality-failure-demonstration/1"
CACHE_SCHEMA = "fdir/repository-quality-cache/1"

EXCLUDED_PARTS = {
    ".git",
    ".issue6-export",
    ".tmp",
    ".validation",
    "__pycache__",
    "reports",
}
TEXT_SUFFIXES = {
    ".cddl",
    ".csv",
    ".json",
    ".jsonld",
    ".jq",
    ".md",
    ".mmd",
    ".py",
    ".sql",
    ".txt",
    ".yaml",
    ".yml",
}
TEXT_NAMES = {
    ".gitattributes",
    ".gitignore",
    ".python-version",
    "LICENSE",
    "NOTICE",
}


@dataclass
class GateResult:
    """One deterministic gate result."""

    gate_id: str
    status: str
    diagnostics: list[str] = field(default_factory=list)
    command: list[str] | None = None
    stdout: str = ""
    stderr: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def passed(
        cls,
        gate_id: str,
        *,
        diagnostics: Iterable[str] = (),
        command: Sequence[str] | None = None,
        stdout: str = "",
        stderr: str = "",
        details: dict[str, Any] | None = None,
    ) -> "GateResult":
        return cls(
            gate_id=gate_id,
            status="passed",
            diagnostics=sorted(set(diagnostics)),
            command=list(command) if command else None,
            stdout=stdout,
            stderr=stderr,
            details=details or {},
        )

    @classmethod
    def failed(
        cls,
        gate_id: str,
        diagnostics: Iterable[str],
        *,
        command: Sequence[str] | None = None,
        stdout: str = "",
        stderr: str = "",
        details: dict[str, Any] | None = None,
    ) -> "GateResult":
        return cls(
            gate_id=gate_id,
            status="failed",
            diagnostics=sorted(set(diagnostics)),
            command=list(command) if command else None,
            stdout=stdout,
            stderr=stderr,
            details=details or {},
        )

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.gate_id,
            "status": self.status,
            "diagnostics": self.diagnostics,
        }
        if self.command is not None:
            payload["command"] = self.command
        if self.stdout:
            payload["stdout"] = self.stdout
        if self.stderr:
            payload["stderr"] = self.stderr
        if self.stdout or self.stderr:
            payload["outputSha256"] = sha256_text(self.stdout + "\0" + self.stderr)
        if self.details:
            payload["details"] = self.details
        return payload


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def stable_json_bytes(value: Any, *, indent: int | None = None) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=indent,
            separators=(",", ":") if indent is None else None,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = stable_json_bytes(value, indent=2)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(data)
    temporary.replace(path)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def import_tool(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def is_excluded(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    return any(part in EXCLUDED_PARTS for part in relative.parts)


def repository_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and not is_excluded(path, root)
    )


def source_digest(root: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    files = repository_files(root)
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest(), len(files)


def normalize_output(value: str, root: Path) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace(str(root.resolve()), ".")
    normalized = re.sub(r"(Ran \d+ tests?) in \d+(?:\.\d+)?s", r"\1 in <duration>s", normalized)
    normalized = re.sub(r"\(\d+(?:\.\d+)?s\)", "(<duration>s)", normalized)
    return normalized.rstrip()


def display_command(command: Sequence[str], root: Path) -> list[str]:
    rendered: list[str] = []
    for index, item in enumerate(command):
        if index == 0 and Path(item).resolve() == Path(sys.executable).resolve():
            rendered.append("python3")
        elif item == str(root.resolve()):
            rendered.append(".")
        else:
            rendered.append(item.replace(str(root.resolve()), "."))
    return rendered


def deterministic_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "TZ": "UTC",
        }
    )
    return environment


def command_gate(gate_id: str, root: Path, command: Sequence[str]) -> GateResult:
    completed = subprocess.run(
        list(command),
        cwd=root,
        env=deterministic_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    stdout = normalize_output(completed.stdout, root)
    stderr = normalize_output(completed.stderr, root)
    rendered = display_command(command, root)
    if completed.returncode == 0:
        return GateResult.passed(
            gate_id,
            command=rendered,
            stdout=stdout,
            stderr=stderr,
            details={"exitCode": completed.returncode},
        )
    return GateResult.failed(
        gate_id,
        [f"command exited with status {completed.returncode}"],
        command=rendered,
        stdout=stdout,
        stderr=stderr,
        details={"exitCode": completed.returncode},
    )


def gate_toolchain(root: Path) -> GateResult:
    gate_id = "toolchain"
    path = root / "quality/toolchain.json"
    failures: list[str] = []
    if not path.is_file():
        return GateResult.failed(gate_id, ["missing quality/toolchain.json"])
    try:
        policy = load_json(path)
    except (OSError, json.JSONDecodeError) as error:
        return GateResult.failed(gate_id, [f"invalid quality/toolchain.json: {error}"])
    if policy.get("schema") != "fdir/quality-toolchain/1":
        failures.append("unexpected toolchain schema")
    python_policy = policy.get("python", {})
    if python_policy.get("implementation") != "CPython":
        failures.append("toolchain must require CPython")
    minimum = tuple(int(item) for item in str(python_policy.get("minimum", "0.0")).split("."))
    maximum = tuple(
        int(item) for item in str(python_policy.get("maximumExclusive", "0.0")).split(".")
    )
    actual = sys.version_info[:2]
    if sys.implementation.name != "cpython":
        failures.append(f"unsupported Python implementation: {sys.implementation.name}")
    if actual < minimum or actual >= maximum:
        failures.append(
            "unsupported Python version: "
            f"{actual[0]}.{actual[1]} is outside {minimum[0]}.{minimum[1]} <= version < "
            f"{maximum[0]}.{maximum[1]}"
        )
    if policy.get("thirdPartyDependencies") != []:
        failures.append("repository quality command must have no third-party dependencies")
    details = {
        "implementation": platform.python_implementation(),
        "pythonVersion": platform.python_version(),
        "policySha256": sha256_bytes(path.read_bytes()),
    }
    if failures:
        return GateResult.failed(gate_id, failures, details=details)
    return GateResult.passed(gate_id, details=details)


def text_paths(root: Path) -> list[Path]:
    return [
        path
        for path in repository_files(root)
        if path.suffix.lower() in TEXT_SUFFIXES or path.name in TEXT_NAMES
    ]


def gate_text_format(root: Path) -> GateResult:
    gate_id = "text-format"
    failures: list[str] = []
    checked = 0
    for path in text_paths(root):
        checked += 1
        relative = path.relative_to(root).as_posix()
        data = path.read_bytes()
        if data.startswith(b"\xef\xbb\xbf"):
            failures.append(f"UTF-8 BOM is forbidden: {relative}")
        if b"\r" in data:
            failures.append(f"CR or CRLF line ending is forbidden: {relative}")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            failures.append(f"invalid UTF-8 in {relative}: {error}")
            continue
        if data and not text.endswith("\n"):
            failures.append(f"missing final newline: {relative}")
        for line_number, line in enumerate(text.splitlines(), 1):
            if line.endswith((" ", "\t")):
                failures.append(f"trailing whitespace: {relative}:{line_number}")
    details = {"checkedFiles": checked}
    if failures:
        return GateResult.failed(gate_id, failures, details=details)
    return GateResult.passed(gate_id, details=details)


def gate_python_lint(root: Path) -> GateResult:
    gate_id = "python-lint"
    failures: list[str] = []
    python_paths = [path for path in repository_files(root) if path.suffix == ".py"]
    for path in python_paths:
        relative = path.relative_to(root).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative)
            compile(tree, relative, "exec")
        except (OSError, UnicodeDecodeError, SyntaxError) as error:
            failures.append(f"Python parse failed {relative}: {error}")
            continue
        try:
            with tokenize.open(path) as handle:
                for token in tokenize.generate_tokens(handle.readline):
                    if token.type == tokenize.INDENT and "\t" in token.string:
                        failures.append(f"tab indentation: {relative}:{token.start[0]}")
        except (OSError, SyntaxError, tokenize.TokenError) as error:
            failures.append(f"Python tokenization failed {relative}: {error}")
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                failures.append(f"bare except: {relative}:{node.lineno}")
            if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
                failures.append(f"wildcard import: {relative}:{node.lineno}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"eval", "exec"}
            ):
                failures.append(f"dynamic {node.func.id} call: {relative}:{node.lineno}")
    details = {"checkedFiles": len(python_paths)}
    if failures:
        return GateResult.failed(gate_id, failures, details=details)
    return GateResult.passed(gate_id, details=details)


def strip_fenced_code(text: str) -> str:
    lines: list[str] = []
    in_fence = False
    marker = ""
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            current = stripped[:3]
            if not in_fence:
                in_fence = True
                marker = current
            elif current == marker:
                in_fence = False
                marker = ""
            lines.append("")
            continue
        lines.append("" if in_fence else line)
    return "\n".join(lines)


def github_slug(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("`", "").strip().lower()
    text = re.sub(r"[^\w\- ]", "", text, flags=re.UNICODE)
    return re.sub(r"[ ]+", "-", text)


def markdown_anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for line in strip_fenced_code(path.read_text(encoding="utf-8")).splitlines():
        match = re.match(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if not match:
            continue
        base = github_slug(match.group(1))
        if not base:
            continue
        count = counts.get(base, 0)
        counts[base] = count + 1
        anchors.add(base if count == 0 else f"{base}-{count}")
    return anchors


def gate_docs_links(root: Path) -> GateResult:
    gate_id = "documentation-links"
    failures: list[str] = []
    markdown_paths = [path for path in repository_files(root) if path.suffix.lower() == ".md"]
    anchor_cache: dict[Path, set[str]] = {}
    link_pattern = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
    checked_links = 0
    for path in markdown_paths:
        relative = path.relative_to(root).as_posix()
        text = strip_fenced_code(path.read_text(encoding="utf-8"))
        for match in link_pattern.finditer(text):
            raw_target = match.group(1).strip()
            if not raw_target:
                failures.append(f"empty Markdown link target: {relative}")
                continue
            if raw_target.startswith("<") and raw_target.endswith(">"):
                raw_target = raw_target[1:-1]
            target_value = raw_target.split(maxsplit=1)[0]
            parsed = urllib.parse.urlsplit(target_value)
            if parsed.scheme in {"http", "https", "mailto", "tel"} or target_value.startswith("//"):
                continue
            checked_links += 1
            decoded_path = urllib.parse.unquote(parsed.path)
            target_path = path if not decoded_path else (path.parent / decoded_path).resolve()
            try:
                target_path.relative_to(root)
            except ValueError:
                failures.append(f"documentation link escapes repository: {relative} -> {target_value}")
                continue
            if not target_path.exists():
                failures.append(f"broken documentation link: {relative} -> {target_value}")
                continue
            if parsed.fragment and target_path.is_file() and target_path.suffix.lower() == ".md":
                anchors = anchor_cache.setdefault(target_path, markdown_anchors(target_path))
                fragment = urllib.parse.unquote(parsed.fragment).lower()
                if fragment not in anchors:
                    failures.append(
                        f"broken documentation anchor: {relative} -> {target_value}"
                    )
    details = {"checkedDocuments": len(markdown_paths), "checkedLocalLinks": checked_links}
    if failures:
        return GateResult.failed(gate_id, failures, details=details)
    return GateResult.passed(gate_id, details=details)


def gate_generated_contracts(root: Path) -> GateResult:
    return command_gate(
        "generated-contract-parity",
        root,
        [sys.executable, "tools/generate_contracts.py", "--check", "."],
    )


def gate_generated_traceability(root: Path) -> GateResult:
    return command_gate(
        "generated-traceability-parity",
        root,
        [sys.executable, "tools/generate_traceability.py", "--check", "."],
    )


def gate_baseline(root: Path) -> GateResult:
    return command_gate(
        "normative-baseline",
        root,
        [sys.executable, "tools/validate_baseline.py", "."],
    )


def gate_release_traceability(root: Path) -> GateResult:
    return command_gate(
        "release-traceability",
        root,
        [
            sys.executable,
            "tools/validate_release_traceability.py",
            "--check",
            "--self-test",
            "--json",
            ".",
        ],
    )


def gate_fixture_registry(root: Path) -> GateResult:
    gate_id = "positive-negative-fixtures"
    failures: list[str] = []
    try:
        validator = import_tool(root / "tools/validate_baseline.py", "fdir_quality_baseline")
        manifest_path = root / "fixtures/negative/manifest.json"
        manifest = load_json(manifest_path)
    except (OSError, json.JSONDecodeError, RuntimeError) as error:
        return GateResult.failed(gate_id, [f"fixture registry could not be loaded: {error}"])

    entries = manifest.get("fixtures")
    if not isinstance(entries, list) or not entries:
        return GateResult.failed(gate_id, ["negative fixture manifest is empty or invalid"])

    actual_negative = {
        path.relative_to(root).as_posix()
        for path in (root / "fixtures/negative").glob("*.json")
        if path.name != "manifest.json"
    }
    registered: list[str] = []
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            failures.append(f"negative manifest entry {index} is not an object")
            continue
        relative = item.get("path")
        expected = item.get("expectedCode")
        if not isinstance(relative, str) or not relative:
            failures.append(f"negative manifest entry {index} has no path")
            continue
        registered.append(relative)
        if not isinstance(expected, str) or not expected:
            failures.append(f"negative fixture has no expectedCode: {relative}")
            continue
        path = root / relative
        try:
            path.resolve().relative_to((root / "fixtures/negative").resolve())
        except ValueError:
            failures.append(f"negative fixture escapes registry directory: {relative}")
            continue
        if not path.is_file():
            failures.append(f"registered negative fixture is missing: {relative}")
            continue
        try:
            document = load_json(path)
        except (OSError, json.JSONDecodeError) as error:
            failures.append(f"negative fixture is invalid JSON {relative}: {error}")
            continue
        errors = validator.validate_document(document)
        if not errors:
            failures.append(f"negative fixture unexpectedly validates: {relative}")
        elif expected not in errors:
            failures.append(
                f"negative fixture did not produce {expected}: {relative} ({', '.join(errors)})"
            )

    duplicates = sorted({item for item in registered if registered.count(item) > 1})
    for relative in duplicates:
        failures.append(f"negative fixture registered more than once: {relative}")
    registered_set = set(registered)
    for relative in sorted(actual_negative - registered_set):
        failures.append(f"unregistered negative fixture: {relative}")
    for relative in sorted(registered_set - actual_negative):
        failures.append(f"manifest references non-fixture path: {relative}")

    positive_paths = sorted((root / "examples").glob("*.json")) + sorted(
        (root / "fixtures/positive").glob("*.json")
    )
    if not positive_paths:
        failures.append("positive example and fixture suite is empty")
    for path in positive_paths:
        relative = path.relative_to(root).as_posix()
        try:
            errors = validator.validate_document(load_json(path))
        except (OSError, json.JSONDecodeError) as error:
            failures.append(f"positive document is invalid JSON {relative}: {error}")
            continue
        if errors:
            failures.append(f"positive document failed {relative}: {', '.join(errors)}")

    details = {
        "negativeFixtures": len(actual_negative),
        "positiveDocuments": len(positive_paths),
    }
    if failures:
        return GateResult.failed(gate_id, failures, details=details)
    return GateResult.passed(gate_id, details=details)


def gate_requirement_traceability(root: Path) -> GateResult:
    gate_id = "requirement-test-traceability"
    failures: list[str] = []
    try:
        requirements = load_json(root / "machine/requirements.yaml").get("requirements")
        tests = load_json(root / "machine/acceptance-tests.yaml").get("tests")
    except (OSError, json.JSONDecodeError) as error:
        return GateResult.failed(gate_id, [f"traceability source could not be loaded: {error}"])
    if not isinstance(requirements, list) or not requirements:
        failures.append("requirement registry is empty or invalid")
        requirements = []
    if not isinstance(tests, list) or not tests:
        failures.append("acceptance-test registry is empty or invalid")
        tests = []

    requirement_ids = [item.get("id") for item in requirements if isinstance(item, dict)]
    test_ids = [item.get("id") for item in tests if isinstance(item, dict)]
    if len(requirement_ids) != len(set(requirement_ids)):
        failures.append("duplicate requirement identifier")
    if len(test_ids) != len(set(test_ids)):
        failures.append("duplicate acceptance-test identifier")
    known_requirements = {item for item in requirement_ids if isinstance(item, str)}
    coverage = {item: 0 for item in known_requirements}
    for item in tests:
        if not isinstance(item, dict):
            failures.append("acceptance-test registry contains a non-object entry")
            continue
        test_id = item.get("id", "<unknown>")
        mapped = item.get("requirements")
        if not isinstance(mapped, list) or not mapped:
            failures.append(f"acceptance test has no requirement mapping: {test_id}")
            continue
        for requirement_id in mapped:
            if requirement_id not in known_requirements:
                failures.append(f"unknown requirement in {test_id}: {requirement_id}")
            else:
                coverage[requirement_id] += 1
        command = item.get("command")
        fixture = item.get("fixture")
        fixture_kinds = {"positive-fixture", "negative-fixture", "canonical-vector"}
        executable_fixture = (
            item.get("kind") in fixture_kinds
            and isinstance(fixture, str)
            and (root / fixture).is_file()
        )
        if not isinstance(command, str) or not command.strip():
            if not executable_fixture:
                failures.append(f"acceptance test has no executable command or fixture: {test_id}")
        else:
            try:
                tokens = shlex.split(command)
            except ValueError as error:
                failures.append(f"invalid command in {test_id}: {error}")
                tokens = []
            for token in tokens:
                if token.endswith(".py") and not (root / token).is_file():
                    failures.append(f"acceptance test command references missing file {test_id}: {token}")
        if fixture is not None and (not isinstance(fixture, str) or not (root / fixture).is_file()):
            failures.append(f"acceptance test references missing fixture {test_id}: {fixture}")
    for requirement_id, count in sorted(coverage.items()):
        if count == 0:
            failures.append(f"orphan normative requirement: {requirement_id}")

    details = {"requirements": len(requirements), "acceptanceTests": len(tests)}
    if failures:
        return GateResult.failed(gate_id, failures, details=details)
    return GateResult.passed(gate_id, details=details)


def gate_claim_dis/ipline(root: Path) -> GateResult:
    gate_id = "claim-dis/ipline"
    failures: list[str] = []
    try:
        baseline = load_json(root / "baseline.yaml")
        manifest = load_json(root / "release/claim-manifest.yaml")
    except (OSError, json.JSONDecodeError) as error:
        return GateResult.failed(gate_id, [f"claim authority could not be loaded: {error}"])
    if baseline.get("productImplementationClaim") is not False:
        failures.append("baseline makes an unsupported product implementation claim")
    if manifest.get("productionReady") is not False:
        failures.append("release manifest makes an unsupported production-ready claim")
    if manifest.get("developmentStatus") not in {
        "development-unqualified",
        "implemented-unqualified",
    }:
        failures.append("release manifest development status is not explicitly unqualified")
    for item in manifest.get("formatTuples", []):
        tuple_id = item.get("id", "<unknown>")
        if item.get("productionReady") is not False:
            failures.append(f"format tuple makes an unsupported production claim: {tuple_id}")
        if item.get("state") == "qualified":
            failures.append(f"format tuple is marked qualified without release evidence: {tuple_id}")
    details = {
        "developmentStatus": manifest.get("developmentStatus"),
        "formatTuples": len(manifest.get("formatTuples", [])),
        "productionReady": manifest.get("productionReady"),
    }
    if failures:
        return GateResult.failed(gate_id, failures, details=details)
    return GateResult.passed(gate_id, details=details)


def gate_release_qualification(root: Path) -> GateResult:
    gate_id = "release-qualification"
    failures: list[str] = []
    try:
        manifest = load_json(root / "release/claim-manifest.yaml")
    except (OSError, json.JSONDecodeError) as error:
        return GateResult.failed(gate_id, [f"release manifest could not be loaded: {error}"])
    if manifest.get("developmentStatus") != "qualified" or manifest.get("productionReady") is not True:
        failures.append("release is not qualified and production-ready")
    for item in manifest.get("formatTuples", []):
        if item.get("state") != "qualified" or item.get("productionReady") is not True:
            failures.append(f"unqualified release tuple: {item.get('id', '<unknown>')}")
    if failures:
        return GateResult.failed(
            gate_id,
            failures,
            details={"releaseCertification": False},
        )
    return GateResult.passed(gate_id, details={"releaseCertification": True})


def gate_unit_tests(root: Path) -> GateResult:
    script = textwrap.dedent(
        """
        import sys
        import unittest

        suite = unittest.defaultTestLoader.dis/over("tests", pattern="test_*.py")
        count = suite.countTestCases()
        print(f"dis/overed {count} unit tests")
        if count == 0:
            raise SystemExit(2)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        raise SystemExit(0 if result.wasSuccessful() else 1)
        """
    ).strip()
    result = command_gate("unit-tests", root, [sys.executable, "-c", script])
    if result.status == "failed" and "dis/overed 0 unit tests" in result.stdout:
        result.diagnostics.append("unit test dis/overy returned zero tests")
        result.diagnostics = sorted(set(result.diagnostics))
    return result


def gate_workflow_policy(root: Path) -> GateResult:
    gate_id = "ci-policy"
    path = root / ".github/workflows/baseline.yml"
    failures: list[str] = []
    if not path.is_file():
        return GateResult.failed(gate_id, ["missing .github/workflows/baseline.yml"])
    text = path.read_text(encoding="utf-8")
    required_tokens = {
        "workflow name": "name: FDIR quality",
        "required job name": "name: quality / full",
        "pinned checkout action": "actions/checkout@v6.0.2",
        "pinned setup-python action": "actions/setup-python@v6.0.0",
        "pinned upload-artifact action": "actions/upload-artifact@v7.0.1",
        "Python series": 'python-version: "3.12"',
        "credential isolation": "persist-credentials: false",
        "full quality command": "python3 tools/quality.py --mode full --cache-policy off",
        "failure demonstration": "python3 tools/quality.py --self-test-gates",
        "read-write cache validation": "--cache-policy read-write",
        "read-only cache validation": "--cache-policy read-only",
        "durable evidence retention": "retention-days: 90",
        "failure evidence upload": "if: always()",
    }
    for description, token in required_tokens.items():
        if token not in text:
            failures.append(f"CI policy is missing {description}: {token}")
    if re.search(r"uses:\s+[^\s]+@(main|master|v\d+)\s*$", text, re.MULTILINE):
        failures.append("CI action references must use exact semantic versions")
    details = {"workflowSha256": sha256_bytes(path.read_bytes())}
    if failures:
        return GateResult.failed(gate_id, failures, details=details)
    return GateResult.passed(gate_id, details=details)


def gate_plan(mode: str) -> list[str]:
    common = [
        "toolchain",
        "text-format",
        "python-lint",
        "documentation-links",
        "generated-contract-parity",
        "positive-negative-fixtures",
        "requirement-test-traceability",
        "normative-baseline",
        "claim-dis/ipline",
    ]
    if mode in {"full", "release"}:
        common.extend(
            [
                "generated-traceability-parity",
                "release-traceability",
                "unit-tests",
                "ci-policy",
            ]
        )
    if mode == "release":
        common.append("release-qualification")
    return common


def gate_plan_digest(mode: str) -> str:
    return sha256_bytes(stable_json_bytes(gate_plan(mode)))


def cache_path(root: Path) -> Path:
    return root / ".validation/quality-cache.json"


def cache_precheck(
    root: Path,
    mode: str,
    policy: str,
    digest: str,
) -> tuple[GateResult, dict[str, Any] | None]:
    gate_id = "cache-policy"
    details = {
        "authoritativeGatesSkipped": False,
        "cachePath": ".validation/quality-cache.json",
        "policy": policy,
    }
    if policy == "off":
        return GateResult.passed(gate_id, details=details), None
    path = cache_path(root)
    cached: dict[str, Any] | None = None
    if path.is_file():
        try:
            value = load_json(path)
            cached = value if isinstance(value, dict) else None
        except (OSError, json.JSONDecodeError):
            cached = None
    if policy == "read-write":
        details["existingCache"] = "valid" if cached else "missing-or-invalid"
        if cached:
            details["existingSourceMatch"] = cached.get("sourceDigest") == digest
        return GateResult.passed(gate_id, details=details), cached

    failures: list[str] = []
    if not cached:
        failures.append("read-only cache is missing or invalid")
    else:
        if cached.get("schema") != CACHE_SCHEMA:
            failures.append("read-only cache schema mismatch")
        if cached.get("qualityVersion") != QUALITY_VERSION:
            failures.append("read-only cache quality version mismatch")
        if cached.get("mode") != mode:
            failures.append("read-only cache mode mismatch")
        if cached.get("sourceDigest") != digest:
            failures.append("read-only cache source digest mismatch")
        if cached.get("gatePlanSha256") != gate_plan_digest(mode):
            failures.append("read-only cache gate plan mismatch")
    if failures:
        return GateResult.failed(gate_id, failures, details=details), cached
    return GateResult.passed(gate_id, details=details), cached


def authoritative_results_digest(results: Sequence[GateResult]) -> str:
    normalized = [
        result.as_dict()
        for result in results
        if result.gate_id not in {"cache-policy", "cache-equivalence"}
    ]
    return sha256_bytes(stable_json_bytes(normalized))


def cache_equivalence(
    policy: str,
    cached: dict[str, Any] | None,
    results_digest: str,
) -> GateResult:
    gate_id = "cache-equivalence"
    details = {
        "authoritativeGatesSkipped": False,
        "resultsSha256": results_digest,
    }
    if policy != "read-only":
        return GateResult.passed(gate_id, details=details)
    expected = cached.get("gateResultsSha256") if cached else None
    details["cachedResultsSha256"] = expected
    if expected != results_digest:
        return GateResult.failed(
            gate_id,
            ["read-only cache result digest differs from fresh execution"],
            details=details,
        )
    return GateResult.passed(gate_id, details=details)


def write_cache(
    root: Path,
    mode: str,
    digest: str,
    results_digest: str,
) -> None:
    write_json(
        cache_path(root),
        {
            "schema": CACHE_SCHEMA,
            "qualityVersion": QUALITY_VERSION,
            "mode": mode,
            "sourceDigest": digest,
            "gatePlanSha256": gate_plan_digest(mode),
            "gateResultsSha256": results_digest,
            "authoritativeGatesSkipped": False,
        },
    )


def run_gates(root: Path, mode: str) -> list[GateResult]:
    functions: dict[str, Callable[[Path], GateResult]] = {
        "toolchain": gate_toolchain,
        "text-format": gate_text_format,
        "python-lint": gate_python_lint,
        "documentation-links": gate_docs_links,
        "generated-contract-parity": gate_generated_contracts,
        "positive-negative-fixtures": gate_fixture_registry,
        "requirement-test-traceability": gate_requirement_traceability,
        "normative-baseline": gate_baseline,
        "claim-dis/ipline": gate_claim_dis/ipline,
        "generated-traceability-parity": gate_generated_traceability,
        "release-traceability": gate_release_traceability,
        "unit-tests": gate_unit_tests,
        "ci-policy": gate_workflow_policy,
        "release-qualification": gate_release_qualification,
    }
    return [functions[gate_id](root) for gate_id in gate_plan(mode)]


def git_metadata(root: Path) -> dict[str, Any]:
    def git(*arguments: str) -> str | None:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            check=False,
        )
        return completed.stdout.strip() if completed.returncode == 0 else None

    revision = git("rev-parse", "HEAD")
    status = git("status", "--porcelain", "--untracked-files=all")
    return {
        "revision": revision,
        "worktreeDirty": bool(status) if status is not None else None,
    }


def build_receipt(
    root: Path,
    mode: str,
    policy: str,
    digest: str,
    file_count: int,
    results: Sequence[GateResult],
) -> dict[str, Any]:
    status = "passed" if all(result.status == "passed" for result in results) else "failed"
    return {
        "schema": RECEIPT_SCHEMA,
        "qualityVersion": QUALITY_VERSION,
        "command": [
            "python3",
            "tools/quality.py",
            "--mode",
            mode,
            "--cache-policy",
            policy,
            ".",
        ],
        "mode": mode,
        "cachePolicy": policy,
        "status": status,
        "durableEvidence": mode in {"full", "release"},
        "releaseCertification": mode == "release" and status == "passed",
        "authoritativeGatesSkipped": False,
        "source": {
            "sha256": digest,
            "fileCount": file_count,
            **git_metadata(root),
        },
        "toolchain": {
            "implementation": platform.python_implementation(),
            "pythonVersion": platform.python_version(),
            "platform": platform.platform(),
        },
        "gatePlan": gate_plan(mode),
        "gatePlanSha256": gate_plan_digest(mode),
        "gates": [result.as_dict() for result in results],
    }


def execute_quality(
    root: Path,
    mode: str,
    policy: str,
    receipt_path: Path,
) -> tuple[int, dict[str, Any]]:
    root = root.resolve()
    digest, file_count = source_digest(root)
    cache_result, cached = cache_precheck(root, mode, policy, digest)
    authoritative = run_gates(root, mode)
    results_digest = authoritative_results_digest(authoritative)
    equivalence = cache_equivalence(policy, cached, results_digest)
    results = [cache_result, *authoritative, equivalence]
    receipt = build_receipt(root, mode, policy, digest, file_count, results)
    if policy == "read-write" and receipt["status"] == "passed":
        write_cache(root, mode, digest, results_digest)
    write_json(receipt_path, receipt)
    return (0 if receipt["status"] == "passed" else 1), receipt


def copy_repository(root: Path, destination: Path) -> None:
    shutil.copytree(
        root,
        destination,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            ".git",
            ".issue6-export",
            ".tmp",
            ".validation",
            "__pycache__",
            "reports",
        ),
    )


def combined_result_text(result: GateResult) -> str:
    return "\n".join([*result.diagnostics, result.stdout, result.stderr]).lower()


def run_failure_demonstrations(root: Path, receipt_path: Path) -> tuple[int, dict[str, Any]]:
    root = root.resolve()
    digest, _ = source_digest(root)
    cases: list[dict[str, Any]] = []

    def demonstrate(
        case_id: str,
        mutation: Callable[[Path], None],
        gate: Callable[[Path], GateResult],
        expected: str,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix=f"fdir-quality-{case_id}-") as directory:
            candidate = Path(directory) / "repo"
            copy_repository(root, candidate)
            mutation(candidate)
            result = gate(candidate)
            detected = result.status == "failed" and expected.lower() in combined_result_text(result)
            cases.append(
                {
                    "id": case_id,
                    "gate": result.gate_id,
                    "expectedFragment": expected,
                    "status": "detected" if detected else "missed",
                    "result": result.as_dict(),
                }
            )

    demonstrate(
        "generated-contract-drift",
        lambda candidate: (candidate / "schemas/fdir.cddl").write_text(
            (candidate / "schemas/fdir.cddl").read_text(encoding="utf-8") + "# drift\n",
            encoding="utf-8",
        ),
        gate_generated_contracts,
        "schemas/fdir.cddl",
    )

    def invalid_positive(candidate: Path) -> None:
        path = candidate / "examples/minimal.json"
        value = load_json(path)
        value["fdirVersion"] = "0.0.0"
        write_json(path, value)

    demonstrate(
        "invalid-positive-example",
        invalid_positive,
        gate_fixture_registry,
        "examples/minimal.json",
    )

    def missing_negative(candidate: Path) -> None:
        path = candidate / "fixtures/negative/manifest.json"
        value = load_json(path)
        value["fixtures"] = value["fixtures"][:-1]
        write_json(path, value)

    demonstrate(
        "missing-negative-expectation",
        missing_negative,
        gate_fixture_registry,
        "unregistered negative fixture",
    )

    def orphan_requirement(candidate: Path) -> None:
        path = candidate / "machine/requirements.yaml"
        value = load_json(path)
        value["requirements"].append(
            {
                "id": "FDIR-SELFTEST-999",
                "level": "must",
                "text": "Intentional orphan for quality-gate self-test.",
            }
        )
        write_json(path, value)

    demonstrate(
        "orphan-requirement",
        orphan_requirement,
        gate_requirement_traceability,
        "FDIR-SELFTEST-999",
    )

    demonstrate(
        "trailing-whitespace",
        lambda candidate: (candidate / "README.md").write_text(
            (candidate / "README.md").read_text(encoding="utf-8") + "bad whitespace  \n",
            encoding="utf-8",
        ),
        gate_text_format,
        "trailing whitespace",
    )

    def bad_lint(candidate: Path) -> None:
        path = candidate / "tests/injected_bad_lint.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("try:\n    pass\nexcept:\n    pass\n", encoding="utf-8")

    demonstrate("lint-failure", bad_lint, gate_python_lint, "bare except")

    demonstrate(
        "broken-documentation-link",
        lambda candidate: (candidate / "README.md").write_text(
            (candidate / "README.md").read_text(encoding="utf-8")
            + "\n[broken self-test link](missing-self-test.md)\n",
            encoding="utf-8",
        ),
        gate_docs_links,
        "missing-self-test.md",
    )

    def failing_test(candidate: Path) -> None:
        path = candidate / "tests/test_injected_failure.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "import unittest\n\n"
            "class InjectedFailure(unittest.TestCase):\n"
            "    def test_failure(self):\n"
            "        self.fail('intentional failure')\n",
            encoding="utf-8",
        )

    demonstrate("failing-unit-test", failing_test, gate_unit_tests, "FAILED")

    def empty_tests(candidate: Path) -> None:
        shutil.rmtree(candidate / "tests", ignore_errors=True)
        (candidate / "tests").mkdir()

    demonstrate("empty-unit-test-suite", empty_tests, gate_unit_tests, "dis/overed 0 unit tests")

    def false_claim(candidate: Path) -> None:
        path = candidate / "release/claim-manifest.yaml"
        value = load_json(path)
        value["productionReady"] = True
        write_json(path, value)

    demonstrate("false-production-claim", false_claim, gate_claim_dis/ipline, "unsupported production-ready")

    def stale_cache(candidate: Path) -> None:
        path = cache_path(candidate)
        write_json(
            path,
            {
                "schema": CACHE_SCHEMA,
                "qualityVersion": QUALITY_VERSION,
                "mode": "fast",
                "sourceDigest": "0" * 64,
                "gatePlanSha256": gate_plan_digest("fast"),
                "gateResultsSha256": "0" * 64,
                "authoritativeGatesSkipped": False,
            },
        )

    def stale_cache_gate(candidate: Path) -> GateResult:
        digest_value, _ = source_digest(candidate)
        result, _ = cache_precheck(candidate, "fast", "read-only", digest_value)
        return result

    demonstrate("stale-read-only-cache", stale_cache, stale_cache_gate, "source digest mismatch")

    def workflow_drift(candidate: Path) -> None:
        path = candidate / ".github/workflows/baseline.yml"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "actions/checkout@v6.0.2", "actions/checkout@main"
            ),
            encoding="utf-8",
        )

    demonstrate("unpinned-ci-action", workflow_drift, gate_workflow_policy, "pinned checkout action")
    demonstrate(
        "unqualified-release",
        lambda _candidate: None,
        gate_release_qualification,
        "release is not qualified",
    )

    status = "passed" if all(case["status"] == "detected" for case in cases) else "failed"
    receipt = {
        "schema": FAILURE_RECEIPT_SCHEMA,
        "qualityVersion": QUALITY_VERSION,
        "command": ["python3", "tools/quality.py", "--self-test-gates", "."],
        "status": status,
        "sourceSha256": digest,
        "caseCount": len(cases),
        "detectedCount": sum(case["status"] == "detected" for case in cases),
        "cases": cases,
    }
    write_json(receipt_path, receipt)
    return (0 if status == "passed" else 1), receipt


def print_summary(receipt_path: Path, receipt: dict[str, Any]) -> None:
    if receipt.get("schema") == RECEIPT_SCHEMA:
        failed = [gate["id"] for gate in receipt["gates"] if gate["status"] != "passed"]
        if failed:
            print(f"FDIR repository quality failed: {', '.join(failed)}")
        else:
            print(
                "FDIR repository quality passed: "
                f"mode={receipt['mode']}, gates={len(receipt['gates'])}, "
                f"source={receipt['source']['sha256']}"
            )
    else:
        print(
            "FDIR quality failure demonstrations "
            f"{receipt['status']}: {receipt['detectedCount']}/{receipt['caseCount']} detected"
        )
    print(f"receipt: {receipt_path}")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".", help="repository root")
    parser.add_argument("--mode", choices=("fast", "full", "release"), default="fast")
    parser.add_argument(
        "--cache-policy",
        choices=("off", "read-only", "read-write"),
        default="off",
    )
    parser.add_argument("--receipt", help="machine-readable JSON receipt path")
    parser.add_argument(
        "--self-test-gates",
        action="store_true",
        help="demonstrate that every major gate detects an intentional failure",
    )
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"repository root does not exist: {root}", file=sys.stderr)
        return 2
    if args.receipt:
        receipt_path = Path(args.receipt)
        if not receipt_path.is_absolute():
            receipt_path = root / receipt_path
    elif args.self_test_gates:
        receipt_path = root / "reports/quality/failure-demonstration.json"
    else:
        receipt_path = root / f"reports/quality/{args.mode}.json"

    if args.self_test_gates:
        status, receipt = run_failure_demonstrations(root, receipt_path)
    else:
        status, receipt = execute_quality(root, args.mode, args.cache_policy, receipt_path)
    print_summary(receipt_path.relative_to(root), receipt)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
