"""Run the executable qualification matrix for GitHub issue #90.

This runner keeps two authorities separate:

* ``jsonschema.Draft202012Validator`` is the external Draft 2020-12
  authority.  ``tools.ir_validation.validate_normative_schema`` is the FDIR
  schema subset implementation.  They must agree for every schema case.
* The graph and status cases are single-defect mutations of checked-in valid
  examples.  Their expected diagnostic codes are data in the corpus, not
  inferred from the runtime validator.  A missing or different diagnostic is
  a qualification failure.

The runner intentionally fails when the corpus does not cover every required
category.  A small passing sample must not be mistaken for issue completion.
It writes all three issue-specific reports even when a fatal setup error makes
the qualification impossible.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA_PATH = ROOT / "schemas" / "document-form-ir.schema.json"
DEFAULT_CORPUS_PATH = ROOT / "machine" / "qualification-issue-90-corpus.json"
DEFAULT_OUT_DIR = ROOT / "e2e" / ".run" / "qualification-issue-90"
REPORT_NAMES = {
    "schema": "schema-differential.json",
    "graph": "graph-invariants.json",
    "status": "status-contract.json",
}
RUNTIME_PATH = ROOT / "tools" / "ir_validation.py"
REGISTRY_PATH = ROOT / "machine" / "reference-registry.json"
MODEL_CONTRACT_PATH = ROOT / "machine" / "model-contract.json"


class QualificationError(RuntimeError):
    """Raised when issue #90 qualification cannot be executed safely."""


class ExternalValidatorUnavailable(QualificationError):
    """Raised when the independent Draft 2020-12 validator is unavailable."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QualificationError(f"cannot read JSON input {path}: {exc}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise QualificationError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _repository_path(relative: str) -> Path:
    """Resolve a corpus path without allowing it to escape the checkout."""

    candidate = Path(*relative.split("/"))
    if candidate.is_absolute():
        raise QualificationError(f"issue #90 corpus path must be relative: {relative}")
    resolved = (ROOT / candidate).resolve()
    try:
        resolved.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise QualificationError(f"issue #90 corpus path escapes repository: {relative}") from exc
    return resolved


def _source_sha() -> str:
    try:
        completed = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise QualificationError(f"cannot execute git: {exc}") from exc
    value = completed.stdout.strip()
    if completed.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise QualificationError(
            f"cannot obtain a 40-character lowercase source SHA: {value!r}"
        )
    return value


def _load_external_validator(schema: dict[str, Any]) -> tuple[Any, dict[str, str]]:
    """Load and pin the independent Draft 2020-12 validator authority."""

    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise ExternalValidatorUnavailable(
            "jsonschema is not installed; an independent Draft 2020-12 "
            "validator is required and qualification is fail-closed"
        ) from exc
    try:
        package_version = importlib.metadata.version("jsonschema")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ExternalValidatorUnavailable(
            "jsonschema import succeeded but its installed distribution "
            "metadata is unavailable"
        ) from exc
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise QualificationError("normative schema is not pinned to Draft 2020-12")
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:  # jsonschema.SchemaError is intentionally not hidden.
        raise QualificationError(
            f"external Draft 2020-12 validator rejected the normative schema: {exc}"
        ) from exc
    return Draft202012Validator(schema), {
        "library": "jsonschema",
        "class": "Draft202012Validator",
        "version": package_version,
    }


def _json_pointer(path: Iterable[Any]) -> str:
    parts = [str(item).replace("~", "~0").replace("/", "~1") for item in path]
    return "/" + "/".join(parts) if parts else "$"


def _external_result(validator: Any, document: Any) -> dict[str, Any]:
    try:
        errors = sorted(
            validator.iter_errors(document),
            key=lambda item: (list(item.absolute_path), item.validator or ""),
        )
    except Exception as exc:
        return {
            "accepted": False,
            "error": {
                "code": "EXTERNAL-VALIDATOR-ERROR",
                "message": f"external validator raised {type(exc).__name__}: {exc}",
            },
        }
    if not errors:
        return {"accepted": True, "errors": []}
    return {
        "accepted": False,
        "errors": [
            {
                "validator": str(error.validator),
                "path": _json_pointer(error.absolute_path),
                "schemaPath": _json_pointer(error.absolute_schema_path),
                "message": error.message,
            }
            for error in errors[:10]
        ],
    }


def _custom_schema_result(document: Any) -> dict[str, Any]:
    try:
        try:
            from ir_validation import IRValidationError, validate_normative_schema
        except ImportError:
            from tools.ir_validation import IRValidationError, validate_normative_schema

        validate_normative_schema(document)
    except Exception as exc:
        result: dict[str, Any] = {
            "accepted": False,
            "error": {
                "code": getattr(exc, "code", "CUSTOM-SCHEMA-ERROR"),
                "message": str(exc),
            },
        }
        return result
    return {"accepted": True, "errors": []}


def _runtime_result(document: dict[str, Any]) -> dict[str, Any]:
    try:
        try:
            from ir_validation import IRValidationError, validate_document
        except ImportError:
            from tools.ir_validation import IRValidationError, validate_document

        warnings = validate_document(document)
    except Exception as exc:
        return {
            "accepted": False,
            "diagnostic": getattr(exc, "code", "RUNTIME-VALIDATOR-ERROR"),
            "message": str(exc),
        }
    return {"accepted": True, "warnings": warnings}


_SEGMENT = re.compile(r"^(?P<name>[^\[]+?)(?:\[(?P<bracket>[^]]+)\])?$")


def _path_tokens(path: str) -> list[tuple[str | None, str | int | None]]:
    if path == "$":
        return []
    if not path.startswith("$"):
        path = "$." + path
    remainder = path[1:]
    if remainder.startswith("."):
        remainder = remainder[1:]
    if not remainder:
        return []
    tokens: list[tuple[str | None, str | int | None]] = []
    for raw in remainder.split("."):
        match = _SEGMENT.fullmatch(raw)
        if match is None:
            raise QualificationError(f"invalid mutation path segment: {raw}")
        name = match.group("name")
        bracket = match.group("bracket")
        selector: str | int | None = None
        if bracket is not None:
            if bracket.isdecimal():
                selector = int(bracket)
            elif "=" in bracket:
                selector = bracket
            else:
                raise QualificationError(f"invalid mutation selector: {bracket}")
        tokens.append((name, selector))
    return tokens


def _step(value: Any, token: tuple[str | None, str | int | None], path: str) -> Any:
    name, selector = token
    if name is not None:
        if not isinstance(value, dict) or name not in value:
            raise QualificationError(f"mutation path does not exist: {path}")
        value = value[name]
    if selector is None:
        return value
    if isinstance(selector, int):
        if not isinstance(value, list) or selector >= len(value):
            raise QualificationError(f"mutation list index does not exist: {path}")
        return value[selector]
    if not isinstance(value, list) or "=" not in selector:
        raise QualificationError(f"mutation selector is not a list selector: {path}")
    field, expected = selector.split("=", 1)
    for item in value:
        if isinstance(item, dict) and str(item.get(field)) == expected:
            return item
    raise QualificationError(f"mutation selector found no item: {path}")


def _resolve(document: Any, path: str, *, allow_missing_final: bool = False) -> Any:
    current = document
    tokens = _path_tokens(path)
    for index, token in enumerate(tokens):
        name, selector = token
        if index == len(tokens) - 1 and allow_missing_final and selector is None:
            if not isinstance(current, dict) or name is None:
                raise QualificationError(f"mutation final target is not an object field: {path}")
            return current, name
        current = _step(current, token, path)
    return current


def _resolve_parent(document: Any, path: str) -> tuple[Any, tuple[str | None, str | int | None]]:
    tokens = _path_tokens(path)
    if not tokens:
        raise QualificationError(f"mutation path needs a child target: {path}")
    current = document
    for token in tokens[:-1]:
        current = _step(current, token, path)
    return current, tokens[-1]


def _set_token(parent: Any, token: tuple[str | None, str | int | None], value: Any, path: str) -> None:
    name, selector = token
    if name is not None:
        if not isinstance(parent, dict):
            raise QualificationError(f"mutation parent is not an object: {path}")
        if selector is None:
            parent[name] = value
            return
        if name not in parent:
            raise QualificationError(f"mutation path does not exist: {path}")
        target = parent[name]
    else:
        target = parent
    if isinstance(selector, int):
        if not isinstance(target, list) or selector >= len(target):
            raise QualificationError(f"mutation list index does not exist: {path}")
        target[selector] = value
        return
    if isinstance(selector, str):
        if not isinstance(target, list) or "=" not in selector:
            raise QualificationError(f"mutation selector is not a list selector: {path}")
        field, expected = selector.split("=", 1)
        for item in target:
            if isinstance(item, dict) and str(item.get(field)) == expected:
                raise QualificationError("set cannot replace a selected list item; select its field")
        raise QualificationError(f"mutation selector found no item: {path}")
    raise QualificationError(f"invalid mutation target: {path}")


def _delete_token(parent: Any, token: tuple[str | None, str | int | None], path: str) -> None:
    name, selector = token
    if name is not None:
        if not isinstance(parent, dict) or name not in parent:
            raise QualificationError(f"mutation path does not exist: {path}")
        if selector is None:
            del parent[name]
            return
        target = parent[name]
    else:
        target = parent
    if isinstance(selector, int):
        if not isinstance(target, list) or selector >= len(target):
            raise QualificationError(f"mutation list index does not exist: {path}")
        del target[selector]
        return
    raise QualificationError(f"mutation delete target is not an indexed list item: {path}")


def _apply_operation(document: dict[str, Any], operation: dict[str, Any]) -> None:
    if not isinstance(operation, dict) or not isinstance(operation.get("op"), str):
        raise QualificationError("corpus operation must have a string op")
    op = operation["op"]
    path = operation.get("path")
    if not isinstance(path, str):
        raise QualificationError(f"corpus operation {op} has no path")
    if op == "set":
        if "field" in operation:
            target = _resolve(document, path)
            if not isinstance(target, dict) or not isinstance(operation["field"], str):
                raise QualificationError(f"set field target is not an object: {path}")
            target[operation["field"]] = deepcopy(operation.get("value"))
        else:
            parent, token = _resolve_parent(document, path)
            _set_token(parent, token, deepcopy(operation.get("value")), path)
        return
    if op == "delete":
        parent, token = _resolve_parent(document, path)
        _delete_token(parent, token, path)
        return
    if op == "append":
        try:
            target = _resolve(document, path)
        except QualificationError:
            parent, token = _resolve_parent(document, path)
            name, selector = token
            if name is None or selector is not None or not isinstance(parent, dict):
                raise
            parent[name] = []
            target = parent[name]
        if not isinstance(target, list):
            raise QualificationError(f"append target is not a list: {path}")
        target.append(deepcopy(operation.get("value")))
        return
    if op == "duplicate-first":
        target = _resolve(document, path)
        if not isinstance(target, list) or not target:
            raise QualificationError(f"duplicate-first target is not a non-empty list: {path}")
        target.append(deepcopy(target[0]))
        return
    raise QualificationError(f"unsupported corpus operation: {op}")


def _mutated_document(source: Path, operations: Sequence[dict[str, Any]]) -> dict[str, Any]:
    document = _read_json(source)
    if not isinstance(document, dict):
        raise QualificationError(f"corpus source is not an object: {source}")
    for operation in operations:
        _apply_operation(document, operation)
    return document


def _case_result_base(case: dict[str, Any], source: Path) -> dict[str, Any]:
    return {
        "caseId": case.get("id"),
        "category": case.get("category", "schema"),
        "source": source.relative_to(ROOT).as_posix(),
        "operations": case.get("operations", []),
    }


def _load_corpus(path: Path) -> dict[str, Any]:
    corpus = _read_json(path)
    if not isinstance(corpus, dict) or corpus.get("schema") != "fdir/qualification-issue-90-corpus":
        raise QualificationError("issue #90 corpus schema is invalid")
    if corpus.get("version") != "1.0.0" or corpus.get("issueNumber") != 90:
        raise QualificationError("issue #90 corpus version or issue binding is invalid")
    for key in ("schemaCases", "graphCases", "statusCases"):
        cases = corpus.get(key)
        if not isinstance(cases, list) or not cases or not all(isinstance(item, dict) for item in cases):
            raise QualificationError(f"issue #90 corpus has no valid {key}")
    return corpus


def _validate_bases(corpus: dict[str, Any]) -> list[dict[str, Any]]:
    """Ensure every fixture used by a mutation is a currently valid IR."""

    try:
        from ir_validation import validate_document
    except ImportError:
        from tools.ir_validation import validate_document
    paths = {
        str(case["source"])
        for key in ("schemaCases", "graphCases", "statusCases")
        for case in corpus[key]
        if isinstance(case.get("source"), str)
    }
    results: list[dict[str, Any]] = []
    for relative in sorted(paths):
        source = _repository_path(relative)
        if not source.is_file():
            raise QualificationError(f"issue #90 corpus source is missing: {relative}")
        document = _read_json(source)
        if not isinstance(document, dict):
            raise QualificationError(f"issue #90 corpus source is not an object: {relative}")
        try:
            warnings = validate_document(document)
        except Exception as exc:
            raise QualificationError(f"corpus base fixture is invalid: {relative}: {exc}") from exc
        results.append({
            "source": relative,
            "status": "passed",
            "warningCount": len(warnings),
            "sha256": _sha256_file(source),
        })
    return results


def _authority(root: Path) -> dict[str, Any]:
    try:
        try:
            from validate_model_contract import validate_contract
        except ImportError:
            from tools.validate_model_contract import validate_contract
        findings = validate_contract()
    except Exception as exc:
        findings = [f"model-contract check raised {type(exc).__name__}: {exc}"]
    paths = {
        "schema": DEFAULT_SCHEMA_PATH,
        "referenceRegistry": REGISTRY_PATH,
        "runtimeValidator": RUNTIME_PATH,
        "modelContract": MODEL_CONTRACT_PATH,
    }
    digests: dict[str, str] = {}
    for name, path in paths.items():
        if not path.is_file():
            raise QualificationError(f"authority input is missing: {path}")
        digests[name] = _sha256_file(path)
    return {
        "normativeSchema": "schemas/document-form-ir.schema.json",
        "referenceRegistry": "machine/reference-registry.json",
        "runtimeValidator": "tools/ir_validation.py",
        "generatedModelContract": "machine/model-contract.json",
        "modelContractDriftFindings": findings,
        "digests": digests,
    }


def _schema_report(
    corpus: dict[str, Any],
    validator: Any,
    validator_info: dict[str, str],
    source_sha: str,
    authority: dict[str, Any],
    base_fixtures: list[dict[str, Any]],
) -> dict[str, Any]:
    try:
        from ir_validation import validate_document
    except ImportError:
        from tools.ir_validation import validate_document
    cases: list[dict[str, Any]] = []
    for case in corpus["schemaCases"]:
        source = _repository_path(str(case["source"]))
        document = _mutated_document(source, case.get("operations", []))
        expected = bool(case.get("expectedSchemaValid"))
        external = _external_result(validator, document)
        custom = _custom_schema_result(document)
        runtime = _runtime_result(document)
        external_ok = external.get("accepted") is expected
        custom_ok = custom.get("accepted") is expected
        differential_ok = external.get("accepted") == custom.get("accepted")
        result = "passed" if external_ok and custom_ok and differential_ok else "failed"
        item = _case_result_base(case, source)
        item.update({
            "expectedSchemaValid": expected,
            "external": external,
            "fdirSchemaSubset": custom,
            "runtimeFullValidator": runtime,
            "result": result,
            "assertions": {
                "externalMatchesExpected": external_ok,
                "fdirSubsetMatchesExpected": custom_ok,
                "externalMatchesFdirSubset": differential_ok,
            },
        })
        cases.append(item)
    positive_sources = sorted({str(case["source"]) for case in corpus["schemaCases"]})
    positive_cases: list[dict[str, Any]] = []
    for relative in positive_sources:
        source = _repository_path(relative)
        document = _read_json(source)
        external = _external_result(validator, document)
        custom = _custom_schema_result(document)
        try:
            runtime_warnings = validate_document(document)
            runtime = {"accepted": True, "warnings": runtime_warnings}
        except Exception as exc:
            runtime = {"accepted": False, "message": str(exc)}
        result = "passed" if external.get("accepted") and custom.get("accepted") else "failed"
        positive_cases.append({
            "caseId": f"positive:{Path(relative).name}",
            "source": relative,
            "expectedSchemaValid": True,
            "external": external,
            "fdirSchemaSubset": custom,
            "runtimeFullValidator": runtime,
            "result": result,
        })
    cases = positive_cases + cases
    mismatches = [item for item in cases if item["result"] != "passed"]
    authority_ok = not authority["modelContractDriftFindings"]
    assertions = [
        {
            "assertionId": "external-draft-2020-12-authority",
            "expected": "jsonschema.Draft202012Validator",
            "actual": f"{validator_info['library']}.{validator_info['class']}",
            "status": "passed",
        },
        {
            "assertionId": "external-schema-check",
            "expected": True,
            "actual": True,
            "status": "passed",
        },
        {
            "assertionId": "schema-runtime-differential-mismatch-count",
            "expected": 0,
            "actual": len(mismatches),
            "status": "passed" if not mismatches else "failed",
        },
        {
            "assertionId": "generated-model-contract-current",
            "expected": True,
            "actual": authority_ok,
            "status": "passed" if authority_ok else "failed",
        },
    ]
    return {
        "schema": "fdir/qualification-issue-90-report",
        "version": "1.0.0",
        "issueNumber": 90,
        "reportKind": "schema-differential",
        "sourceSha": source_sha,
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "validator": validator_info,
        "authority": authority,
        "baseFixtures": base_fixtures,
        "caseCount": len(cases),
        "positiveCaseCount": len(positive_cases),
        "negativeCaseCount": len(cases) - len(positive_cases),
        "mismatchCount": len(mismatches),
        "cases": cases,
        "assertions": assertions,
        "status": "passed" if not mismatches and authority_ok else "failed",
        "limitations": [
            "The differential corpus is explicit and finite; it is not proof of every JSON Schema keyword combination.",
            "Graph and status semantics are intentionally reported separately from schema acceptance.",
        ],
    }


def _semantic_report(
    corpus: dict[str, Any],
    validator: Any,
    source_sha: str,
    authority: dict[str, Any],
    base_fixtures: list[dict[str, Any]],
    *,
    kind: str,
) -> dict[str, Any]:
    key = "graphCases" if kind == "graph" else "statusCases"
    cases: list[dict[str, Any]] = []
    for case in corpus[key]:
        source = _repository_path(str(case["source"]))
        document = _mutated_document(source, case.get("operations", []))
        external = _external_result(validator, document)
        runtime = _runtime_result(document)
        expected = case.get("expectedDiagnostic")
        actual = runtime.get("diagnostic")
        message = str(runtime.get("message", ""))
        code_ok = actual == expected
        path_ok = not case.get("expectedPath") or str(case["expectedPath"]) in message
        result = "passed" if code_ok and path_ok else "failed"
        item = _case_result_base(case, source)
        item.update({
            "expectedDiagnostic": expected,
            "expectedPath": case.get("expectedPath"),
            "externalSchemaAccepted": external.get("accepted"),
            "runtime": runtime,
            "result": result,
            "assertions": {
                "expectedDiagnostic": code_ok,
                "minimumFailingPath": path_ok,
            },
        })
        cases.append(item)
    required_key = "requiredGraphCategories" if kind == "graph" else "requiredStatusCategories"
    required = corpus.get(required_key, [])
    executed = sorted({str(case.get("category")) for case in corpus[key] if case.get("category")})
    missing = sorted(set(required) - set(executed))
    case_failures = [item for item in cases if item["result"] != "passed"]
    authority_ok = not authority["modelContractDriftFindings"]
    coverage_ok = not missing
    assertions = [
        {
            "assertionId": "single-defect-negative-cases-detected",
            "expected": 0,
            "actual": len(case_failures),
            "status": "passed" if not case_failures else "failed",
        },
        {
            "assertionId": "required-category-coverage",
            "expected": True,
            "actual": coverage_ok,
            "status": "passed" if coverage_ok else "failed",
        },
        {
            "assertionId": "generated-model-contract-current",
            "expected": True,
            "actual": authority_ok,
            "status": "passed" if authority_ok else "failed",
        },
    ]
    limitations = []
    if missing:
        limitations.append("Required categories are not yet represented by an executable single-defect case: " + ", ".join(missing))
    if case_failures:
        limitations.append("At least one expected invariant was not detected with the declared diagnostic code/path.")
    return {
        "schema": "fdir/qualification-issue-90-report",
        "version": "1.0.0",
        "issueNumber": 90,
        "reportKind": "graph-invariants" if kind == "graph" else "status-contract",
        "sourceSha": source_sha,
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "authority": authority,
        "baseFixtures": base_fixtures,
        "caseCount": len(cases),
        "failureCount": len(case_failures),
        "coverage": {
            "requiredCategories": required,
            "executedCategories": executed,
            "missingCategories": missing,
            "complete": coverage_ok,
        },
        "cases": cases,
        "assertions": assertions,
        "status": "passed" if not case_failures and coverage_ok and authority_ok else "failed",
        "limitations": limitations,
    }


def _fatal_report(kind: str, source_sha: str | None, message: str) -> dict[str, Any]:
    return {
        "schema": "fdir/qualification-issue-90-report",
        "version": "1.0.0",
        "issueNumber": 90,
        "reportKind": REPORT_NAMES[kind].removesuffix(".json"),
        "sourceSha": source_sha,
        "status": "failed",
        "failure": {
            "code": "QUALIFICATION-SETUP-FAILED",
            "message": message,
        },
        "assertions": [
            {
                "assertionId": "qualification-setup",
                "expected": "executable",
                "actual": "unavailable",
                "status": "failed",
            }
        ],
        "limitations": ["No qualification result is valid when setup fails."],
    }


def run_qualification(
    *,
    schema_path: Path = DEFAULT_SCHEMA_PATH,
    corpus_path: Path = DEFAULT_CORPUS_PATH,
    out_dir: Path = DEFAULT_OUT_DIR,
) -> int:
    """Run issue #90 and write all three reports; return a process exit code."""

    source_sha: str | None = None
    try:
        source_sha = _source_sha()
        schema = _read_json(schema_path)
        if not isinstance(schema, dict):
            raise QualificationError("normative schema root is not an object")
        corpus = _load_corpus(corpus_path)
        authority = _authority(ROOT)
        validator, validator_info = _load_external_validator(schema)
        base_fixtures = _validate_bases(corpus)
        reports = {
            "schema": _schema_report(
                corpus, validator, validator_info, source_sha, authority, base_fixtures
            ),
            "graph": _semantic_report(
                corpus, validator, source_sha, authority, base_fixtures, kind="graph"
            ),
            "status": _semantic_report(
                corpus, validator, source_sha, authority, base_fixtures, kind="status"
            ),
        }
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        reports = {kind: _fatal_report(kind, source_sha, message) for kind in REPORT_NAMES}
        out_dir.mkdir(parents=True, exist_ok=True)
        for kind, name in REPORT_NAMES.items():
            _write_json(out_dir / name, reports[kind])
        print(f"FAIL: issue #90 qualification setup: {message}", file=sys.stderr)
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    for kind, name in REPORT_NAMES.items():
        _write_json(out_dir / name, reports[kind])
    failed = [kind for kind, report in reports.items() if report.get("status") != "passed"]
    if failed:
        print("FAIL: issue #90 qualification reports: " + ", ".join(failed), file=sys.stderr)
        return 1
    print(
        "PASS: issue #90 qualification reports written: "
        + ", ".join(REPORT_NAMES.values())
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA_PATH)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_PATH)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return run_qualification(
        schema_path=args.schema,
        corpus_path=args.corpus,
        out_dir=args.out_dir,
    )


if __name__ == "__main__":
    raise SystemExit(main())
