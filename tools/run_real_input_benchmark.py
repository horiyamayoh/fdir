"""Run and verify a SHA-bound real-input benchmark corpus.

The runner deliberately keeps generated IR and evidence outside the tracked
result directory.  It records one compact JSON object per input in JSONL so a
large corpus can be processed without materialising one large aggregate in
memory.

Examples::

    python tools/run_real_input_benchmark.py run \
        --manifest e2e/corpus/real-world/lynxgate/manifest.json \
        --out e2e/results/lynxgate/baseline-<sha>
    python tools/run_real_input_benchmark.py verify \
        --report e2e/results/lynxgate/final-<sha>/summary.json
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
import posixpath
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Iterable
import zipfile


ROOT = Path(__file__).resolve().parents[1]
CONVERTER = ROOT / "tools" / "convert_document.py"
CANONICALIZER = ROOT / "tools" / "canonicalize_ir.py"
RUN_ROOT = ROOT / "e2e" / ".run"
ALLOWED_FORMATS = {"docx", "xlsx"}
CASE_STATUSES = {"processed", "expected-limit", "conversion-failed", "infrastructure-error"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class BenchmarkError(RuntimeError):
    """A corpus, execution, or result-integrity failure."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_stream(stream: Any) -> str:
    digest = hashlib.sha256()
    while True:
        block = stream.read(1024 * 1024)
        if not block:
            break
        digest.update(block)
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return sha256_stream(stream)


def stable_repo_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return f"<external>/{resolved.name}"


def _is_under_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def redact_runtime_text(value: Any, roots: Iterable[Path] = ()) -> str:
    text = str(value)
    for root in (ROOT, *roots):
        resolved = str(root.resolve())
        text = text.replace(resolved, "<repo>").replace(resolved.replace("\\", "/"), "<repo>")
    return re.sub(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/])[^\r\n\s]+", "<absolute-path>", text)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"cannot read JSON {stable_repo_path(path)}: {exc}") from exc
    if not isinstance(value, dict):
        raise BenchmarkError(f"JSON root is not an object: {stable_repo_path(path)}")
    return value


def validate_relative_path(base: Path, value: str, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise BenchmarkError(f"{label} path is empty")
    candidate = Path(value)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise BenchmarkError(f"{label} path escapes its base: {value}")
    resolved = (base / candidate).resolve()
    try:
        resolved.relative_to(base.resolve())
    except ValueError as exc:
        raise BenchmarkError(f"{label} path escapes its base: {value}") from exc
    return resolved


def validate_zip_member(name: str) -> str:
    if not isinstance(name, str) or not name or "\x00" in name:
        raise BenchmarkError(f"invalid ZIP member name: {name}")
    if "\\" in name:
        raise BenchmarkError(f"non-canonical ZIP member separator is not allowed: {name}")
    normalized = name
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise BenchmarkError(f"absolute ZIP member is not allowed: {name}")
    canonical = normalized[:-1] if normalized.endswith("/") else normalized
    if not canonical:
        raise BenchmarkError(f"empty ZIP member name is not allowed: {name}")
    parts = canonical.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise BenchmarkError(f"parent traversal in ZIP member is not allowed: {name}")
    if PurePosixPath(canonical).as_posix() != canonical or posixpath.normpath(canonical) != canonical:
        raise BenchmarkError(f"non-canonical ZIP member path is not allowed: {name}")
    return normalized


def git_value(arguments: list[str]) -> str:
    command = ["git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", *arguments]
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise BenchmarkError(f"git command failed: {' '.join(arguments)}: {redact_runtime_text(result.stderr)}")
    return result.stdout.strip()


def source_identity(explicit_sha: str | None) -> tuple[str, bool]:
    source_sha = explicit_sha or git_value(["rev-parse", "HEAD"])
    if re.fullmatch(r"[0-9a-f]{40}", source_sha) is None:
        raise BenchmarkError(f"source SHA is not a full commit SHA: {source_sha}")
    dirty_tree = bool(git_value(["status", "--porcelain"]))
    return source_sha, dirty_tree


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256_RE.fullmatch(value) is not None


def expected_limit_match(manifest: dict[str, Any], format_name: str, diagnostics: list[dict[str, Any]]) -> bool:
    expected = manifest.get("expected", {})
    policies = expected.get("expectedLimitDiagnostics", []) if isinstance(expected, dict) else []
    for policy in policies:
        if not isinstance(policy, dict) or policy.get("format") != format_name:
            continue
        for diagnostic in diagnostics:
            if diagnostic.get("code") != policy.get("code"):
                continue
            message = str(diagnostic.get("message", ""))
            if str(policy.get("messageContains", "")) in message:
                return True
    return False


def document_digest(info: zipfile.ZipInfo, archive: zipfile.ZipFile) -> str:
    with archive.open(info, "r") as stream:
        return sha256_stream(stream)


def load_manifest(manifest_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest_path = manifest_path.resolve()
    manifest = read_json(manifest_path)
    if manifest.get("schema") != "fdir/real-input-corpus-manifest" or manifest.get("version") != "1.0.0":
        raise BenchmarkError("unsupported real-input corpus manifest schema")
    archives = manifest.get("archives")
    documents = manifest.get("documents")
    expected = manifest.get("expected")
    if not isinstance(archives, list) or not isinstance(documents, list) or not isinstance(expected, dict):
        raise BenchmarkError("manifest must contain archives, documents, and expected objects")
    expected_count = int(expected.get("documents", -1))
    if len(documents) != expected_count:
        raise BenchmarkError(f"manifest document count is {len(documents)}, expected {expected_count}")

    archive_by_id: dict[str, tuple[dict[str, Any], Path]] = {}
    for archive in archives:
        if not isinstance(archive, dict) or not isinstance(archive.get("id"), str) or not archive.get("id"):
            raise BenchmarkError("manifest contains an invalid archive record")
        archive_id = archive["id"]
        if archive_id in archive_by_id:
            raise BenchmarkError(f"duplicate archive id: {archive_id}")
        path = validate_relative_path(manifest_path.parent, str(archive.get("path", "")), f"archive {archive_id}")
        if not path.is_file():
            raise BenchmarkError(f"archive does not exist: {stable_repo_path(path)}")
        actual_bytes = path.stat().st_size
        if int(archive.get("bytes", -1)) != actual_bytes:
            raise BenchmarkError(f"archive byte count mismatch: {archive_id}")
        actual_sha = sha256_file(path)
        if str(archive.get("sha256", "")).lower() != actual_sha:
            raise BenchmarkError(f"archive SHA-256 mismatch: {archive_id}")
        archive_formats = archive.get("formats")
        if not isinstance(archive_formats, dict) or any(name not in ALLOWED_FORMATS for name in archive_formats):
            raise BenchmarkError(f"archive format inventory is invalid: {archive_id}")
        archive_by_id[archive_id] = (archive, path)

    normalized_documents: list[dict[str, Any]] = []
    seen_document_ids: set[str] = set()
    seen_document_keys: set[tuple[str, str]] = set()
    documents_by_archive: Counter[str] = Counter()
    for document in documents:
        if not isinstance(document, dict):
            raise BenchmarkError("manifest contains a non-object document record")
        document_id = document.get("id")
        archive_id = document.get("archiveId")
        raw_member_name = document.get("path")
        format_name = document.get("format")
        if not isinstance(document_id, str) or not document_id or document_id in seen_document_ids:
            raise BenchmarkError(f"duplicate or invalid document id: {document_id}")
        if not isinstance(archive_id, str) or not archive_id:
            raise BenchmarkError(f"document has an invalid archive id: {archive_id}")
        if archive_id not in archive_by_id:
            raise BenchmarkError(f"document references unknown archive: {archive_id}")
        if not isinstance(raw_member_name, str):
            raise BenchmarkError(f"document has an invalid path: {raw_member_name}")
        member_name = validate_zip_member(raw_member_name)
        if member_name.endswith("/"):
            raise BenchmarkError(f"document path names a directory: {member_name}")
        if not isinstance(format_name, str) or format_name not in ALLOWED_FORMATS:
            raise BenchmarkError(f"unsupported document format in manifest: {format_name}")
        expected_suffix = f".{format_name}"
        if not member_name.lower().endswith(expected_suffix):
            raise BenchmarkError(f"document format does not match path: {member_name}")
        key = (str(archive_id), member_name)
        if key in seen_document_keys:
            raise BenchmarkError(f"duplicate document path: {archive_id}/{member_name}")
        seen_document_ids.add(document_id)
        seen_document_keys.add(key)
        documents_by_archive[str(archive_id)] += 1

    for archive_id, (archive_record, archive_path) in archive_by_id.items():
        with zipfile.ZipFile(archive_path, "r") as archive:
            names = [validate_zip_member(info.filename) for info in archive.infolist()]
            if len(names) != len(set(names)):
                raise BenchmarkError(f"duplicate ZIP member in archive: {archive_id}")
            info_by_name = {validate_zip_member(info.filename): info for info in archive.infolist()}
            actual_documents = {
                name
                for name in info_by_name
                if not name.endswith("/") and Path(name).suffix.lower().lstrip(".") in ALLOWED_FORMATS
            }
            manifest_documents = {path for aid, path in seen_document_keys if aid == archive_id}
            if actual_documents != manifest_documents:
                missing = sorted(actual_documents - manifest_documents)
                extra = sorted(manifest_documents - actual_documents)
                raise BenchmarkError(f"document inventory mismatch for {archive_id}: missing={missing[:3]} extra={extra[:3]}")
            if int(archive_record.get("documentCount", -1)) != len(actual_documents):
                raise BenchmarkError(f"document count mismatch for archive: {archive_id}")
            actual_formats = Counter(Path(name).suffix.lower().lstrip(".") for name in actual_documents)
            expected_archive_formats = archive_record.get("formats", {})
            if any(actual_formats.get(name, 0) != int(expected_archive_formats.get(name, 0)) for name in ALLOWED_FORMATS):
                raise BenchmarkError(f"archive format counts do not match: {archive_id}")
            if documents_by_archive[archive_id] != len(actual_documents):
                raise BenchmarkError(f"manifest document count does not match archive inventory: {archive_id}")
            for document in documents:
                if document.get("archiveId") != archive_id:
                    continue
                member_name = str(document["path"]).replace("\\", "/")
                info = info_by_name[member_name]
                actual_bytes = int(info.file_size)
                actual_sha = document_digest(info, archive)
                if int(document.get("bytes", -1)) != actual_bytes or str(document.get("sha256", "")).lower() != actual_sha:
                    raise BenchmarkError(f"document digest mismatch: {document['id']}")
                normalized = dict(document)
                normalized["archivePath"] = archive_path
                normalized["memberName"] = member_name
                normalized_documents.append(normalized)

    if len(normalized_documents) != len(documents):
        raise BenchmarkError("manifest normalization lost document records")
    normalized_documents.sort(key=lambda item: str(item["id"]))
    actual_formats = Counter(str(item["format"]) for item in normalized_documents)
    expected_formats = expected.get("formats", {})
    if any(actual_formats.get(name, 0) != int(count) for name, count in expected_formats.items()):
        raise BenchmarkError(f"manifest format counts do not match: {dict(actual_formats)}")
    return manifest, normalized_documents


def child_environment() -> dict[str, str]:
    """Make public child CLIs importable under isolated Python distributions."""

    environment = os.environ.copy()
    import_paths = [str(ROOT), str(ROOT / "tools")]
    existing = environment.get("PYTHONPATH")
    if existing:
        import_paths.append(existing)
    environment["PYTHONPATH"] = os.pathsep.join(import_paths)
    return environment


def run_process(arguments: list[str], *, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=cwd,
        env=child_environment(),
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )


def extract_document(archive_path: Path, member_name: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "r") as archive:
        with archive.open(member_name, "r") as source, destination.open("wb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)


def parse_document_result(
    document: dict[str, Any],
    evidence: dict[str, Any],
    validation: subprocess.CompletedProcess[str],
    canonical: subprocess.CompletedProcess[str],
    document_path: Path,
    evidence_path: Path,
    output_path: Path,
    case: dict[str, Any],
    manifest: dict[str, Any],
    conversion_returncode: int,
    conversion_stderr: str,
) -> dict[str, Any]:
    diagnostics = [item for item in document.get("diagnostics", []) if isinstance(item, dict)]
    diagnostic_codes = list(dict.fromkeys(str(item.get("code")) for item in diagnostics if item.get("code")))
    conversion_status = document.get("conversion", {}).get("status")
    evidence_input = evidence.get("input", {}) if isinstance(evidence.get("input"), dict) else {}
    evidence_matches_input = (
        evidence.get("schema") == "fdir/adapter-execution-evidence"
        and evidence.get("version") == "1.0.0"
        and evidence_input.get("format") == case.get("format")
        and evidence_input.get("bytes") == case.get("bytes")
        and evidence_input.get("sha256") == case.get("sha256")
        and evidence_input.get("consumed") is True
        and evidence.get("documentId") == document.get("documentId")
        and evidence.get("conversionStatus") == conversion_status
    )
    conversion_exit_consistent = (
        conversion_returncode == 0 and conversion_status != "failed"
    ) or (
        conversion_returncode == 2 and conversion_status == "failed"
    )
    if (
        validation.returncode != 0
        or canonical.returncode != 0
        or not canonical.stdout.strip()
        or not evidence_matches_input
        or not conversion_exit_consistent
    ):
        case_status = "infrastructure-error"
    elif conversion_status == "failed":
        case_status = "expected-limit" if expected_limit_match(manifest, str(case["format"]), diagnostics) else "conversion-failed"
    else:
        case_status = "processed"
    extensions = document.get("extensions", [])
    has_conditional_extension = any(isinstance(item, dict) and item.get("type") == "conditional-formatting" for item in extensions)
    return {
        "id": case["id"],
        "archiveId": case["archiveId"],
        "sourcePath": case["path"],
        "format": case["format"],
        "inputBytes": case["bytes"],
        "inputSha256": case["sha256"],
        "extractedInputBytes": case["extractedBytes"],
        "extractedInputSha256": case["extractedSha256"],
        "caseStatus": case_status,
        "executionStatus": "processed" if case_status != "infrastructure-error" else "failed",
        "conversionStatus": conversion_status,
        "conversionReturnCode": conversion_returncode,
        "schemaValidation": "passed" if validation.returncode == 0 else "failed",
        "canonicalDigest": canonical.stdout.strip() if canonical.returncode == 0 else None,
        "irSha256": sha256_file(output_path),
        "irBytes": output_path.stat().st_size,
        "evidenceSha256": sha256_file(evidence_path),
        "evidenceBytes": evidence_path.stat().st_size,
        "evidenceInputSha256": evidence_input.get("sha256"),
        "evidenceConsumed": evidence_input.get("consumed"),
        "documentId": document.get("documentId"),
        "diagnosticCodes": diagnostic_codes,
        "diagnosticCount": len(diagnostics),
        "hasConditionalExtension": has_conditional_extension,
        "command": f"python tools/convert_document.py convert <{case['format']}-input> --format {case['format']} --out document-form.json --evidence evidence.json",
        "runtimeDiagnostic": redact_runtime_text(conversion_stderr[-1000:], (document_path.parent,)) if conversion_stderr else None,
    }


def choose_representatives(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    def choose(predicate: Any) -> None:
        for case in cases:
            if case["id"] not in seen and predicate(case):
                selected.append(case)
                seen.add(case["id"])
                return

    choose(lambda item: item["caseStatus"] == "expected-limit")
    choose(lambda item: item["format"] == "docx" and item["caseStatus"] == "processed")
    choose(lambda item: item["format"] == "xlsx" and item["hasConditionalExtension"])
    choose(lambda item: item["format"] == "xlsx" and "DFIR-XLSX-COMPUTED-VALUE-UNAVAILABLE" in item["diagnosticCodes"])
    choose(lambda item: item["format"] == "xlsx" and item["caseStatus"] == "processed")
    return selected


def run_benchmark(manifest_path: Path, output_dir: Path, label: str, explicit_sha: str | None) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise BenchmarkError(f"result directory is not empty: {stable_repo_path(output_dir)}")
    manifest, cases = load_manifest(manifest_path)
    source_sha, dirty_tree = source_identity(explicit_sha)
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = utc_now()
    run_root = RUN_ROOT.resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix=f"lynxgate-{label}-{source_sha[:12]}-", dir=run_root))
    case_records: list[dict[str, Any]] = []
    artifact_paths: dict[str, tuple[Path, Path]] = {}
    cases_path = output_dir / "cases.jsonl"
    with cases_path.open("w", encoding="utf-8", newline="\n") as cases_stream:
        for case in cases:
            case_workspace = workspace / case["id"]
            input_path = case_workspace / f"input.{case['format']}"
            output_path = case_workspace / "document-form.json"
            evidence_path = case_workspace / "evidence.json"
            try:
                extract_document(case["archivePath"], case["memberName"], input_path)
                extracted_bytes = input_path.stat().st_size
                extracted_sha256 = sha256_file(input_path)
                if extracted_bytes != case["bytes"] or extracted_sha256 != case["sha256"]:
                    raise BenchmarkError(f"extracted input digest mismatch: {case['id']}")
                case["extractedBytes"] = extracted_bytes
                case["extractedSha256"] = extracted_sha256
                conversion = run_process(
                    [
                        sys.executable,
                        str(CONVERTER),
                        "convert",
                        str(input_path),
                        "--format",
                        str(case["format"]),
                        "--out",
                        str(output_path),
                        "--evidence",
                        str(evidence_path),
                    ]
                )
                document = read_json(output_path)
                evidence = read_json(evidence_path)
                validation = run_process([sys.executable, str(CONVERTER), "validate", str(output_path)])
                canonical = run_process([sys.executable, str(CANONICALIZER), str(output_path), "--digest"])
                record = parse_document_result(
                    document,
                    evidence,
                    validation,
                    canonical,
                    input_path,
                    evidence_path,
                    output_path,
                    case,
                    manifest,
                    conversion.returncode,
                    conversion.stderr,
                )
                artifact_paths[case["id"]] = (output_path, evidence_path)
            except Exception as exc:
                record = {
                    "id": case["id"],
                    "archiveId": case["archiveId"],
                    "sourcePath": case["path"],
                    "format": case["format"],
                    "inputBytes": case["bytes"],
                    "inputSha256": case["sha256"],
                    "extractedInputBytes": case.get("extractedBytes"),
                    "extractedInputSha256": case.get("extractedSha256"),
                    "caseStatus": "infrastructure-error",
                    "executionStatus": "failed",
                    "conversionStatus": None,
                    "conversionReturnCode": None,
                    "schemaValidation": "not-run",
                    "canonicalDigest": None,
                    "irSha256": None,
                    "irBytes": None,
                    "evidenceSha256": None,
                    "evidenceBytes": None,
                    "evidenceInputSha256": None,
                    "evidenceConsumed": None,
                    "documentId": None,
                    "diagnosticCodes": [],
                    "diagnosticCount": 0,
                    "hasConditionalExtension": False,
                    "command": None,
                    "runtimeDiagnostic": redact_runtime_text(exc, (workspace,)),
                }
            case_records.append(record)
            cases_stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")

    representatives: list[dict[str, Any]] = []
    representative_dir = output_dir / "representatives"
    representative_dir.mkdir(parents=True, exist_ok=True)
    for case in choose_representatives(case_records):
        paths = artifact_paths.get(case["id"])
        if paths is None:
            continue
        source_output, source_evidence = paths
        output_name = f"{case['id']}.document-form.json"
        evidence_name = f"{case['id']}.evidence.json"
        destination_output = representative_dir / output_name
        destination_evidence = representative_dir / evidence_name
        shutil.copyfile(source_output, destination_output)
        shutil.copyfile(source_evidence, destination_evidence)
        representatives.append(
            {
                "id": case["id"],
                "document": f"representatives/{output_name}",
                "evidence": f"representatives/{evidence_name}",
                "documentSha256": sha256_file(destination_output),
                "evidenceSha256": sha256_file(destination_evidence),
            }
        )

    case_counts = Counter(str(item["caseStatus"]) for item in case_records)
    conversion_counts = Counter(str(item["conversionStatus"]) for item in case_records if item.get("conversionStatus"))
    unexpected = case_counts.get("conversion-failed", 0) + case_counts.get("infrastructure-error", 0)
    summary = {
        "schema": "fdir/real-input-benchmark-report",
        "version": "1.0.0",
        "status": "passed" if unexpected == 0 and len(case_records) == len(cases) else "failed",
        "runId": f"{label}-{source_sha[:12]}",
        "label": label,
        "sourceSha": source_sha,
        "dirtyTree": dirty_tree,
        "manifestPath": stable_repo_path(manifest_path),
        "manifestSha256": sha256_file(manifest_path),
        "startedAt": started_at,
        "finishedAt": utc_now(),
        "environment": {
            "python": platform.python_version(),
            "executable": stable_repo_path(Path(sys.executable)) if _is_under_root(Path(sys.executable), ROOT) else platform.python_implementation(),
            "platform": platform.platform(),
        },
        "corpus": {
            "expectedDocuments": len(cases),
            "observedDocuments": len(case_records),
            "caseStatusCounts": dict(sorted(case_counts.items())),
            "conversionStatusCounts": dict(sorted(conversion_counts.items())),
            "unexpectedFailureCount": unexpected,
        },
        "casesFile": "cases.jsonl",
        "representatives": representatives,
        "expectedLimitPolicy": manifest.get("expected", {}).get("expectedLimitDiagnostics", []),
        "qualification": "practical real-input observation; not release qualification",
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def verify_report(report_path: Path) -> dict[str, Any]:
    report_path = report_path.resolve()
    report = read_json(report_path)
    if report.get("schema") != "fdir/real-input-benchmark-report" or report.get("version") != "1.0.0":
        raise BenchmarkError("unsupported benchmark report schema")
    source_sha = str(report.get("sourceSha", ""))
    if re.fullmatch(r"[0-9a-f]{40}", source_sha) is None:
        raise BenchmarkError("report sourceSha is not a full commit SHA")
    label = report.get("label")
    if not isinstance(label, str) or not label or report.get("runId") != f"{label}-{source_sha[:12]}":
        raise BenchmarkError("report runId is not bound to label and sourceSha")
    manifest_path = validate_relative_path(ROOT, str(report.get("manifestPath", "")), "report manifest")
    if not is_sha256(report.get("manifestSha256")) or sha256_file(manifest_path) != report.get("manifestSha256"):
        raise BenchmarkError("report manifest SHA-256 does not match the current checkout")
    manifest, manifest_cases = load_manifest(manifest_path)
    manifest_by_id = {str(item["id"]): item for item in manifest_cases}
    cases_file = validate_relative_path(report_path.parent, str(report.get("casesFile", "")), "report cases")
    if not cases_file.is_file():
        raise BenchmarkError(f"report cases file is missing: {stable_repo_path(cases_file)}")
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    with cases_file.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BenchmarkError(f"invalid JSONL at line {line_number}: {exc}") from exc
            if not isinstance(record, dict) or not isinstance(record.get("id"), str):
                raise BenchmarkError(f"invalid case record at line {line_number}")
            if record["id"] in seen:
                raise BenchmarkError(f"duplicate case record: {record['id']}")
            if record.get("caseStatus") not in CASE_STATUSES:
                raise BenchmarkError(f"invalid case status for {record['id']}")
            manifest_case = manifest_by_id.get(record["id"])
            if manifest_case is None:
                raise BenchmarkError(f"case is not in the manifest: {record['id']}")
            if any(
                record.get(record_key) != manifest_case.get(manifest_key)
                for record_key, manifest_key in (
                    ("archiveId", "archiveId"),
                    ("sourcePath", "path"),
                    ("format", "format"),
                    ("inputBytes", "bytes"),
                    ("inputSha256", "sha256"),
                )
            ):
                raise BenchmarkError(f"input binding mismatch for {record['id']}")
            if not is_sha256(record.get("inputSha256")):
                raise BenchmarkError(f"invalid input SHA-256 for {record['id']}")
            if record.get("caseStatus") in {"processed", "expected-limit"}:
                required_fields = ("irSha256", "evidenceSha256", "canonicalDigest", "extractedInputSha256")
                if any(not is_sha256(record.get(field)) for field in required_fields) or record.get("schemaValidation") != "passed":
                    raise BenchmarkError(f"successful case lacks validated artifacts: {record['id']}")
                if record.get("extractedInputBytes") != record.get("inputBytes") or record.get("extractedInputSha256") != record.get("inputSha256"):
                    raise BenchmarkError(f"extracted input binding mismatch for {record['id']}")
                if record.get("evidenceInputSha256") != record.get("inputSha256") or record.get("evidenceConsumed") is not True:
                    raise BenchmarkError(f"evidence input binding mismatch for {record['id']}")
            seen.add(record["id"])
            records.append(record)
    expected_ids = {str(item["id"]) for item in manifest_cases}
    if seen != expected_ids:
        raise BenchmarkError(f"report case inventory mismatch: missing={sorted(expected_ids - seen)[:3]} extra={sorted(seen - expected_ids)[:3]}")
    case_counts = Counter(str(item["caseStatus"]) for item in records)
    unexpected = case_counts.get("conversion-failed", 0) + case_counts.get("infrastructure-error", 0)
    expected_report_status = "passed" if unexpected == 0 and len(records) == len(manifest_cases) else "failed"
    if report.get("status") != expected_report_status:
        raise BenchmarkError("report status does not match case outcomes")
    corpus = report.get("corpus")
    if not isinstance(corpus, dict) or corpus.get("expectedDocuments") != len(manifest_cases) or corpus.get("observedDocuments") != len(records):
        raise BenchmarkError("report corpus counts are inconsistent")
    if corpus.get("caseStatusCounts") != dict(sorted(case_counts.items())) or corpus.get("unexpectedFailureCount") != unexpected:
        raise BenchmarkError("report corpus status counts are inconsistent")
    representatives = report.get("representatives", [])
    if not isinstance(representatives, list):
        raise BenchmarkError("report representatives must be a list")
    for representative in representatives:
        if not isinstance(representative, dict):
            raise BenchmarkError("invalid representative record")
        if representative.get("id") not in seen:
            raise BenchmarkError("representative references an unknown case")
        for key, digest_key in (("document", "documentSha256"), ("evidence", "evidenceSha256")):
            relative_name = str(representative.get(key, ""))
            path = validate_relative_path(report_path.parent, relative_name, "representative")
            if not path.is_file() and "/" not in relative_name and "\\" not in relative_name:
                path = validate_relative_path(report_path.parent / "representatives", relative_name, "representative")
            if path.parent.resolve() != (report_path.parent / "representatives").resolve() or not is_sha256(representative.get(digest_key)) or not path.is_file() or sha256_file(path) != representative.get(digest_key):
                raise BenchmarkError(f"representative artifact mismatch: {path.name}")
    return {
        "status": "valid",
        "report": stable_repo_path(report_path),
        "sourceSha": source_sha,
        "documents": len(records),
        "caseStatusCounts": dict(sorted(case_counts.items())),
        "reportStatus": report.get("status"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    run = subparsers.add_parser("run", help="run the corpus and write a tracked compact report")
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--label", default="run")
    run.add_argument("--source-sha")
    verify = subparsers.add_parser("verify", help="verify a previously written report")
    verify.add_argument("--report", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.operation == "run":
            summary = run_benchmark(args.manifest, args.out, args.label, args.source_sha)
            print(json.dumps({"status": summary["status"], "runId": summary["runId"], "out": stable_repo_path(args.out.resolve())}, ensure_ascii=False))
            return 0 if summary["status"] == "passed" else 1
        result = verify_report(args.report)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {redact_runtime_text(exc)}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
