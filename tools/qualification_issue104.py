"""Independent multi-producer and hostile-corpus qualification for issue #104.

This runner is intentionally a separate evidence boundary from the existing
``tools/independent_corpus.py`` runner.  Its expected facts live in the issue
#104 corpus and are checked against source bytes before the public converter is
ever consulted.  The converter is a system under test; its output can agree
or disagree with an authored fact, but it can never create that fact.

The runner has two honest outcomes:

* ``passed`` means every required producer and external corpus binding is
  available and every required lane is green.
* ``failed`` with ``completionStatus == incomplete-strict-gate`` records the
  bounded local work while naming the missing evidence.  A grade-D declaration
  or the legacy manifest cannot turn that outcome into a pass.

No adapter module is imported here.  Conversion is invoked through the public
``convert_document.py`` command so the source-side oracle remains independent
of adapter implementation details.
"""

from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Iterable, Sequence
import urllib.error
import urllib.request
import uuid
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS_PATH = ROOT / "machine" / "qualification-issue-104-corpus.json"
DEFAULT_OUT_DIR = ROOT / "e2e" / ".run" / "qualification-issue-104"
CONVERTER_PATH = ROOT / "tools" / "convert_document.py"
LEGACY_MANIFEST_PATH = ROOT / "e2e" / "corpus" / "manifest.json"
SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

REPORT_NAMES = {
    "provenance": "corpus-provenance-independence.json",
    "coverage": "requirement-corpus-coverage.json",
    "producers": "multi-producer-results.json",
    "differential": "differential-adjudication.json",
    "metamorphic": "metamorphic-relations.json",
    "hostile": "hostile-resource-boundaries.json",
    "digests": "fixture-oracle-digest-manifest.json",
}

REQUIRED_REPORT_NAMES = tuple(REPORT_NAMES.values())
VALID_FORMATS = {"docx", "xlsx", "pdf", "markdown"}
VALID_GRADES = {"A", "B", "C", "D", "unavailable"}
QUALIFYING_GRADES = {"A", "B", "C"}

PRODUCER_REPORT_NAME = "producer-report.json"
PRODUCER_REPORT_SCHEMA = "fdir/qualification-producer-report"
PRODUCER_REPORT_VERSION = "1.0.0"
EVIDENCE_ID = "issue-104-independent-corpus"
REQUIREMENT_ID = "QUAL-104-INDEPENDENT-CORPUS"
BUNDLE_PREFIX = "artifacts/104"
DECLARED_INPUTS = (
    "machine/qualification-issue-104-corpus.json",
    "schemas/qualification-issue-104-corpus.schema.json",
    "schemas/qualification-issue-104-report.schema.json",
    "schemas/qualification-issue-104-summary.schema.json",
    "tools/qualification_issue104.py",
)
EVALUATOR_PATH = ROOT / "tools" / "validate_qualification_bundle.py"
SHARED_EVIDENCE_PATH = ROOT / "tools" / "qualification_evidence.py"


class QualificationError(RuntimeError):
    """Raised when the issue #104 corpus cannot be trusted."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QualificationError(f"cannot read JSON {path}: {exc}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _producer_input_paths(corpus_path: Path) -> list[Path]:
    paths = [ROOT / relative for relative in DECLARED_INPUTS]
    candidate = Path(corpus_path)
    paths[0] = candidate if candidate.is_absolute() else ROOT / candidate
    return paths


def _producer_input_digests(corpus_path: Path) -> tuple[list[str], list[str]]:
    digests: list[str] = []
    unavailable: list[str] = []
    for path in _producer_input_paths(corpus_path):
        if path.is_file():
            digests.append(_sha256_file(path))
        else:
            unavailable.append(str(path))
            digests.append(_sha256_bytes(f"missing:{path.as_posix()}".encode("utf-8")))
    return sorted(set(digests)), unavailable


def _producer_pointer(value: Any, pointer: str) -> Any:
    if pointer in {"", "/"}:
        return value
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise QualificationError(f"invalid producer JSON pointer: {pointer!r}")
    current = value
    for raw in pointer[1:].split("/"):
        segment = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit() and int(segment) < len(current):
            current = current[int(segment)]
        else:
            raise QualificationError(f"producer JSON pointer is missing: {pointer}")
    return current


def _producer_artifact_reference(local_path: Path, bundle_path: str, pointer: str) -> dict[str, Any]:
    value = _producer_pointer(_read_json(local_path), pointer)
    return {
        "path": bundle_path,
        "sha256": _sha256_file(local_path),
        "selector": {"kind": "json-pointer", "pointer": pointer},
        "selectedSha256": _sha256_text(_canonical(value)),
    }


def _append_producer_record(report: dict[str, Any], key: str, value: dict[str, Any]) -> str:
    records = report.setdefault(key, [])
    pointer = f"/{key}/{len(records)}"
    records.append(value)
    return pointer


def _source_sha() -> str | None:
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return None
    value = result.stdout.strip()
    return value if SOURCE_SHA_RE.fullmatch(value) else None


def _safe_repo_path(relative: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise QualificationError("source path must be a non-empty repository-relative string")
    candidate = (ROOT / relative).resolve()
    try:
        candidate.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise QualificationError(f"source path escapes repository: {relative!r}") from exc
    return candidate


def _source_units(path: Path) -> dict[str, bytes]:
    """Read source units without importing or executing an adapter."""

    if path.is_dir():
        units: dict[str, bytes] = {}
        for child in sorted(path.rglob("*")):
            if child.is_file():
                units[child.relative_to(path).as_posix()] = child.read_bytes()
        return units
    if not path.is_file():
        raise QualificationError(f"source fixture is missing: {path}")
    try:
        with zipfile.ZipFile(path) as archive:
            return {
                name: archive.read(name)
                for name in sorted(archive.namelist())
                if not name.endswith("/")
            }
    except (OSError, zipfile.BadZipFile):
        return {path.name: path.read_bytes()}


def _actual_source_digest(path: Path) -> dict[str, Any]:
    units = _source_units(path)
    members = [
        {"path": name, "bytes": len(payload), "sha256": _sha256_bytes(payload)}
        for name, payload in sorted(units.items())
    ]
    if path.is_dir():
        return {
            "algorithm": "sha256",
            "mode": "member-manifest",
            "members": members,
            "manifestSha256": _sha256_text(_canonical(members)),
        }
    item = members[0] if members else {"path": path.name, "bytes": 0, "sha256": _sha256_bytes(b"")}
    return {
        "algorithm": "sha256",
        "mode": "file",
        "bytes": item["bytes"],
        "sha256": item["sha256"],
    }


def _digest_mismatches(expected: dict[str, Any], actual: dict[str, Any]) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    if expected.get("algorithm") != "sha256":
        mismatches.append({"code": "DIGEST-ALGORITHM", "expected": "sha256", "actual": expected.get("algorithm")})
    if expected.get("mode") != actual.get("mode"):
        mismatches.append({"code": "DIGEST-MODE", "expected": expected.get("mode"), "actual": actual.get("mode")})
    if actual.get("mode") == "file":
        for field in ("bytes", "sha256"):
            if expected.get(field) != actual.get(field):
                mismatches.append({"code": "DIGEST-MISMATCH", "field": field, "expected": expected.get(field), "actual": actual.get(field)})
    else:
        expected_members = expected.get("members")
        actual_members = actual.get("members")
        if expected_members != actual_members:
            expected_by_path = {item.get("path"): item for item in expected_members or [] if isinstance(item, dict)}
            actual_by_path = {item.get("path"): item for item in actual_members or [] if isinstance(item, dict)}
            for name in sorted(set(expected_by_path) | set(actual_by_path)):
                if expected_by_path.get(name) != actual_by_path.get(name):
                    mismatches.append({
                        "code": "MEMBER-DIGEST-MISMATCH",
                        "path": name,
                        "expected": expected_by_path.get(name),
                        "actual": actual_by_path.get(name),
                    })
    return mismatches


def _load_corpus(path: Path = DEFAULT_CORPUS_PATH) -> dict[str, Any]:
    corpus = _read_json(Path(path))
    if not isinstance(corpus, dict):
        raise QualificationError("issue #104 corpus root must be an object")
    if corpus.get("schema") != "fdir/qualification-issue-104-corpus":
        raise QualificationError("issue #104 corpus schema is invalid")
    if corpus.get("version") != "1.0.0" or corpus.get("issueNumber") != 104:
        raise QualificationError("issue #104 corpus version or issue binding is invalid")
    required_top = {
        "reportNames", "oracle", "gradePolicy", "fixtures", "producerMatrix",
        "adjudicationGroups", "metamorphicRelations", "hostileCases",
        "requirements", "legacyManifestAudit", "officialCorpusPolicy",
    }
    missing_top = sorted(required_top - set(corpus))
    if missing_top:
        raise QualificationError("issue #104 corpus is missing: " + ", ".join(missing_top))
    if corpus.get("reportNames") != list(REQUIRED_REPORT_NAMES):
        raise QualificationError("issue #104 report names do not match the recovery contract")

    oracle = corpus["oracle"]
    if not isinstance(oracle, dict):
        raise QualificationError("issue #104 oracle declaration is invalid")
    for flag in ("expectedValuesAreRuntimeIndependent", "expectedFactsAreAuthored", "adapterOutputUsedForExpected", "adapterOutputUsedToCreateCorpus"):
        if oracle.get(flag) is not (flag.startswith("expected")):
            raise QualificationError(f"issue #104 oracle flag is unsafe: {flag}")
    if not isinstance(oracle.get("forbiddenDerivations"), list) or not oracle["forbiddenDerivations"]:
        raise QualificationError("issue #104 oracle has no forbidden derivation policy")

    policy = corpus["gradePolicy"]
    if not isinstance(policy, dict) or policy.get("minimumPassingGrade") != "C":
        raise QualificationError("issue #104 grade policy must require at least grade C")
    if policy.get("gradeDIsNonQualifying") is not True or policy.get("requiredOfficialArtifactsCannotBeReplacedByGradeD") is not True:
        raise QualificationError("issue #104 grade-D fail-closed policy is missing")
    if set(policy.get("requiredPassingGrades", [])) != QUALIFYING_GRADES:
        raise QualificationError("issue #104 grade policy has an invalid passing set")

    fixtures = corpus["fixtures"]
    if not isinstance(fixtures, list) or not fixtures:
        raise QualificationError("issue #104 has no fixtures")
    fixture_ids: set[str] = set()
    producer_ids_from_fixtures: set[str] = set()
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            raise QualificationError("issue #104 fixture entry is invalid")
        fixture_id = fixture.get("fixtureId")
        if not isinstance(fixture_id, str) or not fixture_id or fixture_id in fixture_ids:
            raise QualificationError(f"invalid or duplicate issue #104 fixture id: {fixture_id!r}")
        fixture_ids.add(fixture_id)
        if fixture.get("format") not in VALID_FORMATS:
            raise QualificationError(f"fixture {fixture_id} has an invalid format")
        if fixture.get("independenceGrade") not in VALID_GRADES:
            raise QualificationError(f"fixture {fixture_id} has an invalid independence grade")
        if fixture.get("independenceGrade") == "D":
            raise QualificationError(f"required local fixture {fixture_id} is grade D")
        producer = fixture.get("producerId")
        if not isinstance(producer, str) or not producer:
            raise QualificationError(f"fixture {fixture_id} has no producer")
        producer_ids_from_fixtures.add(producer)
        source = fixture.get("source")
        if not isinstance(source, dict) or source.get("type") not in {"repository-file", "repository-directory"}:
            raise QualificationError(f"fixture {fixture_id} source declaration is invalid")
        source_path = _safe_repo_path(source.get("path"))
        if not source_path.exists():
            raise QualificationError(f"fixture {fixture_id} source is missing: {source.get('path')}")
        provenance = fixture.get("provenance")
        if not isinstance(provenance, dict) or provenance.get("available") is not True:
            raise QualificationError(f"fixture {fixture_id} lacks available provenance")
        if not isinstance(provenance.get("sourceReference"), str) or not provenance["sourceReference"]:
            raise QualificationError(f"fixture {fixture_id} lacks provenance source reference")
        digest = fixture.get("sourceDigest")
        if not isinstance(digest, dict) or digest.get("algorithm") != "sha256":
            raise QualificationError(f"fixture {fixture_id} lacks an authored source digest")
        facts = fixture.get("expectedFacts")
        if not isinstance(facts, list) or not facts:
            raise QualificationError(f"fixture {fixture_id} lacks authored expected facts")
        fact_ids: set[str] = set()
        for fact in facts:
            if not isinstance(fact, dict) or not isinstance(fact.get("factId"), str) or not isinstance(fact.get("value"), (str, int, float, bool)):
                raise QualificationError(f"fixture {fixture_id} has an invalid authored fact")
            if fact["factId"] in fact_ids:
                raise QualificationError(f"fixture {fixture_id} has duplicate fact {fact['factId']}")
            fact_ids.add(fact["factId"])
            if fact.get("sourceContains") is not True:
                raise QualificationError(f"fixture {fixture_id} fact {fact['factId']} is not source-authored")
        oracle_digest = fixture.get("oracleDigest")
        if not isinstance(oracle_digest, str) or not SHA256_RE.fullmatch(oracle_digest):
            raise QualificationError(f"fixture {fixture_id} lacks an authored oracle digest")

    matrix = corpus["producerMatrix"]
    if not isinstance(matrix, list) or not matrix:
        raise QualificationError("issue #104 producer matrix is missing")
    matrix_ids: set[str] = set()
    for entry in matrix:
        if not isinstance(entry, dict) or not isinstance(entry.get("producerId"), str) or not entry["producerId"]:
            raise QualificationError("issue #104 producer matrix entry is invalid")
        producer_id = entry["producerId"]
        if producer_id in matrix_ids:
            raise QualificationError(f"duplicate issue #104 producer: {producer_id}")
        matrix_ids.add(producer_id)
        if entry.get("required") not in {True, False}:
            raise QualificationError(f"producer {producer_id} must declare required")
        if entry.get("format") not in VALID_FORMATS:
            raise QualificationError(f"producer {producer_id} has an invalid format")
        availability = entry.get("availability")
        if availability not in {"available", "missing"}:
            raise QualificationError(f"producer {producer_id} has an invalid availability")
        provenance = entry.get("provenance")
        if not isinstance(provenance, dict) or not provenance.get("kind") or not provenance.get("sourceReference"):
            raise QualificationError(f"producer {producer_id} has incomplete provenance")
        if availability == "available":
            if entry.get("fixtureId") not in fixture_ids:
                raise QualificationError(f"available producer {producer_id} has no fixture")
            if producer_id not in producer_ids_from_fixtures:
                raise QualificationError(f"producer {producer_id} does not bind a fixture owner")
        else:
            if entry.get("fixtureId") is not None:
                raise QualificationError(f"missing producer {producer_id} cannot bind a local fixture")
            if not isinstance(entry.get("missingReason"), str) or not entry["missingReason"]:
                raise QualificationError(f"missing producer {producer_id} has no explicit missing reason")
            if not isinstance(provenance.get("artifactPath"), str) or not provenance["artifactPath"]:
                raise QualificationError(f"missing producer {producer_id} has no artifact path")
            if _safe_repo_path(provenance["artifactPath"]).exists():
                raise QualificationError(f"producer {producer_id} is marked missing but its artifact exists")
            if entry.get("targetGrade") not in {"A", "B", "C"}:
                raise QualificationError(f"missing producer {producer_id} has no target grade")

    groups = corpus["adjudicationGroups"]
    if not isinstance(groups, list) or not groups:
        raise QualificationError("issue #104 has no differential adjudication groups")
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("groupId"), str) or not isinstance(group.get("producerIds"), list) or len(group["producerIds"]) < 2:
            raise QualificationError("issue #104 differential group is invalid")
        if not set(group["producerIds"]).issubset(matrix_ids):
            raise QualificationError(f"differential group {group.get('groupId')} references an unknown producer")
        if not group.get("factIds"):
            raise QualificationError(f"differential group {group.get('groupId')} has no adjudicated facts")

    relations = corpus["metamorphicRelations"]
    if not isinstance(relations, list) or not relations:
        raise QualificationError("issue #104 has no metamorphic relations")
    relation_ids: set[str] = set()
    for relation in relations:
        if not isinstance(relation, dict) or not isinstance(relation.get("relationId"), str) or relation["relationId"] in relation_ids:
            raise QualificationError("issue #104 metamorphic relation is invalid or duplicated")
        relation_ids.add(relation["relationId"])
        if relation.get("fixtureId") not in fixture_ids or not isinstance(relation.get("transform"), dict) or not relation.get("preservesFactIds"):
            raise QualificationError(f"metamorphic relation {relation.get('relationId')} is incomplete")

    hostile = corpus["hostileCases"]
    if not isinstance(hostile, list) or not hostile:
        raise QualificationError("issue #104 has no hostile/resource cases")
    hostile_ids: set[str] = set()
    for case in hostile:
        if not isinstance(case, dict) or not isinstance(case.get("caseId"), str) or case["caseId"] in hostile_ids:
            raise QualificationError("issue #104 hostile case is invalid or duplicated")
        hostile_ids.add(case["caseId"])
        if case.get("fixtureId") not in fixture_ids or not isinstance(case.get("mutation"), dict) or not isinstance(case.get("limits"), dict) or not isinstance(case.get("expected"), dict):
            raise QualificationError(f"hostile case {case.get('caseId')} is incomplete")

    requirements = corpus["requirements"]
    if not isinstance(requirements, list) or not requirements:
        raise QualificationError("issue #104 has no requirement matrix")
    requirement_ids: set[str] = set()
    for requirement in requirements:
        if not isinstance(requirement, dict) or not isinstance(requirement.get("requirementId"), str) or requirement["requirementId"] in requirement_ids:
            raise QualificationError("issue #104 requirement is invalid or duplicated")
        if requirement.get("required") is not True:
            raise QualificationError(f"issue #104 requirement {requirement.get('requirementId')} is not required")
        requirement_ids.add(requirement["requirementId"])

    if corpus["legacyManifestAudit"].get("expectedIndependenceGrade") != "D":
        raise QualificationError("legacy manifest audit must classify self-declared evidence as grade D")
    official_policy = corpus["officialCorpusPolicy"]
    if official_policy.get("requiredAtQualificationTime") is not True or official_policy.get("missingArtifactIsUnmet") is not True or official_policy.get("gradeDSubstitutionForbidden") is not True:
        raise QualificationError("official corpus fail-closed policy is incomplete")
    return corpus


def _source_fact_text(units: dict[str, bytes], member: str | None) -> str:
    selected = units if member is None else {member: units.get(member, b"")}
    return "\n".join(
        payload.decode("utf-8", errors="replace") + "\n" + payload.decode("latin-1", errors="replace")
        for payload in selected.values()
    )


def _source_fact_match(units: dict[str, bytes], fact: dict[str, Any]) -> dict[str, Any]:
    member = fact.get("sourceMember")
    value = str(fact.get("value"))
    member_exists = member is None or member in units
    text = _source_fact_text(units, member)
    contains = member_exists and value in text
    expected = fact.get("sourceContains") is True
    return {
        "factId": fact.get("factId"),
        "member": member,
        "value": fact.get("value"),
        "memberExists": member_exists,
        "contains": contains,
        "expected": expected,
        "status": "passed" if contains == expected else "failed",
    }


def _output_scalars(value: Any) -> list[str]:
    result: list[str] = []
    if isinstance(value, str):
        result.append(value)
    elif isinstance(value, bool):
        result.append(str(value).lower())
    elif isinstance(value, (int, float)):
        result.append(str(value))
    elif isinstance(value, dict):
        for child in value.values():
            result.extend(_output_scalars(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(_output_scalars(child))
    return result


def _output_fact_match(scalars: Iterable[str], fact: dict[str, Any]) -> dict[str, Any]:
    value = str(fact.get("value"))
    contains = any(value in scalar for scalar in scalars)
    expected = fact.get("outputContains")
    if expected is None:
        return {"factId": fact.get("factId"), "checked": False, "status": "not-required"}
    return {
        "factId": fact.get("factId"),
        "value": fact.get("value"),
        "contains": contains,
        "expected": expected is True,
        "status": "passed" if contains == (expected is True) else "failed",
    }


def _materialize_fixture(fixture: dict[str, Any], directory: Path) -> Path:
    source = fixture["source"]
    source_path = _safe_repo_path(source["path"])
    directory.mkdir(parents=True, exist_ok=True)
    suffix = ".md" if fixture["format"] == "markdown" else f".{fixture['format']}"
    target = directory / f"{fixture['fixtureId']}{suffix}"
    if source["type"] == "repository-file":
        shutil.copyfile(source_path, target)
        return target
    if not source_path.is_dir():
        raise QualificationError(f"fixture source is not a directory: {source['path']}")
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for child in sorted(source_path.rglob("*")):
            if not child.is_file():
                continue
            name = child.relative_to(source_path).as_posix()
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, child.read_bytes())
    return target


def _run_converter(input_path: Path, format_name: str, output_dir: Path, limits: dict[str, Any] | None = None) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:12]
    output = output_dir / f"{input_path.stem}-{token}.json"
    evidence = output_dir / f"{input_path.stem}-{token}.evidence.json"
    command = [
        sys.executable,
        str(CONVERTER_PATH),
        "convert",
        str(input_path),
        "--format",
        format_name,
        "--out",
        str(output),
        "--evidence",
        str(evidence),
    ]
    for name, value in sorted((limits or {}).items()):
        option = "--" + "".join(f"-{char.lower()}" if char.isupper() else char for char in name)
        command.extend([option, str(value)])
    started = time.monotonic()
    try:
        process = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        process = None
        timed_out = True
        stdout = str(exc.stdout or "")
        stderr = str(exc.stderr or "")
    elapsed = round(time.monotonic() - started, 6)
    stdout = process.stdout if process is not None else stdout
    stderr = process.stderr if process is not None else stderr
    document: dict[str, Any] | None = None
    evidence_value: dict[str, Any] | None = None
    if output.is_file():
        try:
            candidate = _read_json(output)
            if isinstance(candidate, dict):
                document = candidate
        except QualificationError:
            document = None
    if evidence.is_file():
        try:
            candidate = _read_json(evidence)
            if isinstance(candidate, dict):
                evidence_value = candidate
        except QualificationError:
            evidence_value = None
    return {
        "command": command,
        "returnCode": None if process is None else process.returncode,
        "timedOut": timed_out,
        "elapsedSeconds": elapsed,
        "stdout": stdout[-4000:],
        "stderr": stderr[-4000:],
        "outputPath": str(output),
        "evidencePath": str(evidence),
        "document": document,
        "evidence": evidence_value,
    }


def _fixture_result(fixture: dict[str, Any], input_path: Path, run: dict[str, Any]) -> dict[str, Any]:
    units = _source_units(input_path)
    source_facts = [_source_fact_match(units, fact) for fact in fixture["expectedFacts"]]
    document = run.get("document")
    scalars = _output_scalars(document) if isinstance(document, dict) else []
    output_facts = [_output_fact_match(scalars, fact) for fact in fixture["expectedFacts"]]
    source_digest = _actual_source_digest(_safe_repo_path(fixture["source"]["path"]))
    digest_mismatches = _digest_mismatches(fixture["sourceDigest"], source_digest)
    oracle_digest = _sha256_text(_canonical(fixture["expectedFacts"]))
    oracle_digest_match = oracle_digest == fixture["oracleDigest"]
    source_mismatches = [item for item in source_facts if item["status"] != "passed"]
    output_mismatches = [item for item in output_facts if item.get("status") == "failed"]
    conversion_status = (document or {}).get("conversion", {}).get("status") if isinstance(document, dict) else None
    converter_ok = not run["timedOut"] and isinstance(document, dict) and conversion_status != "failed"
    return {
        "fixtureId": fixture["fixtureId"],
        "scenarioId": fixture["scenarioId"],
        "format": fixture["format"],
        "producerId": fixture["producerId"],
        "independenceGrade": fixture["independenceGrade"],
        "inputPath": str(input_path),
        "sourceFacts": source_facts,
        "sourceMismatches": source_mismatches,
        "outputFacts": output_facts,
        "outputMismatches": output_mismatches,
        "converter": {
            "returnCode": run["returnCode"],
            "timedOut": run["timedOut"],
            "conversionStatus": conversion_status,
            "evidence": run.get("evidence"),
            "stderr": run.get("stderr"),
        },
        "sourceDigest": source_digest,
        "digestMismatches": digest_mismatches,
        "oracleDigest": {"declared": fixture["oracleDigest"], "actual": oracle_digest, "matches": oracle_digest_match},
        "status": "passed" if not source_mismatches and not output_mismatches and not digest_mismatches and oracle_digest_match and converter_ok else "failed",
    }


def _legacy_manifest_audit(corpus: dict[str, Any]) -> dict[str, Any]:
    try:
        manifest = _read_json(LEGACY_MANIFEST_PATH)
    except QualificationError as exc:
        return {
            "manifestPath": "e2e/corpus/manifest.json",
            "status": "failed",
            "grade": "D",
            "reasons": [f"legacy manifest could not be read: {exc}"],
        }
    cases = list(manifest.get("cases", [])) + list(manifest.get("negativeCases", [])) if isinstance(manifest, dict) else []
    reasons = list(corpus["legacyManifestAudit"]["nonQualifyingReasons"])
    reasons.append("legacy manifest does not contain an authored expected-fact digest")
    self_declared = manifest.get("independent") is True if isinstance(manifest, dict) else False
    producer_matrix_present = isinstance(manifest.get("producerMatrix"), list) if isinstance(manifest, dict) else False
    expected_fact_oracle_present = isinstance(manifest.get("expectedFacts"), list) if isinstance(manifest, dict) else False
    scope_assessment = {
        "selfDeclared": self_declared,
        "small": len(cases) <= 16,
        "generic": not producer_matrix_present and not expected_fact_oracle_present,
        "caseClassCount": len({item.get("caseClass") for item in cases if isinstance(item, dict) and item.get("caseClass")}),
    }
    return {
        "manifestPath": "e2e/corpus/manifest.json",
        "selfDeclaredIndependent": self_declared,
        "sourceOfTruth": manifest.get("sourceOfTruth") if isinstance(manifest, dict) else None,
        "caseCount": len(cases),
        "scopeAssessment": scope_assessment,
        "producerMatrixPresent": producer_matrix_present,
        "expectedFactOraclePresent": expected_fact_oracle_present,
        "grade": "D",
        "reasons": reasons,
        "status": "failed",
    }


def _runner_import_audit() -> dict[str, Any]:
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    adapter_imports = sorted(name for name in imported if "adapter_" in name or name.endswith("adapter_common"))
    return {
        "adapterImports": adapter_imports,
        "independent": not adapter_imports,
        "sourceReader": "runner-owned-byte-reader",
        "expectedFactsFromAdapterOutput": False,
    }


def _base_report(kind: str, source_sha: str | None, corpus_sha: str | None) -> dict[str, Any]:
    return {
        "schema": "fdir/qualification-issue-104-report",
        "version": "1.0.0",
        "issueNumber": 104,
        "reportKind": kind,
        "sourceSha": source_sha,
        "corpusSha256": corpus_sha,
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "status": "failed",
        "completionStatus": "incomplete-strict-gate",
        "implementedScope": [
            "authored source facts and oracle digests",
            "source/member provenance and grade audit",
            "producer availability matrix",
            "differential adjudication records",
            "metamorphic relation execution",
            "hostile and resource-boundary execution",
        ],
        "unmetRequirements": [],
        "assertions": [],
    }


def _fixture_report(source_sha: str | None, corpus_sha: str | None, results: list[dict[str, Any]]) -> dict[str, Any]:
    report = _base_report("fixture-oracle-digest-manifest", source_sha, corpus_sha)
    report["fixtures"] = results
    report["sourceFixtureCount"] = len(results)
    report["passedFixtureCount"] = sum(item["status"] == "passed" for item in results)
    report["mismatchCount"] = sum(
        len(item["sourceMismatches"]) + len(item["outputMismatches"]) + len(item["digestMismatches"]) + (0 if item["oracleDigest"]["matches"] else 1)
        for item in results
    )
    report["sectionStatus"] = "passed" if report["mismatchCount"] == 0 else "failed"
    report["assertions"] = [
        {"id": "all-authored-source-digests-match", "status": "passed" if all(not item["digestMismatches"] for item in results) else "failed"},
        {"id": "all-authored-oracle-digests-match", "status": "passed" if all(item["oracleDigest"]["matches"] for item in results) else "failed"},
        {"id": "source-side-facts-are-executed", "status": "passed" if all(not item["sourceMismatches"] for item in results) else "failed"},
    ]
    return report


def _provenance_report(corpus: dict[str, Any], source_sha: str | None, corpus_sha: str | None, fixture_results: list[dict[str, Any]]) -> dict[str, Any]:
    report = _base_report("corpus-provenance-independence", source_sha, corpus_sha)
    legacy = _legacy_manifest_audit(corpus)
    fixture_by_id = {item["fixtureId"]: item for item in fixture_results}
    dedicated = []
    for fixture in corpus["fixtures"]:
        result = fixture_by_id[fixture["fixtureId"]]
        dedicated.append({
            "fixtureId": fixture["fixtureId"],
            "producerId": fixture["producerId"],
            "grade": fixture["independenceGrade"],
            "sourceReference": fixture["provenance"]["sourceReference"],
            "digestStatus": "passed" if not result["digestMismatches"] else "failed",
            "oracleStatus": "passed" if result["oracleDigest"]["matches"] else "failed",
            "status": "passed" if fixture["independenceGrade"] in QUALIFYING_GRADES and not result["digestMismatches"] and result["oracleDigest"]["matches"] else "failed",
        })
    available_grades = sorted({item["grade"] for item in dedicated if item["status"] == "passed"})
    grade_gate = {
        "minimumPassingGrade": corpus["gradePolicy"]["minimumPassingGrade"],
        "availableQualifyingGrades": available_grades,
        "legacyGradeDRejected": legacy["grade"] == "D" and legacy["status"] == "failed",
        "gradeDOnlyWouldPass": False,
        "passes": bool(available_grades) and bool(set(available_grades) & QUALIFYING_GRADES),
    }
    report["legacyManifestAudit"] = legacy
    report["dedicatedFixtures"] = dedicated
    report["gradeGate"] = grade_gate
    report["sectionStatus"] = "passed" if grade_gate["passes"] else "failed"
    report["assertions"] = [
        {"id": "legacy-self-declaration-is-grade-d", "status": "passed" if legacy["grade"] == "D" else "failed"},
        {"id": "grade-d-is-not-a-passing-grade", "status": "passed" if grade_gate["gradeDOnlyWouldPass"] is False else "failed"},
        {"id": "available-non-d-fixture-exists", "status": "passed" if grade_gate["passes"] else "failed"},
    ]
    return report


def _producer_report(corpus: dict[str, Any], source_sha: str | None, corpus_sha: str | None, fixture_results: list[dict[str, Any]], *, fetch_external: bool = False, acquisition_dir: Path | None = None) -> dict[str, Any]:
    report = _base_report("multi-producer-results", source_sha, corpus_sha)
    by_fixture = {item["fixtureId"]: item for item in fixture_results}
    entries = []
    required_unavailable = []
    for producer in corpus["producerMatrix"]:
        if producer["availability"] == "missing":
            artifact_path = _safe_repo_path(producer["provenance"]["artifactPath"])
            acquisition: dict[str, Any] = {"attempted": fetch_external, "status": "unavailable"}
            artifact_url = producer["provenance"].get("artifactUrl")
            if fetch_external and artifact_url:
                target_dir = acquisition_dir or (ROOT / "e2e" / ".run" / "qualification-issue-104-external")
                target_dir.mkdir(parents=True, exist_ok=True)
                downloaded = target_dir / f"{producer['producerId']}.artifact"
                try:
                    request = urllib.request.Request(str(artifact_url), headers={"User-Agent": "fdir-qualification-issue-104"})
                    with urllib.request.urlopen(request, timeout=5) as response:
                        payload = response.read(8 * 1024 * 1024 + 1)
                    if len(payload) > 8 * 1024 * 1024:
                        raise QualificationError("external artifact exceeds the 8 MiB acquisition bound")
                    downloaded.write_bytes(payload)
                    acquisition.update({"status": "retrieved-but-unbound", "path": str(downloaded), "bytes": len(payload), "sha256": _sha256_bytes(payload), "reason": "artifact retrieval alone cannot create authored expected facts"})
                except (OSError, urllib.error.URLError, QualificationError) as exc:
                    acquisition.update({"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"})
            elif fetch_external:
                acquisition["reason"] = "no artifactUrl is declared; official source reference is not an executable fixture"
            else:
                acquisition["reason"] = "no checked-in external artifact and runtime fetch is disabled by corpus policy"
            entry = {
                "producerId": producer["producerId"],
                "format": producer["format"],
                "required": producer["required"],
                "availability": "missing",
                "targetGrade": producer["targetGrade"],
                "sourceReference": producer["provenance"]["sourceReference"],
                "artifactPath": producer["provenance"]["artifactPath"],
                "artifactExistsAtRuntime": artifact_path.exists(),
                "status": "unavailable",
                "missingReason": producer["missingReason"],
                "acquisition": acquisition,
            }
            if producer["required"]:
                required_unavailable.append(producer["producerId"])
        else:
            fixture_result = by_fixture[producer["fixtureId"]]
            qualified = fixture_result["status"] == "passed" and producer["independenceGrade"] in QUALIFYING_GRADES
            entry = {
                "producerId": producer["producerId"],
                "format": producer["format"],
                "required": producer["required"],
                "availability": "available",
                "fixtureId": producer["fixtureId"],
                "independenceGrade": producer["independenceGrade"],
                "sourceReference": producer["provenance"]["sourceReference"],
                "fixtureStatus": fixture_result["status"],
                "status": "qualified" if qualified else "failed",
            }
            if producer["required"] and not qualified:
                required_unavailable.append(producer["producerId"])
        entries.append(entry)
    report["producerMatrix"] = entries
    report["requiredProducerCount"] = sum(item["required"] is True for item in entries)
    report["requiredQualifiedCount"] = sum(item["required"] is True and item["status"] == "qualified" for item in entries)
    report["requiredUnavailable"] = required_unavailable
    report["sectionStatus"] = "passed" if not required_unavailable else "failed"
    report["assertions"] = [
        {"id": "producer-matrix-is-explicit", "status": "passed" if entries else "failed"},
        {"id": "all-required-producers-available", "status": "passed" if not required_unavailable else "failed"},
        {"id": "missing-producers-have-reasons", "status": "passed" if all(item.get("missingReason") for item in entries if item["status"] == "unavailable") else "failed"},
    ]
    return report


def _producer_envelope(
    corpus: dict[str, Any] | None,
    reports: dict[str, dict[str, Any]],
    *,
    corpus_path: Path,
    out_dir: Path,
    source_sha: str | None,
) -> dict[str, Any]:
    """Build the closed producer-report document from semantic report records.

    The hand-authored corpus supplies authority values.  Converter results and
    the explicit producer/differential/metamorphic/hostile reports supply
    actual values.  Missing official producers are recorded as negative
    evidence and uncovered requirements, so the envelope is blocked rather
    than promoted by the aggregate report status.
    """

    provenance_name = REPORT_NAMES["provenance"]
    coverage_name = REPORT_NAMES["coverage"]
    producers_name = REPORT_NAMES["producers"]
    differential_name = REPORT_NAMES["differential"]
    metamorphic_name = REPORT_NAMES["metamorphic"]
    hostile_name = REPORT_NAMES["hostile"]
    digests_name = REPORT_NAMES["digests"]
    report_kinds = {
        provenance_name: "corpus-provenance-independence",
        coverage_name: "requirement-corpus-coverage",
        producers_name: "multi-producer-results",
        differential_name: "differential-adjudication",
        metamorphic_name: "metamorphic-relations",
        hostile_name: "hostile-resource-boundaries",
        digests_name: "fixture-oracle-digest-manifest",
    }

    def ensure_report(name: str) -> dict[str, Any]:
        report = reports.get(name)
        if not isinstance(report, dict):
            report = _base_report(report_kinds[name], source_sha, None)
            reports[name] = report
        return report

    def records(name: str, key: str) -> list[dict[str, Any]]:
        return ensure_report(name).setdefault(key, [])

    support_report = ensure_report(coverage_name)

    def add_support(case_id: str, assertion_id: str, actual: Any, target: dict[str, Any]) -> str:
        return _append_producer_record(
            support_report,
            "producerSupport",
            {
                "assertionId": assertion_id,
                "caseId": case_id,
                "actual": actual,
                "target": target,
                "status": "passed",
            },
        )

    case_specs: list[dict[str, Any]] = []

    def add_case(
        *,
        case_id: str,
        classification: str,
        assertion_type: str,
        authority_report: str,
        authority_pointer: str,
        actual_report: str,
        actual_pointer: str,
        target: dict[str, Any],
        diagnostic: dict[str, str],
    ) -> None:
        assertion_id = f"issue-104:{case_id}"
        input_pointer = _append_producer_record(
            support_report,
            "producerInput",
            {"caseId": case_id, "target": target, "status": "passed"},
        )
        actual_value = _producer_pointer(ensure_report(actual_report), actual_pointer)
        support_case_pointer = add_support(case_id, case_id, actual_value, target)
        support_assertion_pointer = add_support(case_id, assertion_id, actual_value, target)
        case_specs.append({
            "caseId": case_id,
            "classification": classification,
            "assertionType": assertion_type,
            "assertionId": assertion_id,
            "authorityReport": authority_report,
            "authorityPointer": authority_pointer,
            "actualReport": actual_report,
            "actualPointer": actual_pointer,
            "inputPointer": input_pointer,
            "supportCasePointer": support_case_pointer,
            "supportAssertionPointer": support_assertion_pointer,
            "target": target,
            "diagnostic": diagnostic,
        })

    uncovered: list[str] = []
    if isinstance(source_sha, str) and SOURCE_SHA_RE.fullmatch(source_sha):
        envelope_source_sha = source_sha
    else:
        envelope_source_sha = "0" * 40
        uncovered.append("source SHA is unavailable; envelope is not commit-bound")

    if isinstance(corpus, dict):
        fixture_results = {
            item.get("fixtureId"): item
            for item in records(digests_name, "fixtures")
            if isinstance(item, dict) and isinstance(item.get("fixtureId"), str)
        }
        authority_facts = records(provenance_name, "producerAuthority")
        actual_facts = records(digests_name, "producerActual")
        authority_oracles = records(provenance_name, "producerOracleAuthority")
        actual_oracles = records(digests_name, "producerOracleActual")

        for fixture in corpus.get("fixtures", []):
            fixture_id = fixture["fixtureId"]
            result = fixture_results.get(fixture_id, {})
            output_facts = {
                item.get("factId"): item
                for item in result.get("outputFacts", [])
                if isinstance(item, dict) and isinstance(item.get("factId"), str)
            }
            for fact in fixture.get("expectedFacts", []):
                fact_id = fact["factId"]
                expected = {
                    "value": fact.get("value"),
                    "contains": fact.get("outputContains") is True,
                }
                observed = output_facts.get(fact_id, {})
                actual = {
                    "value": observed.get("value"),
                    "contains": observed.get("contains") is True,
                }
                target = {"fixtureId": fixture_id, "producerId": fixture["producerId"], "factId": fact_id}
                authority_index = len(authority_facts)
                authority_facts.append({"caseId": fact_id, "expected": expected, "target": target, "status": "passed"})
                actual_index = len(actual_facts)
                actual_facts.append({"caseId": fact_id, "actual": actual, "target": target, "status": "passed" if actual == expected else "failed"})
                add_case(
                    case_id=f"fact-{fact_id}",
                    classification="positive",
                    assertion_type="corpus-independence",
                    authority_report=provenance_name,
                    authority_pointer=f"/producerAuthority/{authority_index}/expected",
                    actual_report=digests_name,
                    actual_pointer=f"/producerActual/{actual_index}/actual",
                    target=target,
                    diagnostic={"code": "ISSUE_104_AUTHORED_FACT", "message": "source-side authored fact is compared with converter output"},
                )

            oracle = result.get("oracleDigest", {})
            expected_oracle = {"matches": True}
            actual_oracle = {"matches": oracle.get("matches") is True}
            oracle_target = {"fixtureId": fixture_id, "oracleDigest": fixture.get("oracleDigest")}
            authority_index = len(authority_oracles)
            authority_oracles.append({"caseId": f"oracle-{fixture_id}", "expected": expected_oracle, "target": oracle_target, "status": "passed"})
            actual_index = len(actual_oracles)
            actual_oracles.append({"caseId": f"oracle-{fixture_id}", "actual": actual_oracle, "target": oracle_target, "status": "passed" if actual_oracle == expected_oracle else "failed"})
            add_case(
                case_id=f"oracle-{fixture_id}",
                classification="positive",
                assertion_type="corpus-independence",
                authority_report=provenance_name,
                authority_pointer=f"/producerOracleAuthority/{authority_index}/expected",
                actual_report=digests_name,
                actual_pointer=f"/producerOracleActual/{actual_index}/actual",
                target=oracle_target,
                diagnostic={"code": "ISSUE_104_ORACLE_DIGEST", "message": "authored oracle digest agrees with independently recomputed fact data"},
            )

        producer_entries = {
            item.get("producerId"): item
            for item in records(producers_name, "producerMatrix")
            if isinstance(item, dict) and isinstance(item.get("producerId"), str)
        }
        authority_producers = records(provenance_name, "producerAvailabilityAuthority")
        actual_producers = records(producers_name, "producerAvailabilityActual")
        matrix_by_id = {
            item.get("producerId"): item
            for item in corpus.get("producerMatrix", [])
            if isinstance(item, dict) and isinstance(item.get("producerId"), str)
        }
        for producer_id, producer in matrix_by_id.items():
            entry = producer_entries.get(producer_id, {})
            target = {"producerId": producer_id, "format": producer.get("format")}
            if producer.get("availability") == "missing":
                expected = {"required": producer.get("required") is True, "availability": "available", "targetGrade": producer.get("targetGrade")}
                actual = {
                    "required": entry.get("required") is True,
                    "availability": entry.get("availability"),
                    "status": entry.get("status"),
                    "missingReason": entry.get("missingReason"),
                }
                uncovered.append(f"{producer_id}: required official producer unavailable ({producer.get('missingReason')})")
                classification = "negative"
                assertion_type = "mutation-killed"
            else:
                expected = {"required": producer.get("required") is True, "availability": "available", "independenceGrade": producer.get("independenceGrade")}
                actual = {
                    "required": entry.get("required") is True,
                    "availability": entry.get("availability"),
                    "independenceGrade": entry.get("independenceGrade"),
                }
                classification = "positive"
                assertion_type = "corpus-independence"
            authority_index = len(authority_producers)
            authority_producers.append({"caseId": f"producer-{producer_id}", "expected": expected, "target": target, "status": "passed"})
            actual_index = len(actual_producers)
            actual_producers.append({"caseId": f"producer-{producer_id}", "actual": actual, "target": target, "status": "passed" if actual == expected else "unavailable" if producer.get("availability") == "missing" else "failed"})
            add_case(
                case_id=f"producer-{producer_id}",
                classification=classification,
                assertion_type=assertion_type,
                authority_report=provenance_name,
                authority_pointer=f"/producerAvailabilityAuthority/{authority_index}/expected",
                actual_report=producers_name,
                actual_pointer=f"/producerAvailabilityActual/{actual_index}/actual",
                target=target,
                diagnostic={"code": "ISSUE_104_PRODUCER_AVAILABILITY", "message": "producer availability is adjudicated from the declared producer matrix"},
            )

        authority_groups = records(provenance_name, "producerDifferentialAuthority")
        actual_groups = records(differential_name, "producerDifferentialActual")
        differential_groups = {item.get("groupId"): item for item in records(differential_name, "groups") if isinstance(item, dict)}
        for group in corpus.get("adjudicationGroups", []):
            group_id = group["groupId"]
            observed = differential_groups.get(group_id, {})
            expected_adjudication = "unavailable" if any(matrix_by_id.get(pid, {}).get("availability") == "missing" for pid in group.get("producerIds", [])) else "agreement"
            expected = {"required": group.get("required") is True, "adjudication": expected_adjudication}
            actual = {"required": observed.get("required") is True, "adjudication": observed.get("adjudication")}
            target = {"groupId": group_id, "format": group.get("format"), "factIds": group.get("factIds", [])}
            authority_index = len(authority_groups)
            authority_groups.append({"caseId": f"differential-{group_id}", "expected": expected, "target": target, "status": "passed"})
            actual_index = len(actual_groups)
            actual_groups.append({"caseId": f"differential-{group_id}", "actual": actual, "target": target, "status": "passed" if actual == expected else "failed"})
            add_case(
                case_id=f"differential-{group_id}",
                classification="differential",
                assertion_type="differential-equality",
                authority_report=provenance_name,
                authority_pointer=f"/producerDifferentialAuthority/{authority_index}/expected",
                actual_report=differential_name,
                actual_pointer=f"/producerDifferentialActual/{actual_index}/actual",
                target=target,
                diagnostic={"code": "ISSUE_104_DIFFERENTIAL", "message": "independent producer members are compared without promoting missing members"},
            )

        authority_relations = records(provenance_name, "producerMetamorphicAuthority")
        actual_relations = records(metamorphic_name, "producerMetamorphicActual")
        metamorphic_results = {item.get("relationId"): item for item in records(metamorphic_name, "relations") if isinstance(item, dict)}
        for relation in corpus.get("metamorphicRelations", []):
            relation_id = relation["relationId"]
            observed = metamorphic_results.get(relation_id, {})
            expected = {"preserved": relation.get("required") is True and bool(relation.get("preservesFactIds"))}
            actual = {"preserved": observed.get("status") == "passed"}
            target = {"relationId": relation_id, "fixtureId": relation.get("fixtureId"), "transform": relation.get("transform")}
            authority_index = len(authority_relations)
            authority_relations.append({"caseId": f"metamorphic-{relation_id}", "expected": expected, "target": target, "status": "passed"})
            actual_index = len(actual_relations)
            actual_relations.append({"caseId": f"metamorphic-{relation_id}", "actual": actual, "target": target, "status": "passed" if actual == expected else "failed"})
            add_case(
                case_id=f"metamorphic-{relation_id}",
                classification="metamorphic",
                assertion_type="metamorphic-equality",
                authority_report=provenance_name,
                authority_pointer=f"/producerMetamorphicAuthority/{authority_index}/expected",
                actual_report=metamorphic_name,
                actual_pointer=f"/producerMetamorphicActual/{actual_index}/actual",
                target=target,
                diagnostic={"code": "ISSUE_104_METAMORPHIC", "message": "the authored fact set is preserved across the declared input transformation"},
            )

        authority_hostile = records(provenance_name, "producerHostileAuthority")
        actual_hostile = records(hostile_name, "producerHostileActual")
        hostile_results = {item.get("caseId"): item for item in records(hostile_name, "cases") if isinstance(item, dict)}
        for hostile in corpus.get("hostileCases", []):
            case_id = hostile["caseId"]
            observed = hostile_results.get(case_id, {})
            expected = {"detected": hostile.get("required") is True}
            actual = {"detected": observed.get("detected") is True}
            target = {"caseId": case_id, "fixtureId": hostile.get("fixtureId"), "limits": hostile.get("limits", {})}
            authority_index = len(authority_hostile)
            authority_hostile.append({"caseId": f"hostile-{case_id}", "expected": expected, "target": target, "status": "passed"})
            actual_index = len(actual_hostile)
            actual_hostile.append({"caseId": f"hostile-{case_id}", "actual": actual, "target": target, "status": "passed" if actual == expected else "failed"})
            add_case(
                case_id=f"hostile-{case_id}",
                classification="hostile",
                assertion_type="differential-equality",
                authority_report=provenance_name,
                authority_pointer=f"/producerHostileAuthority/{authority_index}/expected",
                actual_report=hostile_name,
                actual_pointer=f"/producerHostileActual/{actual_index}/actual",
                target=target,
                diagnostic={"code": "ISSUE_104_HOSTILE", "message": "hostile input is checked against the authored fail-closed expectation"},
            )
    else:
        authority = records(provenance_name, "producerSetupAuthority")
        actual = records(producers_name, "producerSetupActual")
        target = {"lane": "issue-104", "evidence": "independent corpus setup"}
        authority_index = len(authority)
        actual_index = len(actual)
        authority.append({"caseId": "setup-unavailable", "expected": {"available": True}, "target": target, "status": "passed"})
        actual.append({"caseId": "setup-unavailable", "actual": {"available": False}, "target": target, "status": "unavailable"})
        uncovered.append("issue-104 authored corpus is unavailable")
        add_case(
            case_id="setup-unavailable",
            classification="negative",
            assertion_type="mutation-killed",
            authority_report=provenance_name,
            authority_pointer=f"/producerSetupAuthority/{authority_index}/expected",
            actual_report=producers_name,
            actual_pointer=f"/producerSetupActual/{actual_index}/actual",
            target=target,
            diagnostic={"code": "ISSUE_104_SETUP_UNAVAILABLE", "message": "the independent corpus could not be loaded"},
        )

    for name in REQUIRED_REPORT_NAMES:
        _write_json(out_dir / name, ensure_report(name))

    input_digests, unavailable_inputs = _producer_input_digests(corpus_path)
    uncovered.extend(unavailable_inputs)
    for name in REQUIRED_REPORT_NAMES:
        if not (out_dir / name).is_file():
            uncovered.append(f"semantic report unavailable: {name}")

    producer_cases: list[dict[str, Any]] = []
    producer_assertions: list[dict[str, Any]] = []
    failures = 0
    for spec in case_specs:
        try:
            authority_local = out_dir / spec["authorityReport"]
            actual_local = out_dir / spec["actualReport"]
            input_local = out_dir / coverage_name
            support_local = out_dir / coverage_name
            authority_ref = _producer_artifact_reference(authority_local, f"{BUNDLE_PREFIX}/{spec['authorityReport']}", spec["authorityPointer"])
            actual_ref = _producer_artifact_reference(actual_local, f"{BUNDLE_PREFIX}/{spec['actualReport']}", spec["actualPointer"])
            input_ref = _producer_artifact_reference(input_local, f"{BUNDLE_PREFIX}/{coverage_name}", spec["inputPointer"])
            support_case_ref = _producer_artifact_reference(support_local, f"{BUNDLE_PREFIX}/{coverage_name}", spec["supportCasePointer"])
            support_assertion_ref = _producer_artifact_reference(support_local, f"{BUNDLE_PREFIX}/{coverage_name}", spec["supportAssertionPointer"])
            expected = _producer_pointer(_read_json(authority_local), spec["authorityPointer"])
            actual = _producer_pointer(_read_json(actual_local), spec["actualPointer"])
            passed = _canonical(expected) == _canonical(actual)
            comparison_operator = "equal" if spec["assertionType"] != "mutation-killed" else "not-equal"
            case_passed = passed if comparison_operator == "equal" else not passed
            producer_cases.append({
                "caseId": spec["caseId"],
                "requirementId": REQUIREMENT_ID,
                "classification": spec["classification"],
                "inputArtifact": input_ref,
                "authorityArtifact": authority_ref,
                "actualArtifact": actual_ref,
                "expected": expected,
                "actual": actual,
                "comparison": {"operator": comparison_operator},
                "result": "passed" if case_passed else "failed",
                "target": spec["target"],
                "diagnostic": spec["diagnostic"],
                "supportingArtifact": support_case_ref,
            })
            producer_assertions.append({
                "assertionId": spec["assertionId"],
                "requirementId": REQUIREMENT_ID,
                "assertionType": spec["assertionType"],
                "testCaseId": spec["caseId"],
                "classification": spec["classification"],
                "authorityArtifact": authority_ref,
                "actualArtifact": actual_ref,
                "expected": expected,
                "actual": actual,
                "comparison": {"operator": comparison_operator},
                "status": "passed" if case_passed else "failed",
                "target": spec["target"],
                "diagnostic": spec["diagnostic"],
                "supportingArtifact": support_assertion_ref,
            })
            if not case_passed:
                failures += 2
        except (OSError, UnicodeError, KeyError, TypeError, json.JSONDecodeError, QualificationError) as exc:
            failures += 2
            uncovered.append(f"{spec['caseId']}: semantic artifact could not be resolved ({type(exc).__name__}: {exc})")

    component_paths = [ROOT / "tools" / "qualification_issue104.py", Path(corpus_path), EVALUATOR_PATH]
    component_digests: list[str] = []
    for path in component_paths:
        if path.is_file():
            component_digests.append(_sha256_file(path))
        else:
            uncovered.append(f"independence component unavailable: {path}")
            component_digests.append(_sha256_bytes(f"missing:{path.as_posix()}".encode("utf-8")))
    if SHARED_EVIDENCE_PATH.is_file():
        shared_digest = _sha256_file(SHARED_EVIDENCE_PATH)
    else:
        shared_digest = _sha256_bytes(b"missing:qualification_evidence")
        uncovered.append("shared artifact-reference evaluator unavailable")
    status = "failed" if failures else "blocked" if uncovered else "passed"
    return {
        "schema": PRODUCER_REPORT_SCHEMA,
        "version": PRODUCER_REPORT_VERSION,
        "evidenceId": EVIDENCE_ID,
        "requirementIds": [REQUIREMENT_ID],
        "sourceSha": envelope_source_sha,
        "inputDigests": input_digests,
        "producerId": "issue-104-independent-corpus-runner",
        "authorityId": "issue-104-hand-authored-corpus",
        "independence": {
            "producerComponentDigest": component_digests[0],
            "authorityComponentDigest": component_digests[1],
            "evaluatorComponentDigest": component_digests[2],
            "expectedDerivedFromActual": False,
            "sharedComponentDigests": [shared_digest],
        },
        "assertions": producer_assertions,
        "testCases": producer_cases,
        "uncoveredItems": sorted(set(uncovered)),
        "unsupportedItems": [],
        "waivedItems": [],
        "status": status,
        "failureCount": failures,
    }


def _fact_map(fixture: dict[str, Any]) -> dict[str, Any]:
    return {fact["factId"]: fact.get("value") for fact in fixture["expectedFacts"]}


def _differential_report(corpus: dict[str, Any], source_sha: str | None, corpus_sha: str | None, producer_report: dict[str, Any]) -> dict[str, Any]:
    report = _base_report("differential-adjudication", source_sha, corpus_sha)
    producer_entries = {item["producerId"]: item for item in producer_report["producerMatrix"]}
    fixture_by_producer = {fixture["producerId"]: fixture for fixture in corpus["fixtures"]}
    groups = []
    for group in corpus["adjudicationGroups"]:
        available = [producer_id for producer_id in group["producerIds"] if producer_entries[producer_id]["status"] == "qualified"]
        unavailable = [producer_id for producer_id in group["producerIds"] if producer_entries[producer_id]["status"] == "unavailable"]
        member_facts = []
        for producer_id in available:
            fixture = fixture_by_producer[producer_id]
            values = _fact_map(fixture)
            member_facts.append({"producerId": producer_id, "fixtureId": fixture["fixtureId"], "facts": {key: values.get(key) for key in group["factIds"]}})
        pairwise = []
        for index, left in enumerate(member_facts):
            for right in member_facts[index + 1:]:
                differences = [fact_id for fact_id in group["factIds"] if left["facts"].get(fact_id) != right["facts"].get(fact_id)]
                pairwise.append({"left": left["producerId"], "right": right["producerId"], "differences": differences, "status": "passed" if not differences else "failed"})
        agreement = len(member_facts) >= 2 and all(item["status"] == "passed" for item in pairwise)
        status = "passed" if agreement and not unavailable else "failed"
        groups.append({
            "groupId": group["groupId"],
            "format": group["format"],
            "required": group["required"],
            "availableProducers": available,
            "unavailableProducers": unavailable,
            "memberFacts": member_facts,
            "pairwiseComparisons": pairwise,
            "adjudication": "agreement" if agreement else ("unavailable" if unavailable else "insufficient-members"),
            "independentExpectedFacts": True,
            "status": status,
        })
    report["groups"] = groups
    report["incompleteGroups"] = [item["groupId"] for item in groups if item["status"] != "passed"]
    report["sectionStatus"] = "passed" if not report["incompleteGroups"] else "failed"
    report["assertions"] = [
        {"id": "required-differential-groups-executed", "status": "passed" if groups else "failed"},
        {"id": "differential-adjudication-has-two-available-members", "status": "passed" if all(len(item["availableProducers"]) >= 2 for item in groups) else "failed"},
        {"id": "no-differential-disagreement", "status": "passed" if all(not item["pairwiseComparisons"] or all(pair["status"] == "passed" for pair in item["pairwiseComparisons"]) for item in groups) else "failed"},
    ]
    return report


def _transform_input(base: Path, transform: dict[str, Any], destination: Path) -> Path:
    kind = transform.get("type")
    if kind == "zip-repack":
        with zipfile.ZipFile(base) as source:
            entries = [(name, source.read(name)) for name in reversed(sorted(source.namelist())) if not name.endswith("/")]
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name, payload in entries:
                info = zipfile.ZipInfo(name, date_time=(2030, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, payload)
    elif kind == "append-pdf-comment":
        destination.write_bytes(base.read_bytes() + b"\n% issue-104 metamorphic comment\n")
    elif kind == "line-endings":
        payload = base.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        if transform.get("value") == "crlf":
            payload = payload.replace(b"\n", b"\r\n")
        destination.write_bytes(payload)
    elif kind == "append-text":
        destination.write_bytes(base.read_bytes() + str(transform.get("value", "")).encode("utf-8"))
    elif kind == "identity":
        shutil.copyfile(base, destination)
    else:
        raise QualificationError(f"unsupported issue #104 metamorphic transform: {kind}")
    return destination


def _metamorphic_report(corpus: dict[str, Any], source_sha: str | None, corpus_sha: str | None, fixture_inputs: dict[str, Path], fixture_by_id: dict[str, dict[str, Any]], work_dir: Path) -> dict[str, Any]:
    report = _base_report("metamorphic-relations", source_sha, corpus_sha)
    relation_results = []
    for relation in corpus["metamorphicRelations"]:
        fixture = fixture_by_id[relation["fixtureId"]]
        base = fixture_inputs[fixture["fixtureId"]]
        relation_dir = work_dir / relation["relationId"]
        relation_dir.mkdir(parents=True, exist_ok=True)
        mutated = relation_dir / base.name
        try:
            _transform_input(base, relation["transform"], mutated)
            run = _run_converter(mutated, fixture["format"], relation_dir / "converter")
            document = run.get("document")
            scalars = _output_scalars(document) if isinstance(document, dict) else []
            units = _source_units(mutated)
            checks = []
            facts = {fact["factId"]: fact for fact in fixture["expectedFacts"]}
            for fact_id in relation["preservesFactIds"]:
                fact = facts.get(fact_id)
                if fact is None:
                    checks.append({"factId": fact_id, "status": "failed", "reason": "fact is not in authored corpus"})
                    continue
                source_check = _source_fact_match(units, fact)
                output_check = _output_fact_match(scalars, fact)
                checks.append({"factId": fact_id, "sourceStatus": source_check["status"], "outputStatus": output_check.get("status"), "status": "passed" if source_check["status"] == "passed" and output_check.get("status") in {"passed", "not-required"} else "failed"})
            status = "passed" if not run["timedOut"] and isinstance(document, dict) and document.get("conversion", {}).get("status") != "failed" and all(item["status"] == "passed" for item in checks) else "failed"
            relation_results.append({
                "relationId": relation["relationId"],
                "fixtureId": fixture["fixtureId"],
                "transform": relation["transform"],
                "independentFromAdapter": True,
                "checks": checks,
                "converter": {"returnCode": run["returnCode"], "conversionStatus": (document or {}).get("conversion", {}).get("status") if isinstance(document, dict) else None, "stderr": run.get("stderr")},
                "status": status,
            })
        except (OSError, QualificationError, zipfile.BadZipFile) as exc:
            relation_results.append({"relationId": relation["relationId"], "fixtureId": fixture["fixtureId"], "transform": relation["transform"], "independentFromAdapter": True, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
    report["relations"] = relation_results
    report["failedRelationCount"] = sum(item["status"] != "passed" for item in relation_results)
    report["sectionStatus"] = "passed" if report["failedRelationCount"] == 0 else "failed"
    report["assertions"] = [
        {"id": "all-required-relations-executed", "status": "passed" if len(relation_results) == len(corpus["metamorphicRelations"]) else "failed"},
        {"id": "all-required-relations-preserve-authored-facts", "status": "passed" if report["failedRelationCount"] == 0 else "failed"},
        {"id": "relations-do-not-source-expected-values-from-adapter", "status": "passed" if all(item.get("independentFromAdapter") is True for item in relation_results) else "failed"},
    ]
    return report


def _mutate_hostile(base: Path, mutation: dict[str, Any], destination: Path) -> Path:
    kind = mutation.get("type")
    if kind == "identity":
        return _transform_input(base, {"type": "identity"}, destination)
    if kind == "replace-bytes":
        destination.write_bytes(str(mutation.get("value", "")).encode("utf-8"))
        return destination
    if kind == "repeat":
        factor = int(mutation.get("factor", 1))
        if factor < 2 or factor > 1000:
            raise QualificationError("hostile repeat factor is outside the bounded range")
        destination.write_bytes(base.read_bytes() * factor)
        return destination
    raise QualificationError(f"unsupported issue #104 hostile mutation: {kind}")


def _diagnostic_text(run: dict[str, Any]) -> str:
    document = run.get("document")
    parts = [run.get("stdout", ""), run.get("stderr", "")]
    if isinstance(document, dict):
        for diagnostic in document.get("diagnostics", []):
            if isinstance(diagnostic, dict):
                parts.extend(str(diagnostic.get(key, "")) for key in ("code", "message", "detail"))
    return " ".join(str(part) for part in parts if part)


def _hostile_report(corpus: dict[str, Any], source_sha: str | None, corpus_sha: str | None, fixture_inputs: dict[str, Path], fixture_by_id: dict[str, dict[str, Any]], work_dir: Path) -> dict[str, Any]:
    report = _base_report("hostile-resource-boundaries", source_sha, corpus_sha)
    cases = []
    for case in corpus["hostileCases"]:
        fixture = fixture_by_id[case["fixtureId"]]
        case_dir = work_dir / case["caseId"]
        case_dir.mkdir(parents=True, exist_ok=True)
        mutated = case_dir / fixture_inputs[fixture["fixtureId"]].name
        try:
            _mutate_hostile(fixture_inputs[fixture["fixtureId"]], case["mutation"], mutated)
            run = _run_converter(mutated, fixture["format"], case_dir / "converter", case["limits"])
            document = run.get("document")
            evidence = run.get("evidence") if isinstance(run.get("evidence"), dict) else {}
            observed_status = document.get("conversion", {}).get("status") if isinstance(document, dict) else None
            diagnostic = _diagnostic_text(run)
            expected = case["expected"]
            status_ok = observed_status == expected.get("conversionStatus")
            diagnostic_ok = not expected.get("diagnosticContains") or str(expected["diagnosticContains"]) in diagnostic
            evidence_ok = expected.get("limitRejectedBeforeParse") is None or evidence.get("input", {}).get("limitRejectedBeforeParse") is expected.get("limitRejectedBeforeParse")
            detected = status_ok and diagnostic_ok and evidence_ok and not run["timedOut"]
            cases.append({
                "caseId": case["caseId"],
                "fixtureId": fixture["fixtureId"],
                "format": fixture["format"],
                "limits": case["limits"],
                "mutation": case["mutation"],
                "observed": {"conversionStatus": observed_status, "diagnostic": diagnostic[-2000:], "limitRejectedBeforeParse": evidence.get("input", {}).get("limitRejectedBeforeParse")},
                "expected": expected,
                "detected": detected,
                "status": "passed" if detected else "failed",
            })
        except (OSError, QualificationError, zipfile.BadZipFile) as exc:
            cases.append({"caseId": case["caseId"], "fixtureId": fixture["fixtureId"], "status": "failed", "detected": False, "error": f"{type(exc).__name__}: {exc}"})
    report["cases"] = cases
    report["failedCaseCount"] = sum(item["status"] != "passed" for item in cases)
    report["sectionStatus"] = "passed" if report["failedCaseCount"] == 0 else "failed"
    report["assertions"] = [
        {"id": "all-hostile-cases-executed", "status": "passed" if len(cases) == len(corpus["hostileCases"]) else "failed"},
        {"id": "all-hostile-cases-fail-closed", "status": "passed" if report["failedCaseCount"] == 0 else "failed"},
        {"id": "resource-limits-are-observable", "status": "passed" if any(item.get("observed", {}).get("limitRejectedBeforeParse") is True for item in cases) else "failed"},
    ]
    return report


def _coverage_report(corpus: dict[str, Any], source_sha: str | None, corpus_sha: str | None, reports: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    report = _base_report("requirement-corpus-coverage", source_sha, corpus_sha)
    oracle_audit = _runner_import_audit()
    provenance = reports["provenance"]
    digests = reports["digests"]
    producers = reports["producers"]
    differential = reports["differential"]
    metamorphic = reports["metamorphic"]
    hostile = reports["hostile"]
    statuses = {
        "QUAL-104-ORACLE-INDEPENDENCE": oracle_audit["independent"] and oracle_audit["expectedFactsFromAdapterOutput"] is False and corpus["oracle"]["adapterOutputUsedForExpected"] is False,
        "QUAL-104-PROVENANCE-GRADE": provenance["sectionStatus"] == "passed",
        "QUAL-104-SOURCE-DIGESTS": digests["sectionStatus"] == "passed",
        "QUAL-104-PRODUCER-MATRIX": producers["sectionStatus"] == "passed",
        "QUAL-104-DIFFERENTIAL-ADJUDICATION": differential["sectionStatus"] == "passed",
        "QUAL-104-METAMORPHIC-RELATIONS": metamorphic["sectionStatus"] == "passed",
        "QUAL-104-HOSTILE-RESOURCE": hostile["sectionStatus"] == "passed",
        "QUAL-104-OFFICIAL-CORPUS": producers["requiredUnavailable"] == [],
    }
    requirements = []
    unmet: list[str] = []
    for requirement in corpus["requirements"]:
        requirement_id = requirement["requirementId"]
        passed = bool(statuses.get(requirement_id, False))
        if not passed:
            unmet.append(requirement_id)
        requirements.append({"requirementId": requirement_id, "required": requirement["required"], "status": "passed" if passed else "failed", "evidence": {"source": "issue-104-dedicated-runner"}})
    report["requirements"] = requirements
    report["unmetRequirements"] = unmet
    report["oracleImportAudit"] = oracle_audit
    report["implementedScope"] = reports["provenance"]["implementedScope"]
    report["sectionStatus"] = "passed" if not unmet else "failed"
    report["assertions"] = [
        {"id": "every-required-issue-104-requirement-has-a-result", "status": "passed" if len(requirements) == len(corpus["requirements"]) else "failed"},
        {"id": "missing-external-corpus-is-explicitly-unmet", "status": "passed" if (producers["requiredUnavailable"] and "QUAL-104-OFFICIAL-CORPUS" in unmet) or not producers["requiredUnavailable"] else "failed"},
        {"id": "no-grade-d-only-pass", "status": "passed" if reports["provenance"]["gradeGate"]["gradeDOnlyWouldPass"] is False and not statuses.get("QUAL-104-PROVENANCE-GRADE") is False else "failed"},
    ]
    return report, unmet


def _fatal_reports(source_sha: str | None, corpus_sha: str | None, message: str) -> dict[str, dict[str, Any]]:
    reports = {}
    for kind in REPORT_NAMES:
        report = _base_report(kind, source_sha, corpus_sha)
        report["failure"] = {"code": "QUALIFICATION-SETUP-FAILED", "message": message}
        report["unmetRequirements"] = ["QUAL-104-SETUP"]
        report["assertions"] = [{"id": "qualification-setup", "status": "failed", "expected": "executable", "actual": "unavailable"}]
        report["sectionStatus"] = "failed"
        reports[kind] = report
    return reports


def run_qualification(*, corpus_path: Path = DEFAULT_CORPUS_PATH, out_dir: Path = DEFAULT_OUT_DIR, fetch_external: bool = False) -> int:
    """Run all issue #104 lanes and write semantic reports plus the envelope."""

    corpus_path = Path(corpus_path)
    if not corpus_path.is_absolute():
        corpus_path = ROOT / corpus_path
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    source_sha = _source_sha()
    corpus_sha: str | None = None
    try:
        corpus_sha = _sha256_file(Path(corpus_path))
        corpus = _load_corpus(Path(corpus_path))
        fixture_work = out_dir / "work" / "fixtures"
        fixture_inputs: dict[str, Path] = {}
        fixture_results: list[dict[str, Any]] = []
        for fixture in corpus["fixtures"]:
            input_path = _materialize_fixture(fixture, fixture_work)
            fixture_inputs[fixture["fixtureId"]] = input_path
            run = _run_converter(input_path, fixture["format"], out_dir / "work" / "converters")
            fixture_results.append(_fixture_result(fixture, input_path, run))
        fixture_by_id = {fixture["fixtureId"]: fixture for fixture in corpus["fixtures"]}
        fixture_report = _fixture_report(source_sha, corpus_sha, fixture_results)
        provenance_report = _provenance_report(corpus, source_sha, corpus_sha, fixture_results)
        producer_report = _producer_report(corpus, source_sha, corpus_sha, fixture_results, fetch_external=fetch_external, acquisition_dir=out_dir / "work" / "external")
        differential_report = _differential_report(corpus, source_sha, corpus_sha, producer_report)
        metamorphic_report = _metamorphic_report(corpus, source_sha, corpus_sha, fixture_inputs, fixture_by_id, out_dir / "work" / "metamorphic")
        hostile_report = _hostile_report(corpus, source_sha, corpus_sha, fixture_inputs, fixture_by_id, out_dir / "work" / "hostile")
        preliminary = {
            "digests": fixture_report,
            "provenance": provenance_report,
            "producers": producer_report,
            "differential": differential_report,
            "metamorphic": metamorphic_report,
            "hostile": hostile_report,
        }
        coverage_report, unmet = _coverage_report(corpus, source_sha, corpus_sha, preliminary)
        reports = {
            "provenance": provenance_report,
            "coverage": coverage_report,
            "producers": producer_report,
            "differential": differential_report,
            "metamorphic": metamorphic_report,
            "hostile": hostile_report,
            "digests": fixture_report,
        }
        overall_status = "passed" if not unmet and all(report.get("sectionStatus") == "passed" for report in reports.values()) else "failed"
        for report in reports.values():
            report["status"] = overall_status
            report["completionStatus"] = "qualified" if overall_status == "passed" else "incomplete-strict-gate"
            report["unmetRequirements"] = sorted(set(report.get("unmetRequirements", [])) | set(unmet))
    except Exception as exc:
        reports = _fatal_reports(source_sha, corpus_sha, f"{type(exc).__name__}: {exc}")
        report_files = {REPORT_NAMES[kind]: report for kind, report in reports.items()}
        producer_envelope = _producer_envelope(
            None,
            report_files,
            corpus_path=corpus_path,
            out_dir=out_dir,
            source_sha=source_sha,
        )
        for name, report in report_files.items():
            _write_json(out_dir / name, report)
        _write_json(out_dir / PRODUCER_REPORT_NAME, producer_envelope)
        print(f"FAIL: issue #104 qualification setup: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    report_files = {REPORT_NAMES[kind]: report for kind, report in reports.items()}
    producer_envelope = _producer_envelope(
        corpus,
        report_files,
        corpus_path=corpus_path,
        out_dir=out_dir,
        source_sha=source_sha,
    )
    for name, report in report_files.items():
        _write_json(out_dir / name, report)
    _write_json(out_dir / PRODUCER_REPORT_NAME, producer_envelope)
    failed = [kind for kind, report in reports.items() if report["status"] != "passed"]
    if producer_envelope["status"] != "passed":
        failed.append("producer-report")
    if failed:
        print("FAIL: issue #104 qualification is incomplete; missing or failed evidence: " + ", ".join(sorted(set(coverage_report["unmetRequirements"]))), file=sys.stderr)
        return 1
    print("PASS: issue #104 qualification reports written: " + ", ".join(REQUIRED_REPORT_NAMES))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_PATH)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--json", action="store_true", help="print a machine-readable summary")
    parser.add_argument("--fetch-external", action="store_true", help="attempt only explicitly declared bounded artifact URLs")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    exit_code = run_qualification(corpus_path=args.corpus, out_dir=args.out_dir, fetch_external=args.fetch_external)
    if args.json:
        summary_path = Path(args.out_dir) / REPORT_NAMES["coverage"]
        try:
            summary = _read_json(summary_path)
        except QualificationError as exc:
            summary = {"schema": "fdir/qualification-issue-104-report", "issueNumber": 104, "status": "failed", "error": str(exc)}
        print(json.dumps({
            "schema": "fdir/qualification-issue-104-summary",
            "issueNumber": 104,
            "status": summary.get("status", "failed"),
            "completionStatus": summary.get("completionStatus", "incomplete-strict-gate"),
            "unmetRequirements": summary.get("unmetRequirements", ["QUAL-104-SETUP"]),
            "implementedScope": summary.get("implementedScope", []),
            "reportDirectory": str(Path(args.out_dir)),
        }, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
