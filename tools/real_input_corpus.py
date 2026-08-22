"""Validate committed real-input regression manifests.

The corpus module validates only test inputs. Conversion outputs are created by
tests in temporary directories and are never written to a repository result
folder.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
from typing import Any
import zipfile


ALLOWED_FORMATS = {"docx", "xlsx"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class CorpusError(ValueError):
    """Raised when a regression manifest or archive is malformed."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_zip_member(name: str) -> str:
    if not isinstance(name, str) or not name or "\x00" in name:
        raise CorpusError(f"invalid ZIP member name: {name}")
    if "\\" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise CorpusError(f"non-canonical ZIP member name: {name}")
    canonical = name[:-1] if name.endswith("/") else name
    if not canonical:
        raise CorpusError("empty ZIP member name")
    parts = canonical.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise CorpusError(f"parent traversal in ZIP member: {name}")
    if PurePosixPath(canonical).as_posix() != canonical or posixpath.normpath(canonical) != canonical:
        raise CorpusError(f"non-canonical ZIP member path: {name}")
    return name


def expected_limit_match(manifest: dict[str, Any], format_name: str, diagnostics: list[dict[str, Any]]) -> bool:
    expected = manifest.get("expected", {})
    policies = expected.get("expectedLimitDiagnostics", []) if isinstance(expected, dict) else []
    for policy in policies:
        if not isinstance(policy, dict) or policy.get("format") != format_name:
            continue
        for diagnostic in diagnostics:
            if diagnostic.get("code") == policy.get("code") and str(policy.get("messageContains", "")) in str(diagnostic.get("message", "")):
                return True
    return False


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorpusError(f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CorpusError("manifest root must be an object")
    return value


def _relative_file(base: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise CorpusError(f"{label} path is empty")
    candidate = Path(value)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise CorpusError(f"{label} path escapes its manifest")
    resolved = (base / candidate).resolve()
    try:
        resolved.relative_to(base.resolve())
    except ValueError as exc:
        raise CorpusError(f"{label} path escapes its manifest") from exc
    return resolved


def _document_digest(archive: zipfile.ZipFile, member: str) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with archive.open(member, "r") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


def load_manifest(manifest_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate a corpus manifest and return normalized document records."""

    manifest_path = Path(manifest_path).resolve()
    manifest = _read_json(manifest_path)
    if manifest.get("schema") != "fdir/real-input-corpus-manifest" or manifest.get("version") != "1.0.0":
        raise CorpusError("unsupported corpus manifest schema")
    archives = manifest.get("archives")
    documents = manifest.get("documents")
    expected = manifest.get("expected")
    if not isinstance(archives, list) or not isinstance(documents, list) or not isinstance(expected, dict):
        raise CorpusError("manifest must contain archives, documents, and expected")
    if len(documents) != int(expected.get("documents", -1)):
        raise CorpusError("manifest document count does not match expected")

    archive_by_id: dict[str, tuple[dict[str, Any], Path]] = {}
    for record in archives:
        if not isinstance(record, dict) or not isinstance(record.get("id"), str) or record["id"] in archive_by_id:
            raise CorpusError("manifest contains a duplicate or invalid archive")
        archive_id = record["id"]
        archive_path = _relative_file(manifest_path.parent, record.get("path"), f"archive {archive_id}")
        if not archive_path.is_file() or int(record.get("bytes", -1)) != archive_path.stat().st_size:
            raise CorpusError(f"archive size mismatch: {archive_id}")
        if not SHA256_RE.fullmatch(str(record.get("sha256", "")).lower()) or sha256_file(archive_path) != str(record["sha256"]).lower():
            raise CorpusError(f"archive digest mismatch: {archive_id}")
        formats = record.get("formats")
        if not isinstance(formats, dict) or any(name not in ALLOWED_FORMATS for name in formats):
            raise CorpusError(f"archive format inventory is invalid: {archive_id}")
        archive_by_id[archive_id] = (record, archive_path)

    seen_ids: set[str] = set()
    seen_paths: set[tuple[str, str]] = set()
    normalized: list[dict[str, Any]] = []
    counts = Counter()
    for document in documents:
        if not isinstance(document, dict):
            raise CorpusError("manifest contains a non-object document")
        document_id = document.get("id")
        archive_id = document.get("archiveId")
        member = document.get("path")
        format_name = document.get("format")
        if not isinstance(document_id, str) or not document_id or document_id in seen_ids:
            raise CorpusError(f"duplicate document id: {document_id}")
        if archive_id not in archive_by_id or not isinstance(member, str):
            raise CorpusError(f"invalid archive reference for {document_id}")
        member = validate_zip_member(member)
        if member.endswith("/") or format_name not in ALLOWED_FORMATS or not member.lower().endswith(f".{format_name}"):
            raise CorpusError(f"invalid document format/path: {document_id}")
        key = (str(archive_id), member)
        if key in seen_paths:
            raise CorpusError(f"duplicate document path: {archive_id}/{member}")
        seen_ids.add(document_id)
        seen_paths.add(key)
        counts[str(format_name)] += 1
        normalized.append(dict(document))

    for archive_id, (record, archive_path) in archive_by_id.items():
        with zipfile.ZipFile(archive_path, "r") as archive:
            infos = archive.infolist()
            names = [validate_zip_member(info.filename) for info in infos]
            if len(names) != len(set(names)):
                raise CorpusError(f"duplicate ZIP member: {archive_id}")
            info_by_name = {info.filename: info for info in infos}
            actual = {name for name in names if not name.endswith("/") and Path(name).suffix.lower().lstrip(".") in ALLOWED_FORMATS}
            listed = {member for aid, member in seen_paths if aid == archive_id}
            if actual != listed or int(record.get("documentCount", -1)) != len(actual):
                raise CorpusError(f"document inventory mismatch: {archive_id}")
            actual_formats = Counter(Path(name).suffix.lower().lstrip(".") for name in actual)
            if any(actual_formats.get(name, 0) != int(record.get("formats", {}).get(name, 0)) for name in ALLOWED_FORMATS):
                raise CorpusError(f"archive format counts do not match: {archive_id}")
            for item in normalized:
                if item.get("archiveId") != archive_id:
                    continue
                member = str(item["path"])
                info = info_by_name[member]
                size, digest = _document_digest(archive, member)
                if int(item.get("bytes", -1)) != size or str(item.get("sha256", "")).lower() != digest:
                    raise CorpusError(f"document digest mismatch: {item.get('id')}")
                item["archivePath"] = archive_path
                item["memberName"] = member
                item["extractedBytes"] = size
                item["extractedSha256"] = digest

    expected_formats = expected.get("formats", {})
    if any(counts.get(name, 0) != int(value) for name, value in expected_formats.items()):
        raise CorpusError("manifest format counts do not match expected")
    normalized.sort(key=lambda item: str(item["id"]))
    return manifest, normalized


__all__ = ["CorpusError", "expected_limit_match", "load_manifest", "sha256_file", "validate_zip_member"]
