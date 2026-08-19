from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RECEIPT_PATH = REPOSITORY_ROOT / "reports/quality/rust-workspace.json"
RECEIPT_SCHEMA = "fdir/rust-workspace-quality-receipt/1"


@dataclass
class CommandReceipt:
    command: list[str]
    status: str
    exit_code: int | None
    stdout: str
    stderr: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "status": self.status,
            "exitCode": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


def stable_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def normalized_output(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace(str(REPOSITORY_ROOT), ".")
    normalized = re.sub(
        r"Finished `[^`]+` profile .* in [0-9.]+s",
        "Finished <profile>",
        normalized,
    )
    normalized = re.sub(
        r"finished in [0-9.]+s", "finished in <duration>s", normalized
    )
    return normalized.rstrip()


def deterministic_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "CARGO_NET_OFFLINE": "true",
            "CARGO_TARGET_DIR": str(REPOSITORY_ROOT / ".validation/rust-target"),
            "CARGO_TERM_COLOR": "never",
            "FDIR_SOURCE_REVISION": environment.get(
                "FDIR_SOURCE_REVISION", "quality-test"
            ),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "RUST_BACKTRACE": "0",
            "TZ": "UTC",
        }
    )
    return environment


def run_command(command: Sequence[str]) -> CommandReceipt:
    rendered = [str(item) for item in command]
    try:
        completed = subprocess.run(
            rendered,
            cwd=REPOSITORY_ROOT,
            env=deterministic_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
        )
    except OSError as error:
        return CommandReceipt(
            rendered,
            "failed",
            None,
            "",
            f"command could not execute: {error}",
        )
    except subprocess.TimeoutExpired as error:
        stdout = normalized_output(error.stdout or "")
        stderr = normalized_output(error.stderr or "")
        return CommandReceipt(
            rendered,
            "failed",
            None,
            stdout,
            f"{stderr}\ncommand timed out",
        )
    return CommandReceipt(
        command=rendered,
        status="passed" if completed.returncode == 0 else "failed",
        exit_code=completed.returncode,
        stdout=normalized_output(completed.stdout),
        stderr=normalized_output(completed.stderr),
    )


def dependency_names(manifest: dict[str, Any]) -> list[str]:
    names: set[str] = set()
    for table_name in ("dependencies", "dev-dependencies", "build-dependencies"):
        table = manifest.get(table_name, {})
        if isinstance(table, dict):
            names.update(table)
    return sorted(names)


def admitted_rust_dependencies() -> dict[str, dict[str, Any]]:
    catalog = json.loads(
        (REPOSITORY_ROOT / "machine/dependency-catalog.yaml").read_text(
            encoding="utf-8"
        )
    )
    dependencies = catalog.get("dependencies", [])
    if not isinstance(dependencies, list):
        return {}
    return {
        dependency["name"]: dependency
        for dependency in dependencies
        if isinstance(dependency, dict)
        and dependency.get("kind") == "rust-crate"
        and dependency.get("qualificationState")
        in {"admitted-unqualified", "adapter-qualified", "production-qualified"}
        and isinstance(dependency.get("name"), str)
    }


def validate_external_specification(
    crate_name: str,
    dependency_name: str,
    specification: Any,
    admitted: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    if not isinstance(specification, dict):
        return [
            f"external dependency must use an exact table specification: "
            f"{crate_name} -> {dependency_name}"
        ]
    expected_version = f"={admitted.get('version')}"
    if specification.get("version") != expected_version:
        failures.append(
            f"external dependency version differs from admitted manifest: "
            f"{crate_name} -> {dependency_name}"
        )
    expected_features = sorted(str(value) for value in admitted.get("features", []))
    actual_features = sorted(str(value) for value in specification.get("features", []))
    if actual_features != expected_features:
        failures.append(
            f"external dependency features differ from admitted manifest: "
            f"{crate_name} -> {dependency_name}"
        )
    if "path" in specification or "git" in specification or "branch" in specification:
        failures.append(
            f"admitted registry dependency cannot override its source: "
            f"{crate_name} -> {dependency_name}"
        )
    return failures


def policy_failures() -> list[str]:
    failures: list[str] = []
    toolchain = tomllib.loads(
        (REPOSITORY_ROOT / "rust-toolchain.toml").read_text(encoding="utf-8")
    )
    quality_toolchain = json.loads(
        (REPOSITORY_ROOT / "quality/toolchain.json").read_text(encoding="utf-8")
    )
    graph = json.loads(
        (REPOSITORY_ROOT / "quality/rust-workspace.json").read_text(
            encoding="utf-8"
        )
    )
    workspace = tomllib.loads(
        (REPOSITORY_ROOT / "Cargo.toml").read_text(encoding="utf-8")
    )
    admitted = admitted_rust_dependencies()

    rust_policy = quality_toolchain.get("rust")
    pinned = toolchain.get("toolchain", {})
    if not isinstance(rust_policy, dict):
        failures.append("quality/toolchain.json has no Rust policy")
        rust_policy = {}
    if pinned.get("channel") != rust_policy.get("channel"):
        failures.append("rust-toolchain.toml channel differs from quality/toolchain.json")
    if pinned.get("profile") != rust_policy.get("profile"):
        failures.append("Rust profile pin differs from quality/toolchain.json")
    if sorted(pinned.get("components", [])) != sorted(
        rust_policy.get("components", [])
    ):
        failures.append("Rust component pins differ from quality/toolchain.json")
    if sorted(pinned.get("targets", [])) != sorted(rust_policy.get("targets", [])):
        failures.append("Rust target pins differ from quality/toolchain.json")
    workspace_package = workspace.get("workspace", {}).get("package", {})
    if workspace_package.get("rust-version") != rust_policy.get(
        "minimumSupportedVersion"
    ):
        failures.append("workspace MSRV differs from the Rust toolchain policy")
    if workspace_package.get("edition") != rust_policy.get("edition"):
        failures.append("workspace edition differs from the Rust toolchain policy")

    graph_entries = graph.get("crateGraph")
    if graph.get("schema") != "fdir/rust-workspace/1" or not isinstance(
        graph_entries, list
    ):
        return [*failures, "invalid quality/rust-workspace.json"]
    expected_members = {
        f"crates/{entry.get('name')}"
        for entry in graph_entries
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    }
    actual_members = set(workspace.get("workspace", {}).get("members", []))
    if expected_members != actual_members:
        failures.append(
            "workspace members differ from the machine-readable crate graph"
        )

    declared_graph: dict[str, list[str]] = {}
    known_names = {path.removeprefix("crates/") for path in expected_members}
    used_external: set[str] = set()
    for entry in graph_entries:
        if not isinstance(entry, dict):
            failures.append("crate graph contains a non-object entry")
            continue
        name = entry.get("name")
        declared_dependencies = entry.get("dependencies")
        if not isinstance(name, str) or not isinstance(declared_dependencies, list):
            failures.append("crate graph entry is incomplete")
            continue
        manifest_path = REPOSITORY_ROOT / "crates" / name / "Cargo.toml"
        if not manifest_path.is_file():
            failures.append(
                f"missing crate manifest: {manifest_path.relative_to(REPOSITORY_ROOT)}"
            )
            continue
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        actual_dependencies = dependency_names(manifest)
        actual_internal = sorted(
            dependency
            for dependency in actual_dependencies
            if dependency in known_names
        )
        expected_dependencies = sorted(str(value) for value in declared_dependencies)
        if actual_internal != expected_dependencies:
            failures.append(
                f"dependency graph mismatch for {name}: "
                f"{actual_internal} != {expected_dependencies}"
            )
        for table_name in (
            "dependencies",
            "dev-dependencies",
            "build-dependencies",
        ):
            table = manifest.get(table_name, {})
            if not isinstance(table, dict):
                continue
            for dependency, specification in table.items():
                if dependency in known_names:
                    if not isinstance(specification, dict) or "path" not in specification:
                        failures.append(
                            f"workspace dependency must be an exact path: "
                            f"{name} -> {dependency}"
                        )
                    continue
                admitted_manifest = admitted.get(dependency)
                if admitted_manifest is None:
                    failures.append(
                        f"external Rust dependency is not admitted: {dependency}"
                    )
                    continue
                used_external.add(dependency)
                failures.extend(
                    validate_external_specification(
                        name,
                        dependency,
                        specification,
                        admitted_manifest,
                    )
                )
        features = manifest.get("features", {})
        if isinstance(features, dict):
            forbidden_features = {"production", "qualified", "release"} & set(
                features
            )
            if forbidden_features:
                failures.append(
                    f"forbidden production feature in {name}: "
                    f"{sorted(forbidden_features)}"
                )
        declared_graph[name] = expected_dependencies

    unused_admissions = sorted(set(admitted) - used_external)
    if unused_admissions:
        failures.append(
            f"admitted Rust dependencies are not declared by the workspace: "
            f"{unused_admissions}"
        )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            failures.append(f"crate dependency cycle includes {name}")
            return
        if name in visited:
            return
        visiting.add(name)
        for dependency in declared_graph.get(name, []):
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for crate_name in sorted(declared_graph):
        visit(crate_name)

    core_text = (REPOSITORY_ROOT / "crates/fdir-core/src/lib.rs").read_text(
        encoding="utf-8"
    ).lower()
    for vocabulary in graph.get("forbiddenCoreVocabulary", []):
        if isinstance(vocabulary, str) and vocabulary.lower() in core_text:
            failures.append(f"adapter vocabulary leaked into fdir-core: {vocabulary}")

    unsafe_pattern = re.compile(
        r"\bunsafe\s*(?:\{|fn\b|impl\b|trait\b)|extern\s+\"C\""
    )
    for path in sorted((REPOSITORY_ROOT / "crates").rglob("*.rs")):
        text = path.read_text(encoding="utf-8")
        if unsafe_pattern.search(text):
            failures.append(
                f"unsafe or FFI implementation is forbidden: "
                f"{path.relative_to(REPOSITORY_ROOT)}"
            )
        if "#![forbid(unsafe_code)]" not in text and path.name != "generated.rs":
            failures.append(
                f"Rust source does not forbid unsafe code: "
                f"{path.relative_to(REPOSITORY_ROOT)}"
            )

    if graph.get("productionFeatures") != []:
        failures.append("foundation must declare no production features")
    if not (REPOSITORY_ROOT / "Cargo.lock").is_file():
        failures.append("Cargo.lock is missing")
    cargo_config = (REPOSITORY_ROOT / ".cargo/config.toml").read_text(
        encoding="utf-8"
    )
    if 'target-dir = ".validation/rust-target"' not in cargo_config:
        failures.append("Cargo target state is not isolated under .validation")
    if "offline = true" not in cargo_config:
        failures.append("authoritative quality checks must run Cargo offline")

    workflow = (REPOSITORY_ROOT / ".github/workflows/baseline.yml").read_text(
        encoding="utf-8"
    )
    channel = rust_policy.get("channel", "<missing>")
    for token in (
        f"rustup toolchain install {channel}",
        f"rustup override set {channel}",
        "--component clippy",
        "--component rustfmt",
        "rustc --version --verbose",
        "cargo clippy --version",
        "cargo fetch --locked",
        'CARGO_NET_OFFLINE: "false"',
    ):
        if token not in workflow:
            failures.append(f"CI workflow is missing pinned Rust token: {token}")
    return failures


class RustWorkspaceQualityTests(unittest.TestCase):
    def test_pinned_workspace_quality_and_receipt(self) -> None:
        failures = policy_failures()
        commands: list[CommandReceipt] = []
        test_count = 0
        tool_versions: dict[str, str] = {}

        command_plan = [
            ["rustc", "--version", "--verbose"],
            ["cargo", "--version"],
            ["rustfmt", "--version"],
            ["cargo", "clippy", "--version"],
            [sys.executable, "tools/generate_rust_contract.py", "--check", "."],
            ["cargo", "metadata", "--no-deps", "--format-version", "1", "--locked"],
            ["cargo", "fmt", "--all", "--", "--check"],
            [
                "cargo",
                "build",
                "--workspace",
                "--all-targets",
                "--all-features",
                "--locked",
            ],
            [
                "cargo",
                "clippy",
                "--workspace",
                "--all-targets",
                "--all-features",
                "--locked",
                "--",
                "-D",
                "warnings",
            ],
            [
                "cargo",
                "test",
                "-p",
                "fdir-contract",
                "--test",
                "generated_contract_parity",
                "--locked",
            ],
            [
                "cargo",
                "test",
                "--workspace",
                "--all-targets",
                "--all-features",
                "--locked",
                "--",
                "--list",
                "--format",
                "terse",
            ],
            [
                "cargo",
                "test",
                "--workspace",
                "--all-targets",
                "--all-features",
                "--locked",
            ],
        ]

        for index, command in enumerate(command_plan):
            receipt = run_command(command)
            commands.append(receipt)
            if index < 4 and receipt.status == "passed":
                tool_versions[" ".join(command)] = (
                    receipt.stdout.splitlines()[0] if receipt.stdout else ""
                )
            if command[:2] == ["rustc", "--version"] and "rustc 1.97.1" not in receipt.stdout:
                failures.append("rustc does not match pinned version 1.97.1")
            if "--list" in command:
                combined = "\n".join((receipt.stdout, receipt.stderr))
                test_count = sum(
                    1
                    for line in combined.splitlines()
                    if line.rstrip().endswith(": test")
                )
                if test_count == 0:
                    failures.append("Rust test discovery returned zero tests")
            if receipt.status != "passed":
                failures.append(f"command failed: {' '.join(command)}")
                break

        receipt_value = {
            "schema": RECEIPT_SCHEMA,
            "status": "passed" if not failures else "failed",
            "sourceRevision": deterministic_environment()["FDIR_SOURCE_REVISION"],
            "toolchain": tool_versions,
            "testCount": test_count,
            "policyFailures": sorted(set(failures)),
            "commands": [command.as_dict() for command in commands],
            "productionReady": False,
            "authoritativeRustChecksSkipped": False,
        }
        stable_write_json(RECEIPT_PATH, receipt_value)
        self.assertFalse(failures, "\n".join(sorted(set(failures))))


if __name__ == "__main__":
    unittest.main()
