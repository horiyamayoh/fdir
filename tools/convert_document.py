"""Public bounded input-to-IR command for the FDIR adapters.

Examples::

    python tools/convert_document.py inspect e2e/fixtures/sample.docx
    python tools/convert_document.py convert e2e/fixtures/sample.docx --out out.json --evidence out.evidence.json
    python tools/convert_document.py validate out.json

The evidence sidecar is ingestion metadata, not part of Document Form IR.  It
contains a hash and execution facts so E2E tests can prove that a real input
was consumed without embedding source bytes in the IR.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
from pathlib import Path
import sys
import time
from typing import Any

try:
    from adapter_common import AdapterLimits, AdapterError, failed_document, input_limit_check
    from ir_validation import validate_document
except ImportError:  # pragma: no cover - package-style import for callers
    from tools.adapter_common import AdapterLimits, AdapterError, failed_document, input_limit_check
    from tools.ir_validation import validate_document


ROOT = Path(__file__).resolve().parents[1]
ADAPTERS = {
    "docx": "adapter_docx",
    "xlsx": "adapter_xlsx",
    "pdf": "adapter_pdf",
    "markdown": "adapter_markdown",
}
EXTENSIONS = {".docx": "docx", ".xlsx": "xlsx", ".pdf": "pdf", ".md": "markdown", ".markdown": "markdown"}


def detect_format(path: Path, explicit: str | None = None) -> str:
    if explicit:
        if explicit not in ADAPTERS:
            raise ValueError(f"unsupported format: {explicit}")
        return explicit
    try:
        return EXTENSIONS[path.suffix.lower()]
    except KeyError as exc:
        raise ValueError(f"cannot detect format from extension: {path.suffix}") from exc


def adapter_module(format_name: str):
    name = ADAPTERS[format_name]
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        return importlib.import_module(f"tools.{name}")


def input_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _call(module: Any, operation: str, path: Path, limits: AdapterLimits) -> Any:
    function = getattr(module, operation, None)
    if function is None:
        raise AdapterError(f"adapter {module.__name__} does not expose {operation}()")
    return function(path, limits=limits)


def convert_path(path: Path, format_name: str | None = None, limits: AdapterLimits | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    path = Path(path)
    limits = limits or AdapterLimits()
    detected = detect_format(path, format_name)
    started = time.time()
    module_name = ADAPTERS[detected]
    try:
        input_limit_check(path, limits)
        module = adapter_module(detected)
        document = _call(module, "convert", path, limits)
        if not isinstance(document, dict):
            raise AdapterError("adapter convert() did not return an IR object")
        validate_document(document)
        outcome = "failed" if document.get("conversion", {}).get("status") == "failed" else "success"
    except Exception as exc:  # adapter boundary must return an explicit failed result
        version = {"docx": "ECMA-376", "xlsx": "Office Open XML", "pdf": "1.7", "markdown": "commonmark"}[detected]
        document = failed_document(path, detected, version, f"DFIR-{detected.upper()}-ADAPTER-FAILED", str(exc))
        validate_document(document)
        outcome = "failed"
    evidence = {
        "schema": "fdir/adapter-execution-evidence",
        "version": "1.0.0",
        "input": {
            "path": str(path.resolve()),
            "format": detected,
            "bytes": path.stat().st_size if path.is_file() else None,
            "sha256": input_sha256(path) if path.is_file() else None,
            "consumed": path.is_file(),
        },
        "adapter": {"module": module_name, "operation": "convert"},
        "outcome": outcome,
        "documentId": document.get("documentId"),
        "conversionStatus": document.get("conversion", {}).get("status"),
        "diagnosticCount": len(document.get("diagnostics", [])),
        "elapsedMilliseconds": round((time.time() - started) * 1000, 3),
    }
    return document, evidence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("input", type=Path)
    inspect.add_argument("--format", choices=sorted(ADAPTERS))
    convert = sub.add_parser("convert")
    convert.add_argument("input", type=Path)
    convert.add_argument("--format", choices=sorted(ADAPTERS))
    convert.add_argument("--out", type=Path, required=True)
    convert.add_argument("--evidence", type=Path)
    convert.add_argument("--max-input-bytes", type=int, default=AdapterLimits.max_input_bytes)
    convert.add_argument("--max-nodes", type=int, default=AdapterLimits.max_nodes)
    convert.add_argument("--max-text-chars", type=int, default=AdapterLimits.max_text_chars)
    validate = sub.add_parser("validate")
    validate.add_argument("input", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.operation == "validate":
        document = json.loads(args.input.read_text(encoding="utf-8"))
        warnings = validate_document(document)
        print(json.dumps({"status": "valid", "warnings": warnings}, ensure_ascii=False))
        return 0
    if args.operation == "inspect":
        detected = detect_format(args.input, args.format)
        module = adapter_module(detected)
        report = _call(module, "inspect", args.input, AdapterLimits())
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    limits = AdapterLimits(
        max_input_bytes=args.max_input_bytes,
        max_nodes=args.max_nodes,
        max_text_chars=args.max_text_chars,
    )
    document, evidence = convert_path(args.input, args.format, limits)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.evidence:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": evidence["outcome"], "format": evidence["input"]["format"], "output": str(args.out), "evidence": str(args.evidence) if args.evidence else None}, ensure_ascii=False))
    return 0 if evidence["outcome"] == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
