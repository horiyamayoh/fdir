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
import inspect
import json
from pathlib import Path
import sys
import time
from typing import Any

try:
    from adapter_common import AdapterLimits, AdapterError, failed_document, input_limit_check
    from ir_validation import validate_document
except ImportError:  # pragma: no cover - package-style import for callers
    try:
        from tools.adapter_common import AdapterLimits, AdapterError, failed_document, input_limit_check
        from tools.ir_validation import validate_document
    except ImportError:  # pragma: no cover - isolated script execution outside the repository root
        _TOOLS_ROOT = Path(__file__).resolve().parent
        if str(_TOOLS_ROOT) not in sys.path:
            sys.path.insert(0, str(_TOOLS_ROOT))
        from adapter_common import AdapterLimits, AdapterError, failed_document, input_limit_check
        from ir_validation import validate_document


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


def input_sha256(path: Path, *, max_bytes: int | None = None) -> str:
    digest = hashlib.sha256()
    bytes_read = 0
    with path.open("rb") as stream:
        while True:
            read_size = 1024 * 1024 if max_bytes is None else min(1024 * 1024, max_bytes - bytes_read + 1)
            block = stream.read(max(1, read_size))
            if not block:
                break
            bytes_read += len(block)
            if max_bytes is not None and bytes_read > max_bytes:
                raise AdapterError(f"input hash budget exceeded: {bytes_read} > {max_bytes}")
            digest.update(block)
    return digest.hexdigest()


def _call(module: Any, operation: str, path: Path, limits: AdapterLimits, profile: str | None = None) -> Any:
    function = getattr(module, operation, None)
    if function is None:
        raise AdapterError(f"adapter {module.__name__} does not expose {operation}()")
    if profile is not None and "profile" in inspect.signature(function).parameters:
        return function(path, limits=limits, profile=profile)
    return function(path, limits=limits)


def convert_path(path: Path, format_name: str | None = None, limits: AdapterLimits | None = None, profile: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    path = Path(path)
    limits = limits or AdapterLimits()
    detected = detect_format(path, format_name)
    started = time.time()
    module_name = ADAPTERS[detected]
    limit_check_passed = False
    parser_attempted = False
    try:
        input_limit_check(path, limits)
        limit_check_passed = True
        module = adapter_module(detected)
        parser_attempted = True
        document = _call(module, "convert", path, limits, profile)
        if not isinstance(document, dict):
            raise AdapterError("adapter convert() did not return an IR object")
        validate_document(document)
        outcome = "failed" if document.get("conversion", {}).get("status") == "failed" else "success"
    except Exception as exc:  # adapter boundary must return an explicit failed result
        version = {"docx": "ECMA-376", "xlsx": "Office Open XML", "pdf": "1.7", "markdown": "commonmark"}[detected]
        document = failed_document(path, detected, version, f"DFIR-{detected.upper()}-ADAPTER-FAILED", str(exc))
        validate_document(document)
        outcome = "failed"
    input_bytes: int | None = None
    input_hash: str | None = None
    input_is_file = path.is_file()
    if input_is_file:
        try:
            input_bytes = path.stat().st_size
            if input_bytes <= limits.max_input_bytes:
                input_hash = input_sha256(path, max_bytes=limits.max_input_bytes)
        except (AdapterError, OSError):
            # Evidence must not turn a bounded conversion failure into an
            # unbounded read or a second failure at the adapter boundary.
            input_hash = None
    evidence = {
        "schema": "fdir/adapter-execution-evidence",
        "version": "1.0.0",
        "input": {
            "path": str(path.resolve()),
            "format": detected,
            "bytes": input_bytes,
            "sha256": input_hash,
            "parserAttempted": parser_attempted,
            "limitRejectedBeforeParse": input_is_file and not limit_check_passed,
            # ``consumed`` means the public boundary inspected a regular input
            # path, including a fail-closed pre-parse limit rejection.  The
            # two explicit fields above distinguish that from adapter parser
            # execution for callers that need the finer-grained fact.
            "consumed": input_is_file,
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
    inspect.add_argument("--profile")
    convert = sub.add_parser("convert")
    convert.add_argument("input", type=Path)
    convert.add_argument("--format", choices=sorted(ADAPTERS))
    convert.add_argument("--out", type=Path, required=True)
    convert.add_argument("--profile")
    convert.add_argument("--evidence", type=Path)
    convert.add_argument("--max-input-bytes", type=int, default=AdapterLimits.max_input_bytes)
    convert.add_argument("--max-nodes", type=int, default=AdapterLimits.max_nodes)
    convert.add_argument("--max-text-chars", type=int, default=AdapterLimits.max_text_chars)
    convert.add_argument("--max-xml-parts", type=int, default=AdapterLimits.max_xml_parts)
    convert.add_argument("--max-xml-bytes", type=int, default=AdapterLimits.max_xml_bytes)
    convert.add_argument("--max-xml-nodes", type=int, default=AdapterLimits.max_xml_nodes)
    convert.add_argument("--max-xml-depth", type=int, default=AdapterLimits.max_xml_depth)
    convert.add_argument("--max-zip-entries", type=int, default=AdapterLimits.max_zip_entries)
    convert.add_argument("--max-zip-uncompressed-bytes", type=int, default=AdapterLimits.max_zip_uncompressed_bytes)
    convert.add_argument("--max-zip-entry-bytes", type=int, default=AdapterLimits.max_zip_entry_bytes)
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
        report = _call(module, "inspect", args.input, AdapterLimits(), args.profile)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    limits = AdapterLimits(
        max_input_bytes=args.max_input_bytes,
        max_nodes=args.max_nodes,
        max_text_chars=args.max_text_chars,
        max_xml_parts=args.max_xml_parts,
        max_xml_bytes=args.max_xml_bytes,
        max_xml_nodes=args.max_xml_nodes,
        max_xml_depth=args.max_xml_depth,
        max_zip_entries=args.max_zip_entries,
        max_zip_uncompressed_bytes=args.max_zip_uncompressed_bytes,
        max_zip_entry_bytes=args.max_zip_entry_bytes,
    )
    input_path = args.input.resolve()
    output_path = args.out.resolve()
    evidence_path = args.evidence.resolve() if args.evidence else None
    if output_path == input_path:
        print("conversion output must not overwrite the input", file=sys.stderr)
        return 2
    if evidence_path is not None and evidence_path in {input_path, output_path}:
        print("evidence output must be distinct from input and IR output", file=sys.stderr)
        return 2
    document, evidence = convert_path(args.input, args.format, limits, args.profile)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.evidence:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": evidence["outcome"], "format": evidence["input"]["format"], "output": str(args.out), "evidence": str(args.evidence) if args.evidence else None}, ensure_ascii=False))
    return 0 if evidence["outcome"] == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
