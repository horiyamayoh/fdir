"""Run the bounded, independent-oracle qualification lane for issue #97.

The corpus is authored evidence.  This runner reads the current registry,
extension schema, runtime validator, canonicalizer, query implementation, and
adapter source as systems under test, but never asks an adapter helper to
manufacture an expected value.  Static AST inspection is used for adapter
emission coverage so a new literal emission cannot hide behind a helper.

Every required report is written on both success and failure.  A report with
unmet assertions is deliberately ``failed`` and the lane returns exit status 1;
the bounded lane never turns an incomplete implementation into a completion
claim.
"""

from __future__ import annotations

import argparse
import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Literal
import uuid

try:
    from qualification_producer_report import write_producer_report
except ImportError:  # pragma: no cover - package-style imports.
    from tools.qualification_producer_report import write_producer_report


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS_PATH = ROOT / "machine" / "qualification-issue-97-corpus.json"
DEFAULT_OUT_DIR = ROOT / "e2e" / ".run" / "qualification-issue-97"
REGISTRY_PATH = ROOT / "machine" / "extension-registry.json"
EXTENSION_SCHEMA_PATH = ROOT / "schemas" / "extensions" / "format-extensions.schema.json"
RUNTIME_PATH = ROOT / "tools" / "extension_registry.py"
CANONICALIZER_PATH = ROOT / "tools" / "canonicalize_ir.py"
QUERY_PATH = ROOT / "tools" / "query_ir.py"
ADAPTER_PATHS = (
    ROOT / "tools" / "adapter_docx.py",
    ROOT / "tools" / "adapter_xlsx.py",
    ROOT / "tools" / "adapter_pdf.py",
    ROOT / "tools" / "adapter_markdown.py",
)

REPORT_NAMES = {
    "emission": "extension-emission-coverage.json",
    "schema": "extension-schema-conformance.json",
    "reference": "extension-reference-closure.json",
    "version": "extension-version-compatibility.json",
    "migration": "extension-migration-report.json",
}
EVIDENCE_ID = "issue-97-extension-registry"
REQUIREMENT_ID = "QUAL-97-EXTENSION-CLOSURE"
Issue97EvaluatorType = Literal["extension-closure", "mutation-killed"]
EXTENSION_EVALUATOR: Issue97EvaluatorType = "extension-closure"
MUTATION_EVALUATOR: Issue97EvaluatorType = "mutation-killed"
# The schema report is a declared output, but its role is intentionally not
# used as an artifact reference because the bundle validator treats schema
# snapshots as source/static evidence.
PRODUCER_ARTIFACT_REPORT_NAMES = (
    REPORT_NAMES["emission"],
    REPORT_NAMES["reference"],
    REPORT_NAMES["version"],
    REPORT_NAMES["migration"],
)

REQUIRED_NEGATIVE_CASES = {
    "unregistered-emission",
    "unused-read-only-drift",
    "criticality-mismatch",
    "wrong-target-kind",
    "wrong-target-collection",
    "dangling-payload-reference",
    "unknown-critical-policy",
    "unknown-non-critical-policy",
    "unknown-field",
    "major-version-incompatibility",
    "minor-version-breaking",
    "migration-loss",
    "migration-receipt-drop",
    "migration-dropped-fields-drop",
    "round-trip-loss",
    "query-canonical-round-trip-loss",
}

SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
ADAPTER_FORMATS = {
    "adapter_docx.py": "docx",
    "adapter_xlsx.py": "xlsx",
    "adapter_pdf.py": "pdf",
    "adapter_markdown.py": "markdown",
}


class QualificationError(RuntimeError):
    """Raised when the bounded qualification cannot be executed safely."""


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


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_sha() -> str:
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise QualificationError(f"cannot obtain exact 40-character source SHA: {value!r}")
    return value


def _posix(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _key(value: dict[str, Any]) -> tuple[str, str, str]:
    return (str(value.get("namespace", "")), str(value.get("type", "")), str(value.get("schemaVersion", "")))


def _key_text(value: dict[str, Any] | tuple[str, str, str]) -> str:
    if isinstance(value, tuple):
        return ":".join(value)
    return ":".join(_key(value))


def _literal(node: ast.AST | None) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    return None


def _expression(node: ast.AST | None) -> str:
    if node is None:
        return "<missing>"
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - Python 3.9 fallback
        return type(node).__name__


def _dict_literals(node: ast.AST | None) -> dict[str, Any]:
    if not isinstance(node, ast.Dict):
        return {}
    result: dict[str, Any] = {}
    for raw_key, raw_value in zip(node.keys, node.values):
        key = _literal(raw_key)
        if isinstance(key, str):
            result[key] = _literal(raw_value)
    return result


def _validate_corpus(corpus: dict[str, Any]) -> dict[str, Any]:
    if corpus.get("issueNumber") != 97:
        raise QualificationError("issue #97 corpus has the wrong issue number")
    if corpus.get("qualificationScope") != "bounded-independent-extension-registry-typed-payload-reference-version-migration-query-canonical":
        raise QualificationError("issue #97 corpus is not marked as the bounded lane")
    if corpus.get("reportNames") != list(REPORT_NAMES.values()):
        raise QualificationError("issue #97 corpus report names do not match the required five reports")
    oracle = corpus.get("oracle")
    if not isinstance(oracle, dict):
        raise QualificationError("issue #97 corpus has no oracle declaration")
    if oracle.get("expectedValuesAreRuntimeIndependent") is not True:
        raise QualificationError("issue #97 corpus does not declare an independent expected-value oracle")
    if oracle.get("adapterHelpersUsedForExpected") is not False:
        raise QualificationError("issue #97 corpus permits adapter-derived expected values")
    if not isinstance(oracle.get("forbiddenDerivations"), list) or not oracle["forbiddenDerivations"]:
        raise QualificationError("issue #97 corpus has no forbidden derivation list")

    expected_registry = corpus.get("expectedRegistry")
    if not isinstance(expected_registry, list) or len(expected_registry) < 20:
        raise QualificationError("issue #97 corpus has too few authored registry entries")
    registry_keys: set[tuple[str, str, str]] = set()
    for entry in expected_registry:
        if not isinstance(entry, dict):
            raise QualificationError("authored registry entry is not an object")
        current_key = _key(entry)
        if "" in current_key or current_key in registry_keys:
            raise QualificationError(f"invalid or duplicate authored registry key: {current_key}")
        registry_keys.add(current_key)
        if not isinstance(entry.get("targetCollections"), list) or not entry["targetCollections"]:
            raise QualificationError(f"authored registry entry lacks target collection: {current_key}")
        if not isinstance(entry.get("targetKinds"), list) or not entry["targetKinds"]:
            raise QualificationError(f"authored registry entry lacks target kinds: {current_key}")
        if entry.get("criticality") not in {"critical", "non-critical"}:
            raise QualificationError(f"authored registry entry has invalid criticality: {current_key}")

    sites = corpus.get("emissionSites")
    if not isinstance(sites, list) or not sites:
        raise QualificationError("issue #97 corpus has no authored emission sites")
    site_ids: set[str] = set()
    for site in sites:
        if not isinstance(site, dict) or not isinstance(site.get("siteId"), str):
            raise QualificationError("authored emission site is malformed")
        if site["siteId"] in site_ids:
            raise QualificationError(f"duplicate authored emission site: {site['siteId']}")
        site_ids.add(site["siteId"])
        if not isinstance(site.get("line"), int) or site["line"] < 1:
            raise QualificationError(f"authored emission site has no source line: {site['siteId']}")
        if _key(site) not in registry_keys:
            raise QualificationError(f"authored emission site is not backed by authored registry: {site['siteId']}")

    schema_cases = corpus.get("schemaCases")
    if not isinstance(schema_cases, list) or not schema_cases:
        raise QualificationError("issue #97 corpus has no schema cases")
    schema_case_keys: set[tuple[str, str, str]] = set()
    for case in schema_cases:
        if not isinstance(case, dict) or not isinstance(case.get("caseId"), str):
            raise QualificationError("authored schema case is malformed")
        case_key = case.get("key")
        if not isinstance(case_key, dict) or _key(case_key) not in registry_keys:
            raise QualificationError(f"schema case has unknown registry key: {case.get('caseId')}")
        if _key(case_key) in schema_case_keys:
            raise QualificationError(f"duplicate schema case key: {_key_text(case_key)}")
        schema_case_keys.add(_key(case_key))
        if not isinstance(case.get("payload"), dict):
            raise QualificationError(f"schema case payload is not an object: {case['caseId']}")
    if schema_case_keys != registry_keys:
        raise QualificationError("schema cases are not exhaustive over the authored registry")

    negatives = corpus.get("negativeMutations")
    if not isinstance(negatives, list):
        raise QualificationError("issue #97 corpus has no negative mutations")
    negative_ids = {item.get("caseId") for item in negatives if isinstance(item, dict)}
    missing_negatives = sorted(REQUIRED_NEGATIVE_CASES - negative_ids)
    if missing_negatives:
        raise QualificationError(f"issue #97 corpus is missing required negatives: {missing_negatives}")
    for item in negatives:
        if not isinstance(item, dict) or not isinstance(item.get("caseId"), str):
            raise QualificationError("negative mutation is malformed")
        mutation = item.get("mutation")
        if not isinstance(mutation, dict) or mutation.get("op") not in {"set", "delete", "append"}:
            raise QualificationError(f"negative mutation has an invalid operation: {item.get('caseId')}")
        if not isinstance(mutation.get("path"), str) or not mutation["path"].startswith("/"):
            raise QualificationError(f"negative mutation has an invalid path: {item.get('caseId')}")

    if not isinstance(corpus.get("referenceDocument"), dict):
        raise QualificationError("issue #97 corpus has no reference document")
    if not isinstance(corpus.get("referenceContracts"), list) or not corpus["referenceContracts"]:
        raise QualificationError("issue #97 corpus has no payload reference contracts")
    if not isinstance(corpus.get("policyCases"), list) or not corpus["policyCases"]:
        raise QualificationError("issue #97 corpus has no criticality/unknown policy cases")
    if not isinstance(corpus.get("versionMatrix"), dict):
        raise QualificationError("issue #97 corpus has no version matrix")
    for name in ("exact", "patch", "minorAdditive", "minorBreaking", "major"):
        if not isinstance(corpus["versionMatrix"].get(name), dict):
            raise QualificationError(f"issue #97 version matrix lacks {name}")
    if not isinstance(corpus.get("migrationCases"), list) or not corpus["migrationCases"]:
        raise QualificationError("issue #97 corpus has no migration cases")
    if not isinstance(corpus.get("roundTripDocument"), dict):
        raise QualificationError("issue #97 corpus has no query/canonical round-trip document")
    return corpus


def _load_corpus(path: Path = DEFAULT_CORPUS_PATH) -> dict[str, Any]:
    value = _read_json(path)
    if not isinstance(value, dict):
        raise QualificationError("issue #97 corpus root must be an object")
    return _validate_corpus(value)


class _EmissionVisitor(ast.NodeVisitor):
    """Find both helper calls and direct extension dictionaries without execution."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.format_name = ADAPTER_FORMATS[path.name]
        self.scope: list[str] = []
        self.sites: list[dict[str, Any]] = []
        self.constructor_violations: list[dict[str, Any]] = []

    def _site_base(self, node: ast.Call, kind: str, values: dict[str, Any]) -> dict[str, Any]:
        return {
            "siteId": f"{_posix(self.path)}:{node.lineno}",
            "path": _posix(self.path),
            "line": node.lineno,
            "kind": kind,
            "scope": ".".join(self.scope),
            "namespace": values.get("namespace") or f"urn:fdir:format:{self.format_name}",
            "type": values.get("type"),
            "schemaVersion": values.get("schemaVersion") or "1.0.0",
            "schemaId": values.get("schemaId"),
            "criticality": values.get("criticality") or "non-critical",
        }

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.append(node.name)
        if node.name == "_extension":
            arguments = {item.arg: item.annotation for item in node.args.args}
            payload_annotation = _expression(arguments.get("payload"))
            if "dict" in payload_annotation.lower() or "any" in payload_annotation.lower():
                self.constructor_violations.append({
                    "path": _posix(self.path),
                    "line": node.lineno,
                    "kind": "free-form-payload-annotation",
                    "detail": payload_annotation,
                })
        self.generic_visit(node)
        self.scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # pragma: no cover - adapters are synchronous today
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id == "_extension":
            values = {
                "type": _literal(node.args[2]) if len(node.args) >= 3 else None,
                "criticality": next((_literal(item.value) for item in node.keywords if item.arg == "criticality"), None),
            }
            site = self._site_base(node, "constructor-call", values)
            site["targetExpression"] = _expression(node.args[1] if len(node.args) >= 2 else None)
            extension_type = site.get("type")
            if isinstance(extension_type, str):
                site["schemaId"] = f"urn:fdir:schema:{self.format_name}-{extension_type}"
            else:
                site["schemaId"] = "<dynamic>"
            self.sites.append(site)

        is_add_item = (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_item"
            and len(node.args) >= 2
            and _literal(node.args[0]) == "extensions"
        )
        if is_add_item and "_extension" not in self.scope:
            values = _dict_literals(node.args[1])
            site = self._site_base(node, "direct-dict", values)
            site["targetExpression"] = _expression(values.get("targetId") if isinstance(values.get("targetId"), ast.AST) else None)
            self.sites.append(site)
            self.constructor_violations.append({
                "path": _posix(self.path),
                "line": node.lineno,
                "kind": "direct-extension-dict",
                "detail": "builder.add_item('extensions', {...}) bypasses a typed constructor",
            })
        self.generic_visit(node)


def _scan_emission_sites() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sites: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for path in ADAPTER_PATHS:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            raise QualificationError(f"cannot parse adapter emission source {path}: {exc}") from exc
        visitor = _EmissionVisitor(path)
        visitor.visit(tree)
        sites.extend(visitor.sites)
        violations.extend(visitor.constructor_violations)
    sites.sort(key=lambda item: (str(item["path"]), int(item["line"])))
    violations.sort(key=lambda item: (str(item["path"]), int(item["line"]), str(item["kind"])))
    return sites, violations


def _registry_key_map(registry: dict[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
    entries = registry.get("entries")
    if not isinstance(entries, list):
        raise QualificationError("under-test extension registry has no entries array")
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise QualificationError("under-test extension registry contains a non-object entry")
        current_key = _key(entry)
        if current_key in result:
            raise QualificationError(f"under-test extension registry has a duplicate key: {current_key}")
        result[current_key] = entry
    return result


def _resolve_fragment(root: dict[str, Any], reference: str) -> dict[str, Any]:
    if "#" not in reference:
        raise QualificationError(f"schema path has no fragment: {reference}")
    _path, fragment = reference.split("#", 1)
    if not fragment.startswith("/"):
        raise QualificationError(f"schema fragment is not a pointer: {reference}")
    current: Any = root
    for raw_part in fragment[1:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or part not in current:
            raise QualificationError(f"unresolved schema fragment: {reference}")
        current = current[part]
    if not isinstance(current, dict):
        raise QualificationError(f"schema fragment is not an object: {reference}")
    return current


def _schema_fragment(entry: dict[str, Any], schema_root: dict[str, Any]) -> dict[str, Any]:
    schema_path = entry.get("schemaPath")
    if not isinstance(schema_path, str):
        raise QualificationError(f"registry entry has no schemaPath: {_key_text(entry)}")
    path_text = schema_path.split("#", 1)[0]
    path = ROOT / path_text
    if not path.is_file():
        raise QualificationError(f"registry schemaPath does not exist: {schema_path}")
    if path.resolve() != EXTENSION_SCHEMA_PATH.resolve():
        schema_root = _read_json(path)
        if not isinstance(schema_root, dict):
            raise QualificationError(f"extension schema is not an object: {path}")
    return _resolve_fragment(schema_root, schema_path)


def _jsonschema_errors(
    payload: dict[str, Any],
    schema: dict[str, Any],
    root_schema: dict[str, Any] | None = None,
) -> list[str]:
    try:
        from jsonschema import Draft202012Validator, RefResolver
    except ImportError as exc:  # pragma: no cover - locked qualification dependency
        raise QualificationError("jsonschema is required for independent payload validation") from exc
    # Registry entries point at fragments in the shared extension schema.  A
    # fragment containing ``#/$defs/...`` references cannot be validated as a
    # detached object: its local root has no $defs table.  Keep the fragment
    # as the validation target while resolving references against the
    # independently loaded document root.
    resolver = RefResolver.from_schema(root_schema or schema)
    validator = Draft202012Validator(schema, resolver=resolver)
    return [error.message for error in sorted(validator.iter_errors(payload), key=lambda item: list(item.path))]


def _runtime_imports() -> dict[str, Any]:
    tools_path = str(ROOT / "tools")
    if tools_path not in sys.path:
        sys.path.insert(0, tools_path)
    try:
        from canonicalize_ir import canonical_digest, migrate_document, migrate_extensions
        from extension_registry import validate_extension
        from ir_validation import validate_document
        from query_ir import find_extensions, get_field, query_field_coverage, rebuild_index
    except ImportError:  # pragma: no cover - package-style import
        from tools.canonicalize_ir import canonical_digest, migrate_document, migrate_extensions
        from tools.extension_registry import validate_extension
        from tools.ir_validation import validate_document
        from tools.query_ir import find_extensions, get_field, query_field_coverage, rebuild_index
    return {
        "canonical_digest": canonical_digest,
        "migrate_document": migrate_document,
        "migrate_extensions": migrate_extensions,
        "validate_extension": validate_extension,
        "validate_document": validate_document,
        "find_extensions": find_extensions,
        "get_field": get_field,
        "query_field_coverage": query_field_coverage,
        "rebuild_index": rebuild_index,
    }


def _simple_document(extension: dict[str, Any], status: str, *, diagnostic: bool = False, target_kind: str = "paragraph") -> dict[str, Any]:
    diagnostics: list[dict[str, Any]] = []
    diagnostic_ids: list[str] = []
    if diagnostic:
        diagnostic_id = "diagnostic-unknown-extension"
        diagnostic_ids.append(diagnostic_id)
        diagnostics.append({
            "diagnosticId": diagnostic_id,
            "code": "DFIR-EXTENSION-UNKNOWN-OPAQUE",
            "severity": "warning",
            "message": "Unknown non-critical extension retained as opaque payload.",
            "targetId": extension["extensionId"],
            "action": "review",
        })
    return {
        "schema": {"name": "fdir/document-form", "version": "1.0.0"},
        "documentId": "doc-policy",
        "sourceFormat": {"namespace": "format", "name": "markdown", "version": "commonmark"},
        "rootNodeId": "node-target",
        "nodes": [{"nodeId": "node-target", "kind": target_kind, "childIds": [], "status": "preserved"}],
        "extensions": [extension],
        "diagnostics": diagnostics,
        "conversion": {"status": status, "features": [], "diagnostics": diagnostic_ids},
    }


def _runtime_extension_result(
    extension: dict[str, Any],
    status: str,
    *,
    diagnostic: bool = False,
    target_kind: str = "paragraph",
    validate_full_document: bool = True,
) -> tuple[str, str | None]:
    runtime = _runtime_imports()
    document = _simple_document(extension, status, diagnostic=diagnostic, target_kind=target_kind)
    try:
        result = runtime["validate_extension"](
            extension,
            {"conversion": {"status": status}},
            {"node-target": "nodes"},
            {"node-target": target_kind},
        )
        if validate_full_document and status in {"complete", "complete-with-warnings", "partial"}:
            runtime["validate_document"](document)
        return str(result), None
    except Exception as exc:
        return "rejected", f"{type(exc).__name__}: {exc}"


def _apply_mutation(value: Any, mutation: dict[str, Any]) -> Any:
    result = deepcopy(value)
    path = mutation.get("path")
    if not isinstance(path, str) or not path.startswith("/"):
        raise QualificationError(f"mutation path is invalid: {path!r}")
    segments = [item.replace("~1", "/").replace("~0", "~") for item in path[1:].split("/") if item != ""]
    if not segments:
        raise QualificationError("root mutation is not supported")
    current = result
    for segment in segments[:-1]:
        if isinstance(current, dict):
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit():
            current = current[int(segment)]
        else:
            raise QualificationError(f"mutation path does not resolve: {path}")
    leaf = segments[-1]
    operation = mutation.get("op")
    if isinstance(current, dict):
        if operation == "set":
            current[leaf] = deepcopy(mutation.get("value"))
        elif operation == "delete":
            current.pop(leaf, None)
        elif operation == "append":
            target = current.get(leaf)
            if not isinstance(target, list):
                raise QualificationError(f"mutation append target is not an array: {path}")
            target.append(deepcopy(mutation.get("value")))
        else:
            raise QualificationError(f"unknown mutation operation: {operation}")
    elif isinstance(current, list) and leaf.isdigit():
        index = int(leaf)
        if operation == "delete":
            current.pop(index)
        elif operation == "set":
            current[index] = deepcopy(mutation.get("value"))
        else:
            raise QualificationError(f"unsupported list mutation: {operation}")
    else:
        raise QualificationError(f"mutation leaf does not resolve: {path}")
    return result


def _pointer(value: Any, path: str) -> Any:
    current = value
    if path in {"", "/"}:
        return current
    for raw in path[1:].split("/"):
        segment = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit():
            current = current[int(segment)]
        else:
            raise KeyError(path)
    return current


def _emission_evidence(corpus: dict[str, Any], registry: dict[str, Any]) -> dict[str, Any]:
    actual_sites, constructor_violations = _scan_emission_sites()
    actual_registry = _registry_key_map(registry)
    authored_sites = list(corpus["emissionSites"])
    authored_groups = _group_fingerprints(authored_sites)
    observed_groups = _group_fingerprints(actual_sites)
    missing_fingerprints: list[dict[str, Any]] = []
    unexpected_fingerprints: list[dict[str, Any]] = []
    for fingerprint, expected_values in authored_groups.items():
        actual_values = observed_groups.get(fingerprint, [])
        if len(actual_values) < len(expected_values):
            missing_fingerprints.append({"fingerprint": list(fingerprint), "count": len(expected_values) - len(actual_values)})
    for fingerprint, actual_values in observed_groups.items():
        expected_values = authored_groups.get(fingerprint, [])
        if len(actual_values) > len(expected_values):
            unexpected_fingerprints.append({"fingerprint": list(fingerprint), "count": len(actual_values) - len(expected_values)})
    line_drift: list[dict[str, Any]] = []
    site_mismatches: list[dict[str, Any]] = []
    for fingerprint in sorted(set(authored_groups) & set(observed_groups), key=str):
        authored_values = authored_groups[fingerprint]
        observed_values = observed_groups[fingerprint]
        for authored, observed in zip(authored_values, observed_values):
            if authored.get("line") != observed.get("line"):
                line_drift.append({
                    "path": authored.get("path"),
                    "type": authored.get("type"),
                    "authoredLine": authored.get("line"),
                    "observedLine": observed.get("line"),
                    "selector": list(fingerprint),
                })
        fields = ("namespace", "type", "schemaVersion", "schemaId", "criticality")
        for authored, observed in zip(authored_values, observed_values):
            mismatch = {
                field: {"expected": authored.get(field), "actual": observed.get(field)}
                for field in fields
                if authored.get(field) != observed.get(field)
            }
            if mismatch:
                site_mismatches.append({"siteId": authored.get("siteId"), "fields": mismatch})

    authored_keys = {_key(item) for item in corpus["expectedRegistry"]}
    observed_emission_keys = {_key(item) for item in actual_sites}
    unregistered = sorted((_key_text(item) for item in observed_emission_keys - set(actual_registry)), key=str)
    missing_expected_registry = sorted((_key_text(item) for item in authored_keys - set(actual_registry)), key=str)
    unexpected_registry = sorted((_key_text(item) for item in set(actual_registry) - authored_keys), key=str)
    unused_entries = sorted((_key_text(item) for item in set(actual_registry) - observed_emission_keys), key=str)

    readonly_drift: list[dict[str, Any]] = []
    readonly_by_key = {_key(item): item for item in corpus.get("readOnlyExpectations", []) if isinstance(item, dict)}
    for current_key in set(actual_registry) - observed_emission_keys:
        actual_entry = actual_registry[current_key]
        authored_reason = readonly_by_key.get(current_key, {}).get("reason")
        if not isinstance(actual_entry.get("readOnly"), bool) or not isinstance(actual_entry.get("readOnlyReason"), str) or not actual_entry.get("readOnlyReason"):
            readonly_drift.append({
                "key": _key_text(current_key),
                "authoredReason": authored_reason,
                "actualReadOnly": actual_entry.get("readOnly"),
                "actualReadOnlyReason": actual_entry.get("readOnlyReason"),
            })

    target_contract_missing: list[dict[str, Any]] = []
    schema_digest_missing: list[str] = []
    target_policy_mismatches: list[dict[str, Any]] = []
    authored_by_key = {_key(item): item for item in corpus["expectedRegistry"]}
    for current_key, actual_entry in actual_registry.items():
        authored = authored_by_key.get(current_key)
        if authored is None:
            continue
        if not isinstance(actual_entry.get("targetCollections"), list) or not actual_entry.get("targetCollections"):
            target_contract_missing.append({"key": _key_text(current_key), "missing": "targetCollections"})
        if not isinstance(actual_entry.get("schemaDigest"), str) or not re.fullmatch(r"[0-9a-f]{64}", str(actual_entry.get("schemaDigest", ""))):
            schema_digest_missing.append(_key_text(current_key))
        if actual_entry.get("targetKinds") != authored.get("targetKinds"):
            target_policy_mismatches.append({
                "key": _key_text(current_key),
                "expected": authored.get("targetKinds"),
                "actual": actual_entry.get("targetKinds"),
            })

    typed_constructor_policy = {
        "required": True,
        "observedViolationCount": len(constructor_violations),
        "violations": constructor_violations,
        "helperImportsUsedForExpected": False,
    }
    return {
        "authoredSiteCount": len(authored_sites),
        "observedSiteCount": len(actual_sites),
        "authoredRegisteredTypeCount": len(authored_keys),
        "observedRegisteredTypeCount": len(actual_registry),
        "observedEmissionTypeCount": len(observed_emission_keys),
        "missingAuthoredSites": missing_fingerprints,
        "unexpectedObservedSites": unexpected_fingerprints,
        "emissionSelectorLineDrift": line_drift,
        "siteMismatches": site_mismatches,
        "unregisteredEmissionKeys": unregistered,
        "missingExpectedRegistryKeys": missing_expected_registry,
        "unexpectedRegistryKeys": unexpected_registry,
        "unusedRegistryEntries": unused_entries,
        "unusedReadOnlyDrift": readonly_drift,
        "targetContractMissing": target_contract_missing,
        "targetPolicyMismatches": target_policy_mismatches,
        "schemaDigestMissing": schema_digest_missing,
        "typedConstructorPolicy": typed_constructor_policy,
        "observedSites": actual_sites,
        "runtimeEmission": _runtime_emission_evidence(corpus),
    }


def _payload_subset(actual: Any, expected: Any, path: str = "") -> list[str]:
    """Compare authored payload facts without manufacturing expected values."""

    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [path or "/"]
        failures: list[str] = []
        for key, expected_value in expected.items():
            child_path = f"{path}/{key}" if path else f"/{key}"
            if key not in actual:
                failures.append(child_path)
            else:
                failures.extend(_payload_subset(actual[key], expected_value, child_path))
        return failures
    if isinstance(expected, list):
        return [] if actual == expected else [path or "/"]
    return [] if actual == expected else [path or "/"]


def _forbidden_keys(value: Any, forbidden: set[str], path: str = "") -> list[str]:
    if isinstance(value, dict):
        failures: list[str] = []
        for key, child in value.items():
            child_path = f"{path}/{key}" if path else f"/{key}"
            if key in forbidden:
                failures.append(child_path)
            failures.extend(_forbidden_keys(child, forbidden, child_path))
        return failures
    if isinstance(value, list):
        failures = []
        for index, child in enumerate(value):
            failures.extend(_forbidden_keys(child, forbidden, f"{path}/{index}" if path else f"/{index}"))
        return failures
    return []


def _runtime_emission_evidence(corpus: dict[str, Any]) -> dict[str, Any]:
    """Exercise authored Markdown/PDF inputs and prove each listed site emits."""

    cases = corpus.get("runtimeEmissionCases", [])
    if not isinstance(cases, list) or not cases:
        return {"status": "failed", "caseCount": 0, "passedCaseCount": 0, "failures": [{"reason": "runtime emission corpus is empty"}], "cases": []}
    results: list[dict[str, Any]] = []
    scratch = ROOT / "e2e" / ".run" / f"qualification-issue-97-runtime-{uuid.uuid4().hex[:10]}"
    scratch.mkdir(parents=True, exist_ok=True)
    for case in cases:
        case_id = str(case.get("caseId", "<missing>"))
        format_name = str(case.get("format", ""))
        suffix = ".pdf" if format_name == "pdf" else ".md" if format_name == "markdown" else ".bin"
        input_path = scratch / f"{case_id}{suffix}"
        output_path = scratch / f"{case_id}.json"
        evidence_path = scratch / f"{case_id}.evidence.json"
        encoding = "latin-1" if format_name == "pdf" else "utf-8"
        row: dict[str, Any] = {"caseId": case_id, "format": format_name, "status": "failed", "failures": []}
        try:
            input_path.write_bytes(str(case.get("source", "")).encode(encoding))
            command = [
                sys.executable,
                str(ROOT / "tools" / "convert_document.py"),
                "convert",
                str(input_path),
                "--format",
                format_name,
                "--out",
                str(output_path),
                "--evidence",
                str(evidence_path),
            ]
            completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=30)
            row["returnCode"] = completed.returncode
            if not output_path.is_file():
                row["failures"].append("conversion did not produce an IR output")
                results.append(row)
                continue
            document = _read_json(output_path)
            extensions = [item for item in document.get("extensions", []) if isinstance(item, dict)]
            row["observedExtensionTypes"] = sorted(str(item.get("type", "")) for item in extensions)
            by_type: dict[str, list[dict[str, Any]]] = {}
            for extension in extensions:
                by_type.setdefault(str(extension.get("type", "")), []).append(extension)
            for expected in case.get("expectedExtensions", []):
                extension_type = str(expected.get("type", ""))
                candidates = by_type.get(extension_type, [])
                if not candidates:
                    row["failures"].append(f"missing runtime extension type: {extension_type}")
                    continue
                payload_expected = expected.get("payload", {})
                matching = [item for item in candidates if not _payload_subset(item.get("payload"), payload_expected)]
                if not matching:
                    row["failures"].append(f"payload mismatch for runtime extension type: {extension_type}")
            for forbidden_type in case.get("forbiddenExtensionTypes", []):
                if by_type.get(str(forbidden_type)):
                    row["failures"].append(f"forbidden runtime extension type was emitted: {forbidden_type}")
            diagnostic_codes = {str(item.get("code")) for item in document.get("diagnostics", []) if isinstance(item, dict)}
            row["observedDiagnosticCodes"] = sorted(diagnostic_codes)
            for code in case.get("expectedDiagnosticCodes", []):
                if code not in diagnostic_codes:
                    row["failures"].append(f"missing expected diagnostic: {code}")
            diagnostic_by_code = {
                str(item.get("code")): item
                for item in document.get("diagnostics", [])
                if isinstance(item, dict) and isinstance(item.get("code"), str)
            }
            for link in case.get("diagnosticLinks", []):
                extension_type = str(link.get("extensionType", ""))
                extension = next((item for item in extensions if item.get("type") == extension_type), None)
                if extension is None:
                    continue
                target_id = extension.get("targetId")
                for code in link.get("codes", []):
                    diagnostic = diagnostic_by_code.get(str(code))
                    linked = bool(diagnostic and diagnostic.get("targetId") == target_id)
                    if not linked:
                        linked = any(
                            feature.get("targetId") == target_id and str(diagnostic.get("diagnosticId")) in set(feature.get("diagnosticIds", []))
                            for feature in document.get("conversion", {}).get("features", [])
                            if isinstance(feature, dict) and diagnostic is not None
                        )
                    if not linked:
                        row["failures"].append(f"diagnostic is not linked to extension target: {extension_type}:{code}")
            forbidden = {
                "sourceBytes",
                "sourceByteStore",
                "contentAddressedSource",
                "rawStringBytesHex",
                "rawBytesHex",
            }
            forbidden.update(str(item) for item in case.get("forbiddenPayloadKeys", []))
            forbidden_paths = _forbidden_keys(document, forbidden)
            if forbidden_paths:
                row["failures"].append({"sourceBytesInIr": forbidden_paths})
            row["status"] = "passed" if not row["failures"] and completed.returncode == 0 else "failed"
        except Exception as exc:
            row["failures"].append(f"{type(exc).__name__}: {exc}")
        results.append(row)
    failures = [row for row in results if row.get("status") != "passed"]
    return {
        "status": "passed" if not failures and len(results) == len(cases) else "failed",
        "caseCount": len(results),
        "passedCaseCount": len(results) - len(failures),
        "failureCount": len(failures),
        "failures": failures,
        "cases": results,
    }


def _schema_evidence(corpus: dict[str, Any], registry: dict[str, Any], schema_root: dict[str, Any]) -> dict[str, Any]:
    actual_registry = _registry_key_map(registry)
    rows: list[dict[str, Any]] = []
    positive_failures: list[dict[str, Any]] = []
    semantic_failures: list[dict[str, Any]] = []
    path_failures: list[dict[str, Any]] = []
    runtime_failures: list[dict[str, Any]] = []
    cases_by_key = {_key(item["key"]): item for item in corpus["schemaCases"]}
    for authored_entry in corpus["expectedRegistry"]:
        current_key = _key(authored_entry)
        case = cases_by_key[current_key]
        actual_entry = actual_registry.get(current_key)
        row: dict[str, Any] = {"caseId": case["caseId"], "key": _key_text(current_key)}
        if actual_entry is None:
            row.update({"status": "failed", "error": "registry entry is missing"})
            path_failures.append(row)
            rows.append(row)
            continue
        try:
            fragment = _schema_fragment(actual_entry, schema_root)
            errors = _jsonschema_errors(case["payload"], fragment, schema_root)
            row["positiveErrors"] = errors
            row["positiveStatus"] = "passed" if not errors else "failed"
            if errors:
                positive_failures.append({"caseId": case["caseId"], "errors": errors})
            required = set(fragment.get("required", [])) if isinstance(fragment.get("required"), list) else set()
            semantic_required = set(authored_entry.get("semanticRequiredFields", []))
            missing_semantic = sorted(semantic_required - required)
            row["schemaRequiredFields"] = sorted(required)
            row["semanticRequiredFields"] = sorted(semantic_required)
            row["missingSemanticRequiredFields"] = missing_semantic
            if missing_semantic:
                semantic_failures.append({"caseId": case["caseId"], "fields": missing_semantic})
            row["schemaPath"] = actual_entry.get("schemaPath")
            row["schemaId"] = actual_entry.get("schemaId")
            row["schemaDigest"] = _sha256_file(ROOT / str(actual_entry["schemaPath"]).split("#", 1)[0])
        except Exception as exc:
            row.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            path_failures.append(row)

        try:
            extension = {
                "extensionId": f"extension-schema-{case['caseId']}",
                "targetId": "node-target",
                "namespace": current_key[0],
                "type": current_key[1],
                "schemaVersion": current_key[2],
                "schemaId": actual_entry.get("schemaId"),
                "payload": case["payload"],
                "criticality": actual_entry.get("criticality", "non-critical"),
            }
            runtime_result, runtime_error = _runtime_extension_result(
                extension,
                "partial",
                target_kind=str((actual_entry.get("targetKinds") or ["paragraph"])[0]),
                validate_full_document=False,
            )
            row["runtimePositiveResult"] = runtime_result
            row["runtimePositiveError"] = runtime_error
            if runtime_result != "known":
                runtime_failures.append({"caseId": case["caseId"], "result": runtime_result, "error": runtime_error})
        except Exception as exc:
            runtime_failures.append({"caseId": case["caseId"], "result": "setup-failed", "error": f"{type(exc).__name__}: {exc}"})
        rows.append(row)

    missing_schema_digests = [
        _key_text(_key(entry))
        for entry in registry.get("entries", [])
        if not isinstance(entry, dict) or not isinstance(entry.get("schemaDigest"), str)
    ]
    return {
        "authoredSchemaCaseCount": len(cases_by_key),
        "positiveFailures": positive_failures,
        "semanticRequiredFailures": semantic_failures,
        "schemaPathFailures": path_failures,
        "runtimePositiveFailures": runtime_failures,
        "missingSchemaDigestCount": len(missing_schema_digests),
        "missingSchemaDigests": missing_schema_digests,
        "cases": rows,
    }


def _collection_maps(document: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    collection_keys = {
        "parts": "partId",
        "surfaces": "surfaceId",
        "nodes": "nodeId",
        "texts": "textId",
        "tables": "tableId",
        "styles": "styleId",
        "layouts": "layoutId",
        "coordinateSpaces": "coordinateSpaceId",
        "geometries": "geometryId",
        "resources": "resourceId",
        "formulas": "formulaId",
        "fields": "fieldId",
        "annotations": "annotationId",
        "relations": "relationId",
        "orders": "orderId",
        "observations": "observationId",
        "extensions": "extensionId",
        "sourceMaps": "sourceMapId",
        "diagnostics": "diagnosticId",
    }
    return {
        collection: {
            str(item[key]): item
            for item in document.get(collection, [])
            if isinstance(item, dict) and isinstance(item.get(key), str)
        }
        for collection, key in collection_keys.items()
    }


def _reference_evidence(corpus: dict[str, Any]) -> dict[str, Any]:
    document = deepcopy(corpus["referenceDocument"])
    maps = _collection_maps(document)
    nodes = maps.get("nodes", {})
    extension = next((item for item in document.get("extensions", []) if isinstance(item, dict)), None)
    if extension is None:
        raise QualificationError("reference document has no extension")
    contract = corpus["referenceContracts"][0]
    envelope_target = extension.get("targetId")
    envelope_collection = next((name for name, values in maps.items() if envelope_target in values), None)
    envelope_kind = maps.get(envelope_collection or "", {}).get(str(envelope_target), {}).get("kind")
    payload_value = _pointer(extension.get("payload", {}), str(contract["payloadPointer"]))
    payload_collection = next((name for name, values in maps.items() if payload_value in values), None)
    payload_kind = maps.get(payload_collection or "", {}).get(str(payload_value), {}).get("kind")
    independent_rows = [{
        "extensionId": extension.get("extensionId"),
        "envelope": {
            "expectedCollection": contract["envelopeTarget"]["collection"],
            "actualCollection": envelope_collection,
            "expectedKinds": contract["envelopeTarget"]["kinds"],
            "actualKind": envelope_kind,
            "closed": envelope_collection == contract["envelopeTarget"]["collection"] and envelope_kind in contract["envelopeTarget"]["kinds"],
        },
        "payloadReference": {
            "pointer": contract["payloadPointer"],
            "value": payload_value,
            "expectedCollection": contract["payloadTarget"]["collection"],
            "actualCollection": payload_collection,
            "expectedKinds": contract["payloadTarget"]["kinds"],
            "actualKind": payload_kind,
            "closed": payload_collection == contract["payloadTarget"]["collection"] and payload_kind in contract["payloadTarget"]["kinds"],
        },
    }]
    independent_failures = [row for row in independent_rows if not row["envelope"]["closed"] or not row["payloadReference"]["closed"]]

    runtime = _runtime_imports()
    runtime_baseline: dict[str, Any]
    try:
        runtime["validate_document"](document)
        runtime_baseline = {"result": "accepted", "error": None}
    except Exception as exc:
        runtime_baseline = {"result": "rejected", "error": f"{type(exc).__name__}: {exc}"}

    mutation_runtime: list[dict[str, Any]] = []
    for case_id, path, expected_rejection in (
        ("wrong-target-kind", "/extensions/0/targetId", True),
        ("wrong-target-collection", "/extensions/0/targetId", True),
        ("dangling-payload-reference", "/extensions/0/payload/sourceId", True),
    ):
        mutated = deepcopy(document)
        if case_id == "wrong-target-kind":
            mutated["extensions"][0]["targetId"] = "node-target"
        elif case_id == "wrong-target-collection":
            mutated["extensions"][0]["targetId"] = "part-document"
        else:
            mutated["extensions"][0]["payload"]["sourceId"] = "node-missing"
        try:
            runtime["validate_document"](mutated)
            actual = "accepted"
            error = None
        except Exception as exc:
            actual = "rejected"
            error = f"{type(exc).__name__}: {exc}"
        mutation_runtime.append({"caseId": case_id, "expectedRejection": expected_rejection, "actual": actual, "error": error})

    source = RUNTIME_PATH.read_text(encoding="utf-8")
    reference_runtime_hooks = {
        "checksExtensionEnvelopeTarget": "targetKinds" in source and "targetId" in source,
        "checksPayloadReference": "payload" in source and "sourceId" in source and "target_kinds" in source,
        "usesGlobalReferenceRegistry": "reference-registry" in source or "reference_registry" in source,
    }
    return {
        "authoredReferenceContractCount": len(corpus["referenceContracts"]),
        "independentRows": independent_rows,
        "independentFailures": independent_failures,
        "runtimeBaseline": runtime_baseline,
        "runtimeMutationResults": mutation_runtime,
        "runtimeReferenceHooks": reference_runtime_hooks,
        "nodeCount": len(nodes),
    }


def _policy_evidence(corpus: dict[str, Any], registry: dict[str, Any]) -> dict[str, Any]:
    actual_registry = _registry_key_map(registry)
    rows: list[dict[str, Any]] = []
    runtime_failures: list[dict[str, Any]] = []
    independent_failures: list[dict[str, Any]] = []
    schema_cases = {_key(item["key"]): item for item in corpus["schemaCases"]}
    for case in corpus["policyCases"]:
        case_key = _key(case)
        known_entry = actual_registry.get(case_key)
        is_known = known_entry is not None
        if not is_known and case.get("expected") == "known":
            independent_outcome = "reject"
        elif is_known and case.get("criticality") != known_entry.get("criticality"):
            independent_outcome = "reject"
        elif not is_known and case.get("criticality") == "critical":
            independent_outcome = "reject"
        elif not is_known and case.get("documentStatus") in {"complete", "complete-with-warnings"}:
            independent_outcome = "reject"
        elif not is_known and case.get("documentStatus") == "partial" and case.get("diagnostic"):
            independent_outcome = "opaque"
        elif not is_known:
            independent_outcome = "reject"
        else:
            independent_outcome = "known"
        if independent_outcome != case.get("expected"):
            independent_failures.append({"caseId": case["caseId"], "expected": case.get("expected"), "independent": independent_outcome})

        payload = {"opaque": case.get("rawPayload", {"value": "case"})}
        if case_key in schema_cases:
            payload = schema_cases[case_key]["payload"]
        extension = {
            "extensionId": f"extension-policy-{case['caseId']}",
            "targetId": "node-target",
            "namespace": case["namespace"],
            "type": case["type"],
            "schemaVersion": case["schemaVersion"],
            "schemaId": case["schemaId"],
            "payload": payload,
            "criticality": case["criticality"],
        }
        runtime_result, runtime_error = _runtime_extension_result(extension, str(case["documentStatus"]), diagnostic=bool(case.get("diagnostic")))
        row = {
            "caseId": case["caseId"],
            "expected": case.get("expected"),
            "independent": independent_outcome,
            "runtime": runtime_result,
            "runtimeError": runtime_error,
            "registryKnown": is_known,
        }
        rows.append(row)
        if (case.get("expected") == "reject" and runtime_result != "rejected") or (case.get("expected") == "known" and runtime_result != "known") or (case.get("expected") == "opaque" and runtime_result != "opaque"):
            runtime_failures.append(row)
    return {
        "authoredPolicyCaseCount": len(corpus["policyCases"]),
        "cases": rows,
        "independentFailures": independent_failures,
        "runtimeFailures": runtime_failures,
        "unknownPolicyObserved": registry.get("unknownPolicy"),
        "criticalityPolicyPresent": all(isinstance(item.get("criticality"), str) for item in registry.get("entries", []) if isinstance(item, dict)),
    }


def _semver(value: Any) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    match = SEMVER_RE.fullmatch(value)
    return tuple(int(item) for item in match.groups()) if match else None


def _version_evidence(corpus: dict[str, Any], registry: dict[str, Any]) -> dict[str, Any]:
    actual_registry = _registry_key_map(registry)
    matrix = corpus["versionMatrix"]
    cases: list[dict[str, Any]] = []
    runtime_failures: list[dict[str, Any]] = []
    matrix_failures: list[dict[str, Any]] = []
    schema_cases = {_key(item["key"]): item for item in corpus["schemaCases"]}
    for authored_entry in corpus["expectedRegistry"]:
        current_key = _key(authored_entry)
        actual_entry = actual_registry.get(current_key)
        case_payload = schema_cases[current_key]["payload"]
        for label in ("exact", "patch", "minorAdditive", "minorBreaking", "major"):
            descriptor = matrix[label]
            version = str(descriptor["version"])
            expected = str(descriptor["outcome"])
            extension = {
                "extensionId": f"extension-version-{authored_entry['type']}-{label}",
                "targetId": "node-target",
                "namespace": current_key[0],
                "type": current_key[1],
                "schemaVersion": version,
                "schemaId": actual_entry.get("schemaId") if actual_entry else authored_entry.get("schemaId"),
                "payload": case_payload,
                "criticality": "non-critical",
            }
            if label == "exact":
                independent = "known"
            elif label == "patch":
                independent = "accept-with-capability"
            elif label == "minorAdditive":
                independent = "accept-with-capability"
            elif label == "minorBreaking":
                independent = "reject-or-migrate"
            else:
                independent = "migration-required"
            runtime_result, runtime_error = _runtime_extension_result(
                extension,
                "partial",
                target_kind=str((actual_entry.get("targetKinds") if actual_entry else authored_entry.get("targetKinds") or ["paragraph"])[0]),
                validate_full_document=False,
            )
            row = {
                "key": _key_text(current_key),
                "case": label,
                "version": version,
                "expected": expected,
                "independent": independent,
                "runtime": runtime_result,
                "runtimeError": runtime_error,
            }
            cases.append(row)
            if independent == "known" and runtime_result != "known":
                runtime_failures.append(row)
            if independent != "known" and runtime_result == "known":
                runtime_failures.append(row)
            if independent != "known" and runtime_result == "opaque":
                row["runtimePolicyGap"] = True

        if actual_entry is None:
            matrix_failures.append({"key": _key_text(current_key), "error": "registry entry missing"})

    required_metadata = (
        "schemaDigest",
        "versionRange",
        "consumerCapabilities",
        "targetCollections",
        "payloadReferences",
        "query",
        "canonicalization",
        "migration",
        "downgrade",
        "unknownFieldPolicy",
    )
    missing_metadata: list[dict[str, Any]] = []
    for entry in registry.get("entries", []):
        if not isinstance(entry, dict):
            continue
        missing = [field for field in required_metadata if field not in entry]
        if missing:
            missing_metadata.append({"key": _key_text(entry), "missing": missing})
    compatibility = registry.get("compatibility")
    executable_compatibility = isinstance(compatibility, dict) and all(isinstance(compatibility.get(name), dict) for name in ("patch", "minor", "major"))
    return {
        "authoredVersionCaseCount": len(cases),
        "cases": cases,
        "runtimeFailures": runtime_failures,
        "matrixFailures": matrix_failures,
        "missingExecutableMetadata": missing_metadata,
        "compatibilityObject": compatibility,
        "executableCompatibility": executable_compatibility,
        "semverCurrentEntries": sum(1 for entry in registry.get("entries", []) if _semver(entry.get("schemaVersion")) is not None) if isinstance(registry.get("entries"), list) else 0,
    }


def _migration_evidence(corpus: dict[str, Any]) -> dict[str, Any]:
    runtime = _runtime_imports()
    document = deepcopy(corpus["roundTripDocument"])
    query_result: dict[str, Any]
    canonical_result: dict[str, Any]
    migration_result: dict[str, Any]
    try:
        extensions = runtime["find_extensions"](document)
        queried_payload = runtime["get_field"](document, "extensions", "extension-opaque", "/payload")
        field_coverage = runtime["query_field_coverage"](document)
        runtime["rebuild_index"](document)
        expected_extension = next(item for item in corpus["roundTripDocument"]["extensions"] if item.get("extensionId") == "extension-opaque")
        query_result = {
            "status": "passed" if extensions and extensions[0] == expected_extension and queried_payload == expected_extension["payload"] else "failed",
            "extensionCount": len(extensions),
            "payloadPreserved": queried_payload == expected_extension["payload"],
            "fieldCoverageStatus": field_coverage.get("status"),
        }
    except Exception as exc:
        query_result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}

    try:
        before = runtime["canonical_digest"](document, "source-map-excluded")
        migrated, receipt = runtime["migrate_extensions"](document, "9.0.0")
        after = runtime["canonical_digest"](migrated, "source-map-excluded")
        canonical_result = {
            "status": "passed" if before == after else "failed",
            "beforeDigest": before,
            "afterDigest": after,
            "digestStable": before == after,
            "receipt": receipt,
            "opaquePayloadStillPresent": any(item.get("extensionId") == "extension-opaque" for item in migrated.get("extensions", [])),
        }
    except Exception as exc:
        canonical_result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}

    source = CANONICALIZER_PATH.read_text(encoding="utf-8")
    runtime_source = RUNTIME_PATH.read_text(encoding="utf-8")
    extension_migration_hooks = {
        "extensionVersionDispatch": "schemaVersion" in source and "extensions" in source and "target_version" in source,
        "receiptFields": all(token in source for token in ("sourceVersion", "targetVersion", "ruleId", "loss")),
        "downgradePolicy": "downgrade" in runtime_source or "downgrade" in source,
    }
    migration_rows: list[dict[str, Any]] = []
    migration_failures: list[dict[str, Any]] = []
    schema_cases = {_key(item["key"]): item for item in corpus["schemaCases"]}
    for case in corpus["migrationCases"]:
        case_id = case["caseId"]
        if case_id == "minor-additive-migration-success":
            key = ("urn:fdir:format:docx", "numbering", "1.0.0")
            extension = {
                "extensionId": "migration-numbering",
                "targetId": "node-document",
                "namespace": key[0],
                "type": key[1],
                "schemaVersion": key[2],
                "schemaId": "urn:fdir:schema:docx-numbering",
                "payload": deepcopy(schema_cases[key]["payload"]),
                "criticality": "non-critical",
            }
        elif case_id == "major-migration-explicit-loss":
            key = ("urn:fdir:format:docx", "revision", "1.0.0")
            payload = deepcopy(schema_cases[key]["payload"])
            payload["legacyRange"] = "legacy-range"
            extension = {
                "extensionId": "migration-revision",
                "targetId": "node-document",
                "namespace": key[0],
                "type": key[1],
                "schemaVersion": key[2],
                "schemaId": "urn:fdir:schema:docx-revision",
                "payload": payload,
                "criticality": "non-critical",
            }
        elif case_id == "migration-failure-receipt":
            extension = {
                "extensionId": "migration-downgrade",
                "targetId": "node-document",
                "namespace": "urn:fdir:format:docx",
                "type": "revision",
                "schemaVersion": "2.0.0",
                "schemaId": "urn:fdir:schema:docx-revision",
                "payload": {"kind": "insert", "author": "author", "revisionId": "r1", "range": "legacy"},
                "criticality": "non-critical",
            }
        elif case_id == "opaque-payload-migration-retained":
            extension = deepcopy(document["extensions"][0])
        else:
            migration_failures.append({"caseId": case_id, "reason": "qualification has no authored migration fixture"})
            continue
        migration_document = deepcopy(document)
        migration_document["extensions"] = [extension]
        row = {
            "caseId": case_id,
            "expectedReceipt": {
                "sourceVersion": case["sourceVersion"],
                "targetVersion": case["targetVersion"],
                "ruleId": case["ruleId"],
                "status": case["status"],
                "losses": case["losses"],
                "droppedFields": case["droppedFields"],
            },
            "receiptRequired": case.get("receiptRequired") is True,
            "runtimeReceipt": [],
            "status": "failed",
        }
        try:
            _migrated, receipts = runtime["migrate_extensions"](migration_document, str(case["targetVersion"]))
            row["runtimeReceipt"] = receipts
            receipt = receipts[0] if receipts else {}
            losses = receipt.get("losses", receipt.get("loss", []))
            matches = (
                receipt.get("sourceVersion") == case["sourceVersion"]
                and receipt.get("targetVersion") == case["targetVersion"]
                and receipt.get("ruleId") == case["ruleId"]
                and receipt.get("status") == case["status"]
                and losses == case["losses"]
                and receipt.get("droppedFields") == case["droppedFields"]
            )
            row["status"] = "passed" if matches else "failed"
            if not matches:
                migration_failures.append({"caseId": case_id, "reason": "runtime migration receipt does not match authored case", "receipt": receipt})
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            migration_failures.append({"caseId": case_id, "reason": row["error"]})
        migration_rows.append(row)
    return {
        "authoredMigrationCaseCount": len(corpus["migrationCases"]),
        "migrationCases": migration_rows,
        "migrationFailures": migration_failures,
        "extensionMigrationHooks": extension_migration_hooks,
        "queryRoundTrip": query_result,
        "canonicalRoundTrip": canonical_result,
        "roundTripAuthoredFieldCount": 2,
    }


def _negative_baseline(item: dict[str, Any]) -> dict[str, Any]:
    baseline = item.get("baseline")
    if not isinstance(baseline, dict):
        raise QualificationError(f"negative baseline is not an object: {item.get('caseId')}")
    return deepcopy(baseline)


def _negative_detected(item: dict[str, Any], mutated: dict[str, Any], corpus: dict[str, Any]) -> tuple[bool, str]:
    category = str(item.get("category"))
    if category == "unregistered-emission":
        actual_keys = {
            _key(entry)
            for entry in corpus["expectedRegistry"]
        }
        detected = _key(mutated) not in actual_keys
        return detected, "emission key is outside the authored registry" if detected else "emission key still appears registered"
    if category == "unused-read-only-drift":
        detected = mutated.get("readOnly") is not True or not isinstance(mutated.get("readOnlyReason"), str) or not mutated.get("readOnlyReason")
        return detected, "unused entry has no explicit read-only reason" if detected else "read-only reason remains complete"
    if category == "criticality-mismatch":
        registry_entry = next((entry for entry in corpus["expectedRegistry"] if _key(entry) == _key(mutated)), None)
        detected = registry_entry is not None and mutated.get("criticality") != registry_entry.get("criticality")
        return detected, "instance criticality differs from registry policy" if detected else "criticality matches policy"
    if category == "wrong-target-kind":
        detected = mutated.get("targetKind") not in set(mutated.get("allowedKinds", []))
        return detected, "target kind is outside the owner target-kind contract" if detected else "target kind remains allowed"
    if category == "wrong-target-collection":
        detected = mutated.get("targetCollection") not in set(mutated.get("allowedCollections", []))
        return detected, "target collection is outside the owner collection contract" if detected else "target collection remains allowed"
    if category == "dangling-payload-reference":
        detected = mutated.get("referenceValue") not in set(mutated.get("availableIds", []))
        return detected, "payload reference does not resolve in the global id graph" if detected else "payload reference still resolves"
    if category == "unknown-critical-policy":
        detected = mutated.get("known") is False and mutated.get("criticality") == "critical"
        return detected, "unknown critical extension must reject or become partial" if detected else "unknown critical policy mutation was not made"
    if category == "unknown-non-critical-policy":
        detected = mutated.get("known") is False and mutated.get("criticality") == "non-critical" and mutated.get("documentStatus") in {"complete", "complete-with-warnings"}
        return detected, "unknown non-critical extension is forbidden in a complete claim" if detected else "unknown non-critical complete policy was not made"
    if category == "unknown-field":
        allowed = set(mutated.get("schemaProperties", []))
        payload = mutated.get("payload", {})
        unknown = set(payload) - allowed if isinstance(payload, dict) else {"payload-not-object"}
        detected = bool(unknown)
        return detected, "closed payload schema detects unknown field" if detected else "payload has no unknown field"
    if category == "major-version":
        current = _semver(mutated.get("currentVersion"))
        version = _semver(mutated.get("version"))
        detected = current is not None and version is not None and version[0] != current[0] and mutated.get("outcome") == "known"
        return detected, "major version cannot use the current schema without migration" if detected else "major compatibility mutation was not detected"
    if category == "minor-version":
        detected = mutated.get("change") == "required-field-or-meaning-change" and mutated.get("outcome") == "accept-with-capability"
        return detected, "breaking minor change cannot be treated as additive" if detected else "minor compatibility mutation was not detected"
    if category == "migration-loss":
        detected = mutated.get("status") == "preserved" and bool(mutated.get("losses"))
        return detected, "loss-bearing migration cannot claim preservation" if detected else "loss accounting remains coherent"
    if category == "migration-receipt-drop":
        detected = not all(isinstance(mutated.get(field), str) and mutated.get(field) for field in ("receiptId", "sourceVersion", "targetVersion", "ruleId"))
        return detected, "migration receipt identity is incomplete" if detected else "migration receipt identity remains complete"
    if category == "migration-dropped-fields-drop":
        detected = mutated.get("status") == "loss-declared" and not isinstance(mutated.get("droppedFields"), list)
        return detected, "loss receipt lacks dropped-field accounting" if detected else "dropped-field accounting remains present"
    if category in {"round-trip-loss", "query-canonical-round-trip-loss"}:
        required = item.get("baseline", {}).get("requiredPointers")
        if isinstance(required, list):
            detected = any(_safe_pointer_missing(mutated, pointer) for pointer in required)
        else:
            baseline_payload = item.get("baseline", {}).get("payload", {})
            current_payload = mutated.get("payload", {})
            detected = current_payload != baseline_payload
        return detected, "round-trip oracle observes a changed or missing authored payload fact" if detected else "round-trip payload remains equal"
    if category == "typed-constructor-drift":
        detected = bool(mutated.get("directDictEmission"))
        return detected, "direct dictionary emission bypasses the typed constructor" if detected else "constructor remains typed"
    return False, f"no independent detector for mutation category {category}"


def _safe_pointer_missing(value: Any, path: str) -> bool:
    try:
        _pointer(value, path)
    except (KeyError, IndexError, TypeError):
        return True
    return False


def _run_negative_mutations(corpus: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in corpus["negativeMutations"]:
        baseline = _negative_baseline(item)
        mutation = item["mutation"]
        try:
            mutated = _apply_mutation(baseline, mutation)
            detected, reason = _negative_detected(item, mutated, corpus)
            error = None
        except Exception as exc:
            mutated = None
            detected = False
            reason = "mutation execution failed"
            error = f"{type(exc).__name__}: {exc}"
        mutation_evidence = {
            "baseline": deepcopy(baseline),
            "mutated": deepcopy(mutated),
            "mutation": deepcopy(mutation),
            "reason": reason,
            "error": error,
        }
        producer_expected = {
            "mutationKind": item.get("category"),
            "oracleMutationRequired": True,
        }
        producer_actual = {
            "mutationKind": item.get("category"),
            "oracleMutationDetected": detected,
            "mutationEvidence": mutation_evidence,
        }
        if not detected:
            # A surviving mutation must also fail the typed producer evaluator;
            # retain the full mutated value in oracleEvidence below.
            producer_actual = deepcopy(producer_expected)
        results.append({
            "caseId": item["caseId"],
            "category": item.get("category"),
            "defect": item.get("defect"),
            "mutation": mutation,
            "oracleMutationDetected": detected,
            "reason": reason,
            "error": error,
            "producerExpected": producer_expected,
            "producerActual": producer_actual,
            "status": "passed" if detected else "failed",
        })
    return results


def _safe_pointer(value: Any, path: str) -> Any:
    try:
        return _pointer(value, path)
    except (KeyError, IndexError, TypeError):
        return None


def _assertion(assertion_id: str, expected: Any, actual: Any, *, detail: Any = None) -> dict[str, Any]:
    result = {
        "assertionId": assertion_id,
        "expected": expected,
        "actual": actual,
        "status": "passed" if expected == actual else "failed",
    }
    if detail is not None:
        result["detail"] = detail
    return result


def _fingerprint(site: dict[str, Any]) -> tuple[str, str, str, str, str, str, str]:
    """Stable emission selector; source line is evidence, not identity."""

    return (
        str(site.get("path", "")),
        str(site.get("kind", "")),
        str(site.get("namespace", "")),
        str(site.get("type", "")),
        str(site.get("schemaVersion", "")),
        str(site.get("schemaId", "")),
        str(site.get("criticality", "")),
    )


def _group_fingerprints(sites: list[dict[str, Any]]) -> dict[tuple[str, str, str, str, str, str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str, str, str, str, str, str], list[dict[str, Any]]] = {}
    for site in sites:
        grouped.setdefault(_fingerprint(site), []).append(site)
    for values in grouped.values():
        values.sort(key=lambda item: int(item.get("line", 0)))
    return grouped


def _stage(function: Any) -> dict[str, Any]:
    try:
        value = function()
        return value if isinstance(value, dict) else {"value": value}
    except Exception as exc:
        return {"setupError": f"{type(exc).__name__}: {exc}"}


def _stage_error_assertion(stage: dict[str, Any]) -> dict[str, Any]:
    return _assertion("qualification-stage-available", True, "setupError" not in stage, detail=stage.get("setupError"))


def _common_assertions(source_sha: str | None, corpus: dict[str, Any], negative_results: list[dict[str, Any]], stage: dict[str, Any]) -> list[dict[str, Any]]:
    negative_failures = sum(1 for item in negative_results if item.get("status") != "passed")
    return [
        _assertion("source-sha-format", True, bool(re.fullmatch(r"[0-9a-f]{40}", source_sha or ""))),
        _assertion("authored-independent-oracle", True, corpus.get("oracle", {}).get("expectedValuesAreRuntimeIndependent") is True),
        _assertion("adapter-helper-not-used-for-expected", False, corpus.get("oracle", {}).get("adapterHelpersUsedForExpected")),
        _assertion("negative-mutations-detected", 0, negative_failures, detail=[item for item in negative_results if item.get("status") != "passed"]),
        _assertion("whole-issue-completion-claim", False, False),
        _stage_error_assertion(stage),
    ]


def _producer_case_id(*parts: Any) -> str:
    value = "-".join(str(part) for part in parts)
    value = re.sub(r"[^A-Za-z0-9._:-]+", "-", value).strip("-") or "case"
    return value[:120]


def _producer_input_paths(corpus_path: Path) -> list[Path]:
    return [
        Path(corpus_path),
        REGISTRY_PATH,
        EXTENSION_SCHEMA_PATH,
        RUNTIME_PATH,
        ROOT / "tools" / "qualification_issue97.py",
        ROOT / "tools" / "test_qualification_issue97.py",
        ROOT / "tools" / "validate_qualification_contract.py",
    ]


def _producer_rows(
    corpus: dict[str, Any] | None,
    reports: dict[str, dict[str, Any]],
    negative_results: list[dict[str, Any]],
    *,
    setup_error: str | None = None,
) -> list[dict[str, Any]]:
    del corpus
    rows: list[dict[str, Any]] = []
    for report_kind, report in reports.items():
        for assertion in report.get("assertions", []):
            assertion_id = str(assertion.get("assertionId", ""))
            if not assertion_id:
                continue
            expected = {
                "reportKind": report_kind,
                "assertionId": assertion_id,
                "value": deepcopy(assertion.get("expected")),
                "status": "passed",
            }
            actual = {
                "reportKind": report_kind,
                "assertionId": assertion_id,
                "value": deepcopy(assertion.get("actual")),
                "status": assertion.get("status"),
            }
            rows.append({
                "caseId": _producer_case_id("positive", report_kind, assertion_id),
                "classification": "positive",
                "evaluatorType": EXTENSION_EVALUATOR,
                "input": {"reportKind": report_kind, "assertionId": assertion_id},
                "expected": expected,
                "actual": actual,
                "result": "passed" if expected == actual else "failed",
                "target": {"reportKind": report_kind, "assertionId": assertion_id, "dimension": "extension-registry"},
                "diagnostic": {"code": "ISSUE-97-EXTENSION-CLOSURE", "message": "authored extension registry, schema, reference, version, and migration expectations are evaluated independently"},
                "oracleEvidence": {"identity": "authored-independent-extension-oracle", "expectedValuesAreRuntimeIndependent": True},
            })

    for result in negative_results:
        case_id = str(result.get("caseId", ""))
        if not case_id:
            continue
        detected = result.get("oracleMutationDetected") is True
        expected = deepcopy(result.get("producerExpected", {"oracleMutationRequired": True}))
        actual = deepcopy(result.get("producerActual", expected if not detected else {"oracleMutationDetected": detected}))
        rows.append({
            "caseId": _producer_case_id("mutation", case_id),
            "classification": "mutation",
            "evaluatorType": MUTATION_EVALUATOR,
            "input": {"mutationCaseId": case_id, "category": result.get("category")},
            "expected": expected,
            "actual": actual,
            "result": "passed" if detected else "failed",
            "target": {"mutationCaseId": case_id, "category": result.get("category"), "oracleMutationDetected": detected},
            "diagnostic": {"code": "ISSUE-97-MUTATION", "message": "authored extension-registry mutation must be detected by the independent oracle"},
            "oracleEvidence": {
                "oracleMutationDetected": detected,
                "baseline": deepcopy(result.get("producerExpected")),
                "mutated": deepcopy(result.get("producerActual")),
                "evidence": deepcopy(result.get("mutation")),
                "reason": result.get("reason"),
                "error": result.get("error"),
            },
        })

    if setup_error or not rows:
        message = setup_error or "no issue #97 producer cases were generated"
        rows = [
            {
                "caseId": "setup-positive",
                "classification": "positive",
                "evaluatorType": EXTENSION_EVALUATOR,
                "input": {"setup": "issue-97"},
                "expected": {"setup": "available"},
                "actual": {"setup": "unavailable", "error": message},
                "result": "failed",
                "target": {"phase": "qualification-setup"},
                "diagnostic": {"code": "ISSUE-97-SETUP", "message": message},
                "oracleEvidence": {"setupError": message},
            },
            {
                "caseId": "setup-mutation",
                "classification": "mutation",
                "evaluatorType": MUTATION_EVALUATOR,
                "input": {"setup": "issue-97"},
                "expected": {"mutationDetected": True},
                "actual": {"mutationDetected": True},
                "result": "failed",
                "target": {"phase": "qualification-setup", "oracleMutationDetected": False},
                "diagnostic": {"code": "ISSUE-97-SETUP", "message": message},
                "oracleEvidence": {"setupError": message},
            },
        ]
    return rows


def _write_producer_report(
    out_dir: Path,
    reports: dict[str, dict[str, Any]],
    corpus_path: Path,
    source_sha: str | None,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    return write_producer_report(
        out_dir=out_dir,
        reports=reports,
        report_names=REPORT_NAMES,
        artifact_report_names=PRODUCER_ARTIFACT_REPORT_NAMES,
        issue_number=97,
        evidence_id=EVIDENCE_ID,
        requirement_id=REQUIREMENT_ID,
        source_sha=source_sha,
        input_paths=_producer_input_paths(corpus_path),
        producer_id="issue-97-extension-registry-runner",
        authority_id="issue-97-authored-extension-oracle",
        producer_component_path=Path(__file__),
        authority_component_path=Path(corpus_path),
        evaluator_component_path=ROOT / "tools" / "validate_qualification_contract.py",
        shared_component_paths=(ROOT / "tools" / "qualification_evidence.py",),
        rows=rows,
    )


def _report(
    report_kind: str,
    source_sha: str | None,
    corpus: dict[str, Any],
    corpus_sha256: str | None,
    authored_case_count: int,
    positive_case_count: int,
    negative_results: list[dict[str, Any]],
    assertions: list[dict[str, Any]],
    details: dict[str, Any],
) -> dict[str, Any]:
    all_assertions = [*assertions, *_common_assertions(source_sha, corpus, negative_results, details)]
    failed_assertions = [item for item in all_assertions if item.get("status") != "passed"]
    negative_failures = sum(1 for item in negative_results if item.get("status") != "passed")
    failure_summary = [
        f"{item.get('assertionId')}: expected {item.get('expected')!r}, actual {item.get('actual')!r}"
        for item in failed_assertions
    ]
    return {
        "schema": "fdir/qualification-issue-97-report",
        "version": "1.0.0",
        "issueNumber": 97,
        "reportKind": report_kind,
        "qualificationScope": corpus.get("qualificationScope"),
        "sourceSha": source_sha,
        "corpusSha256": corpus_sha256,
        "status": "passed" if not failed_assertions else "failed",
        "completionStatus": "incomplete-bounded-lane",
        "authoredCaseCount": authored_case_count,
        "caseCounts": {
            "authored": authored_case_count,
            "positive": positive_case_count,
            "negativeMutations": len(negative_results),
        },
        "assertions": all_assertions,
        "negativeMutationResults": negative_results,
        "negativeDefectResults": negative_results,
        "negativeMutationFailureCount": negative_failures,
        "negativeDefectFailureCount": negative_failures,
        "details": details,
        "limitations": corpus.get("limitations", []),
        "unmetRequirements": [*failure_summary, *corpus.get("unmetRequirements", [])],
        "failureSummary": failure_summary,
    }


def _make_reports(
    source_sha: str | None,
    corpus_sha256: str | None,
    corpus: dict[str, Any],
    registry: dict[str, Any],
    schema_root: dict[str, Any],
    negative_results: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    emission = _stage(lambda: _emission_evidence(corpus, registry))
    schema = _stage(lambda: _schema_evidence(corpus, registry, schema_root))
    reference = _stage(lambda: _reference_evidence(corpus))
    policy = _stage(lambda: _policy_evidence(corpus, registry))
    version = _stage(lambda: _version_evidence(corpus, registry))
    migration = _stage(lambda: _migration_evidence(corpus))

    emission_assertions = [
        _assertion("authored-emission-case-count-positive", True, emission.get("authoredSiteCount", 0) > 0),
        _assertion("emission-fingerprint-coverage", 0, len(emission.get("missingAuthoredSites", [])) + len(emission.get("unexpectedObservedSites", []))),
        _assertion("unregistered-emission-zero", 0, len(emission.get("unregisteredEmissionKeys", []))),
        _assertion("registry-key-drift-zero", 0, len(emission.get("missingExpectedRegistryKeys", [])) + len(emission.get("unexpectedRegistryKeys", []))),
        _assertion("unused-read-only-drift-zero", 0, len(emission.get("unusedReadOnlyDrift", [])), detail=emission.get("unusedReadOnlyDrift")),
        _assertion("target-collection-contract-zero", 0, len(emission.get("targetContractMissing", []))),
        _assertion("target-kind-policy-mismatch-zero", 0, len(emission.get("targetPolicyMismatches", []))),
        _assertion("schema-digest-present-for-every-entry", 0, len(emission.get("schemaDigestMissing", []))),
        _assertion("typed-constructor-violation-zero", 0, emission.get("typedConstructorPolicy", {}).get("observedViolationCount", 0), detail=emission.get("typedConstructorPolicy")),
        _assertion("runtime-extension-emission-failures-zero", 0, emission.get("runtimeEmission", {}).get("failureCount", 0), detail=emission.get("runtimeEmission")),
    ]

    schema_assertions = [
        _assertion("authored-schema-case-count-positive", True, schema.get("authoredSchemaCaseCount", 0) > 0),
        _assertion("positive-payload-schema-errors-zero", 0, len(schema.get("positiveFailures", [])), detail=schema.get("positiveFailures")),
        _assertion("schema-path-resolution-errors-zero", 0, len(schema.get("schemaPathFailures", []))),
        _assertion("semantic-required-payload-fields-zero", 0, len(schema.get("semanticRequiredFailures", [])), detail=schema.get("semanticRequiredFailures")),
        _assertion("runtime-positive-payload-errors-zero", 0, len(schema.get("runtimePositiveFailures", [])), detail=schema.get("runtimePositiveFailures")),
        _assertion("schema-digest-metadata-zero", 0, schema.get("missingSchemaDigestCount", 0)),
    ]

    reference_runtime_failures = [
        item for item in reference.get("runtimeMutationResults", [])
        if item.get("expectedRejection") and item.get("actual") != "rejected"
    ]
    hooks = reference.get("runtimeReferenceHooks", {})
    reference_assertions = [
        _assertion("authored-reference-case-count-positive", True, reference.get("authoredReferenceContractCount", 0) > 0),
        _assertion("independent-reference-closure-zero", 0, len(reference.get("independentFailures", [])), detail=reference.get("independentFailures")),
        _assertion("reference-document-runtime-baseline-accepted", "accepted", reference.get("runtimeBaseline", {}).get("result"), detail=reference.get("runtimeBaseline")),
        _assertion("runtime-target-and-payload-mutation-failures-zero", 0, len(reference_runtime_failures), detail=reference_runtime_failures),
        _assertion("runtime-payload-reference-hook-present", True, hooks.get("checksPayloadReference")),
        _assertion("global-reference-registry-integration-present", True, hooks.get("usesGlobalReferenceRegistry")),
    ]

    policy_criticality_failures = [
        item for item in policy.get("runtimeFailures", [])
        if item.get("caseId") == "known-criticality-mismatch-policy"
    ]
    version_assertions = [
        _assertion("authored-version-matrix-positive", True, version.get("authoredVersionCaseCount", 0) > 0),
        _assertion("version-matrix-runtime-failures-zero", 0, len(version.get("runtimeFailures", [])), detail=version.get("runtimeFailures", [])[:10]),
        _assertion("executable-compatibility-contract", True, version.get("executableCompatibility")),
        _assertion("per-entry-version-metadata-complete", 0, len(version.get("missingExecutableMetadata", [])), detail=version.get("missingExecutableMetadata", [])[:10]),
        _assertion("criticality-mismatch-runtime-rejected", 0, len(policy_criticality_failures), detail=policy_criticality_failures),
        _assertion("unknown-policy-cases-independent", 0, len(policy.get("independentFailures", [])), detail=policy.get("independentFailures")),
    ]

    migration_assertions = [
        _assertion("authored-migration-case-count-positive", True, migration.get("authoredMigrationCaseCount", 0) > 0),
        _assertion("extension-migration-loss-failures-zero", 0, len(migration.get("migrationFailures", [])), detail=migration.get("migrationFailures")),
        _assertion("extension-migration-dispatch-present", True, migration.get("extensionMigrationHooks", {}).get("extensionVersionDispatch")),
        _assertion("migration-receipt-fields-present", True, migration.get("extensionMigrationHooks", {}).get("receiptFields")),
        _assertion("migration-downgrade-policy-present", True, migration.get("extensionMigrationHooks", {}).get("downgradePolicy")),
        _assertion("query-opaque-payload-round-trip", "passed", migration.get("queryRoundTrip", {}).get("status"), detail=migration.get("queryRoundTrip")),
        _assertion("canonical-opaque-payload-round-trip", "passed", migration.get("canonicalRoundTrip", {}).get("status"), detail=migration.get("canonicalRoundTrip")),
        _assertion("canonical-migration-receipt-nonempty", True, bool(migration.get("canonicalRoundTrip", {}).get("receipt")), detail=migration.get("canonicalRoundTrip", {}).get("receipt")),
    ]

    return {
        "emission": _report("extension-emission-coverage", source_sha, corpus, corpus_sha256, emission.get("authoredSiteCount", 0), emission.get("authoredRegisteredTypeCount", 0), negative_results, emission_assertions, emission),
        "schema": _report("extension-schema-conformance", source_sha, corpus, corpus_sha256, schema.get("authoredSchemaCaseCount", 0), schema.get("authoredSchemaCaseCount", 0) - len(schema.get("positiveFailures", [])), negative_results, schema_assertions, schema),
        "reference": _report("extension-reference-closure", source_sha, corpus, corpus_sha256, reference.get("authoredReferenceContractCount", 0), reference.get("authoredReferenceContractCount", 0) - len(reference.get("independentFailures", [])), negative_results, reference_assertions, reference),
        "version": _report("extension-version-compatibility", source_sha, corpus, corpus_sha256, version.get("authoredVersionCaseCount", 0) + policy.get("authoredPolicyCaseCount", 0), version.get("authoredVersionCaseCount", 0) - len(version.get("runtimeFailures", [])), negative_results, version_assertions, {"version": version, "policy": policy}),
        "migration": _report("extension-migration-report", source_sha, corpus, corpus_sha256, migration.get("authoredMigrationCaseCount", 0) + 2, migration.get("authoredMigrationCaseCount", 0) + 2 - len(migration.get("migrationFailures", [])), negative_results, migration_assertions, migration),
    }


def _fatal_reports(source_sha: str | None, corpus: dict[str, Any] | None, corpus_sha256: str | None, message: str) -> dict[str, dict[str, Any]]:
    fallback = corpus or {
        "qualificationScope": "bounded-independent-extension-registry-typed-payload-reference-version-migration-query-canonical",
        "oracle": {"expectedValuesAreRuntimeIndependent": False, "adapterHelpersUsedForExpected": True},
        "limitations": ["Corpus could not be loaded."],
        "unmetRequirements": ["Qualification setup failed."],
    }
    negative_results: list[dict[str, Any]] = []
    reports: dict[str, dict[str, Any]] = {}
    for report_kind, report_name in REPORT_NAMES.items():
        reports[report_kind] = _report(
            report_name.removesuffix(".json"),
            source_sha,
            fallback,
            corpus_sha256,
            0,
            0,
            negative_results,
            [_assertion("fatal-setup", "available", "unavailable", detail=message)],
            {"setupError": message},
        )
        reports[report_kind]["status"] = "failed"
    return reports


def run_qualification(*, corpus_path: Path = DEFAULT_CORPUS_PATH, out_dir: Path = DEFAULT_OUT_DIR) -> int:
    source_sha: str | None = None
    corpus: dict[str, Any] | None = None
    corpus_sha256: str | None = None
    negative_results: list[dict[str, Any]] = []
    setup_error: str | None = None
    try:
        source_sha = _source_sha()
        corpus_sha256 = _sha256_file(corpus_path)
        corpus = _load_corpus(corpus_path)
        registry = _read_json(REGISTRY_PATH)
        schema_root = _read_json(EXTENSION_SCHEMA_PATH)
        if not isinstance(registry, dict) or not isinstance(schema_root, dict):
            raise QualificationError("under-test registry or extension schema root is not an object")
        negative_results = _run_negative_mutations(corpus)
        reports = _make_reports(source_sha, corpus_sha256, corpus, registry, schema_root, negative_results)
    except Exception as exc:
        setup_error = f"{type(exc).__name__}: {exc}"
        reports = _fatal_reports(source_sha, corpus, corpus_sha256, setup_error)
    out_dir.mkdir(parents=True, exist_ok=True)
    producer_report = _write_producer_report(
        out_dir,
        reports,
        Path(corpus_path),
        source_sha,
        _producer_rows(corpus, reports, negative_results, setup_error=setup_error),
    )
    failed = [report_name for report_name, report in reports.items() if report.get("status") != "passed"]
    if producer_report.get("status") != "passed":
        failed.append("producer-report")
    if failed:
        failed_names = [REPORT_NAMES[item] if item in REPORT_NAMES else item for item in failed]
        print("FAIL: issue #97 bounded reports: " + ", ".join(failed_names), file=sys.stderr)
        return 1
    print("PASS: issue #97 bounded reports written: " + ", ".join(REPORT_NAMES.values()))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_PATH)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    raise SystemExit(run_qualification(corpus_path=args.corpus, out_dir=args.out_dir))
