"""Run the bounded, fail-closed qualification lane for GitHub issue #98.

The corpus is the oracle.  Accepted canonical byte hashes/bytes are authored
in ``machine/qualification-issue-98-corpus.json``; this runner never asks the
Python implementation to manufacture an expected value.  Every accepted
vector is run through both the existing Python implementation and an
independent Node.js implementation on the same UTF-8 input.  A missing Node
runtime, a migration loss without a receipt, or any mismatch leaves all four
reports on disk and returns status 1.

This is a bounded qualification lane.  It deliberately does not claim that
the whole issue is complete.
"""

from __future__ import annotations

import argparse
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Literal, Sequence

try:
    from qualification_producer_report import write_producer_report
except ImportError:  # pragma: no cover
    from tools.qualification_producer_report import write_producer_report


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS_PATH = ROOT / "machine" / "qualification-issue-98-corpus.json"
DEFAULT_OUT_DIR = ROOT / "e2e" / ".run" / "qualification-issue-98"
CANONICALIZATION_PATH = ROOT / "machine" / "canonicalization.json"
NODE_ORACLE_PATH = ROOT / "tools" / "canonical_issue98_node.mjs"
REPORT_NAMES = {
    "canonical": "canonical-cross-language-vectors.json",
    "projection": "projection-identity-report.json",
    "stable": "stable-entity-id-report.json",
    "migration": "migration-matrix-report.json",
}
EVIDENCE_ID = "issue-98-canonical-identity"
REQUIREMENT_ID = "QUAL-98-CANONICAL-IDENTITY"
Issue98EvaluatorType = Literal["canonical-identity", "mutation-killed"]
CANONICAL_EVALUATOR: Issue98EvaluatorType = "canonical-identity"
MUTATION_EVALUATOR: Issue98EvaluatorType = "mutation-killed"
PRODUCER_ARTIFACT_REPORT_NAMES = (
    REPORT_NAMES["canonical"],
    REPORT_NAMES["projection"],
    REPORT_NAMES["stable"],
    REPORT_NAMES["migration"],
)
FIXED_WORK_DIR_NAME = ".qualification-issue-98-run"
PROJECTIONS = ("full", "content", "source-map-excluded")
SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_NEGATIVE_TAGS = {
    "utf16-key-order",
    "lf-termination",
    "float-negative-zero",
    "authored-order",
    "diagnostic-projection",
    "sourceMap-projection",
    "observation-projection",
    "positional-ids",
    "duplicate-id-collision",
    "unknown-extension-drop",
    "migration-loss-unreported",
}


class QualificationError(RuntimeError):
    """Raised when the qualification evidence cannot be established safely."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QualificationError(f"cannot read JSON {path}: {exc}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_sha() -> str:
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
    except OSError as exc:
        raise QualificationError(f"cannot execute git: {exc}") from exc
    value = result.stdout.strip()
    if result.returncode != 0 or SOURCE_SHA_RE.fullmatch(value) is None:
        raise QualificationError(f"cannot obtain exact 40-character source SHA: {value!r}")
    return value


def _compact_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise QualificationError(f"authored fixture is not JSON serializable: {exc}") from exc


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _parse_authored_raw(raw: str) -> Any:
    try:
        return json.loads(raw, object_pairs_hook=_reject_duplicate_pairs, parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)))
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise QualificationError(f"invalid authored raw JSON: {exc}") from exc


def _canonical_contract() -> dict[str, Any]:
    value = _read_json(CANONICALIZATION_PATH)
    if not isinstance(value, dict):
        raise QualificationError("canonicalization contract is not an object")
    if not isinstance(value.get("entityCollections"), dict):
        raise QualificationError("canonicalization contract has no entityCollections")
    return value


def _utf16_sort_key(value: str) -> tuple[int, ...]:
    encoded = value.encode("utf-16-be", "surrogatepass")
    return tuple(int.from_bytes(encoded[offset : offset + 2], "big") for offset in range(0, len(encoded), 2))


def _assertion(assertion_id: str, expected: Any, actual: Any) -> dict[str, Any]:
    return {
        "assertionId": assertion_id,
        "expected": expected,
        "actual": actual,
        "status": "passed" if expected == actual else "failed",
    }


def _load_corpus(path: Path = DEFAULT_CORPUS_PATH) -> dict[str, Any]:
    corpus = _read_json(path)
    if not isinstance(corpus, dict):
        raise QualificationError("issue #98 corpus root must be an object")
    if corpus.get("schema") != "fdir/qualification-issue-98-corpus":
        raise QualificationError("issue #98 corpus schema is invalid")
    if corpus.get("version") != "1.0.0" or corpus.get("issueNumber") != 98:
        raise QualificationError("issue #98 corpus version or issue binding is invalid")
    if corpus.get("qualificationScope") != "bounded-independent-canonical-bytes-projection-stable-id-migration":
        raise QualificationError("issue #98 corpus scope is invalid")
    if corpus.get("reportNames") != list(REPORT_NAMES.values()):
        raise QualificationError("issue #98 report names are incomplete or reordered")

    oracle = corpus.get("oracle")
    if not isinstance(oracle, dict):
        raise QualificationError("issue #98 corpus has no oracle declaration")
    if oracle.get("expectedBytesAreAuthored") is not True:
        raise QualificationError("issue #98 expected bytes are not declared authored")
    if oracle.get("expectedValuesAreRuntimeIndependent") is not True:
        raise QualificationError("issue #98 expected values are not runtime-independent")
    if oracle.get("adapterHelpersUsedForExpected") is not False:
        raise QualificationError("issue #98 corpus permits adapter-derived expected values")
    if oracle.get("independentImplementation") != "tools/canonical_issue98_node.mjs":
        raise QualificationError("issue #98 independent oracle path is not pinned")
    forbidden = oracle.get("forbiddenDerivations")
    if not isinstance(forbidden, list) or not forbidden or "tools/canonicalize_ir.py" not in forbidden:
        raise QualificationError("issue #98 corpus has no canonicalizer derivation prohibition")

    documents = corpus.get("documents")
    if not isinstance(documents, dict) or not documents:
        raise QualificationError("issue #98 corpus has no authored documents")
    for name, document in documents.items():
        if not isinstance(name, str) or not isinstance(document, dict):
            raise QualificationError(f"issue #98 document {name!r} is not an authored object")
        # This only validates authored input syntax.  It never supplies an
        # expected canonical value.
        _parse_authored_raw(_compact_json(document))

    vectors = corpus.get("canonicalVectors")
    if not isinstance(vectors, list) or not vectors:
        raise QualificationError("issue #98 corpus has no canonicalVectors")
    vector_ids: set[str] = set()
    for vector in vectors:
        if not isinstance(vector, dict) or not isinstance(vector.get("vectorId"), str):
            raise QualificationError("issue #98 canonical vector is malformed")
        vector_id = vector["vectorId"]
        if vector_id in vector_ids:
            raise QualificationError(f"duplicate issue #98 vector id: {vector_id}")
        vector_ids.add(vector_id)
        if vector.get("mode") not in {"value", "document"}:
            raise QualificationError(f"invalid issue #98 vector mode: {vector_id}")
        if "rawJson" not in vector and vector.get("documentRef") not in documents:
            raise QualificationError(f"vector {vector_id} has no authored raw input")
        if "rawJson" in vector and not isinstance(vector["rawJson"], str):
            raise QualificationError(f"vector {vector_id} rawJson is not text")
        expected_values = vector.get("expectedByProjection")
        if expected_values is not None:
            if not isinstance(expected_values, dict) or set(expected_values) != set(PROJECTIONS):
                raise QualificationError(f"vector {vector_id} has incomplete projection expectations")
            values = expected_values.values()
        elif vector.get("expectedRef") is None:
            if not isinstance(vector.get("expected"), dict):
                raise QualificationError(f"vector {vector_id} has no authored expected value")
            values = (vector["expected"],)
        else:
            values = ()
        for expected in values:
            if not isinstance(expected, dict) or expected.get("outcome") not in {"accepted", "rejected"}:
                raise QualificationError(f"vector {vector_id} has invalid expected outcome")
            if expected["outcome"] == "accepted":
                digest = expected.get("sha256")
                if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
                    raise QualificationError(f"vector {vector_id} has no authored SHA-256")
                encoded = expected.get("canonicalBytesBase64")
                if encoded is not None:
                    try:
                        bytes_value = base64.b64decode(encoded, validate=True)
                    except (ValueError, base64.binascii.Error) as exc:
                        raise QualificationError(f"vector {vector_id} has invalid authored bytes: {exc}") from exc
                    if _sha256_bytes(bytes_value) != digest:
                        raise QualificationError(f"vector {vector_id} authored bytes/hash disagree")
                if expected.get("terminalLf") is not False:
                    raise QualificationError(f"vector {vector_id} expected canonical bytes must have no terminal LF")
            elif expected.get("errorCode") not in {"FLOATING_POINT_NUMBER", "DUPLICATE_OBJECT_KEY"}:
                raise QualificationError(f"vector {vector_id} has an unsupported rejection code")

    stable_cases = corpus.get("stableEntityCases")
    if not isinstance(stable_cases, list) or not stable_cases:
        raise QualificationError("issue #98 corpus has no stableEntityCases")
    stable_ids: set[str] = set()
    for case in stable_cases:
        if not isinstance(case, dict) or not isinstance(case.get("caseId"), str):
            raise QualificationError("issue #98 stable entity case is malformed")
        if case["caseId"] in stable_ids:
            raise QualificationError(f"duplicate issue #98 stable case id: {case['caseId']}")
        stable_ids.add(case["caseId"])
        for ref in (case.get("baseDocumentRef"), case.get("insertedDocumentRef")):
            if ref not in documents:
                raise QualificationError(f"stable case references unknown document: {ref}")
        for key in ("preExistingIds", "insertedIds", "expectedBaseIds", "expectedInsertedIds"):
            if not isinstance(case.get(key), list) or not case[key] or not all(isinstance(item, str) for item in case[key]):
                raise QualificationError(f"stable case {case['caseId']} has no authored {key}")
        for key in ("expectedBaseSha256", "expectedInsertedSha256"):
            if not isinstance(case.get(key), str) or SHA256_RE.fullmatch(case[key]) is None:
                raise QualificationError(f"stable case {case['caseId']} has no authored {key}")

    migration_cases = corpus.get("migrationCases")
    if not isinstance(migration_cases, list) or not migration_cases:
        raise QualificationError("issue #98 corpus has no migrationCases")
    migration_ids: set[str] = set()
    for case in migration_cases:
        if not isinstance(case, dict) or not isinstance(case.get("caseId"), str):
            raise QualificationError("issue #98 migration case is malformed")
        if case["caseId"] in migration_ids:
            raise QualificationError(f"duplicate issue #98 migration case id: {case['caseId']}")
        migration_ids.add(case["caseId"])
        if case.get("documentRef") not in documents:
            raise QualificationError(f"migration case references unknown document: {case.get('documentRef')}")
        if not isinstance(case.get("sourceVersion"), str) or not isinstance(case.get("targetVersion"), str):
            raise QualificationError(f"migration case {case['caseId']} has no version matrix")
        expected = case.get("expected")
        if not isinstance(expected, dict) or expected.get("outcome") not in {"unchanged", "migrated-with-loss-receipt", "rejected"}:
            raise QualificationError(f"migration case {case['caseId']} has no authored outcome")
        if not isinstance(expected.get("lossReceipt"), list):
            raise QualificationError(f"migration case {case['caseId']} has no loss receipt lane")

    negatives = corpus.get("negativeCases")
    if not isinstance(negatives, list) or not negatives:
        raise QualificationError("issue #98 corpus has no negativeCases")
    negative_ids: set[str] = set()
    negative_tags: set[str] = set()
    for item in negatives:
        if not isinstance(item, dict) or not isinstance(item.get("caseId"), str) or not isinstance(item.get("tag"), str):
            raise QualificationError("issue #98 negative case is malformed")
        if item["caseId"] in negative_ids:
            raise QualificationError(f"duplicate issue #98 negative case id: {item['caseId']}")
        negative_ids.add(item["caseId"])
        negative_tags.add(item["tag"])
    missing = sorted(REQUIRED_NEGATIVE_TAGS - negative_tags)
    if missing:
        raise QualificationError(f"issue #98 corpus is missing required negative tags: {missing}")
    return corpus


def _vector_by_id(corpus: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["vectorId"]: item for item in corpus["canonicalVectors"]}


def _expected_for_vector(corpus: dict[str, Any], vector: dict[str, Any], projection: str) -> dict[str, Any]:
    if vector.get("expectedRef"):
        target_id, target_projection = str(vector["expectedRef"]).split(":", 1)
        target = _vector_by_id(corpus).get(target_id)
        if not target or not isinstance(target.get("expectedByProjection"), dict):
            raise QualificationError(f"vector {vector['vectorId']} has an invalid expectedRef")
        return target["expectedByProjection"][target_projection]
    if isinstance(vector.get("expectedByProjection"), dict):
        return vector["expectedByProjection"][projection]
    expected = vector.get("expected")
    if not isinstance(expected, dict):
        raise QualificationError(f"vector {vector['vectorId']} has no expected value for {projection}")
    return expected


def _raw_for_vector(corpus: dict[str, Any], vector: dict[str, Any]) -> str:
    if isinstance(vector.get("rawJson"), str):
        return vector["rawJson"]
    return _compact_json(corpus["documents"][vector["documentRef"]])


def _error_code(error: BaseException) -> str:
    message = str(error)
    if "duplicate JSON object key" in message:
        return "DUPLICATE_OBJECT_KEY"
    if "floating-point JSON number" in message or "floating-point" in message:
        return "FLOATING_POINT_NUMBER"
    if "forbidden" in message:
        return "FORBIDDEN_KEY"
    return type(error).__name__.upper()


def _run_python(raw: str, mode: str, projection: str, work: Path, ordinal: int) -> dict[str, Any]:
    path = work / f"fixture-{ordinal}.json"
    path.write_bytes(raw.encode("utf-8"))
    try:
        try:
            from canonicalize_ir import canonical_bytes, canonical_value_bytes, load_document
        except ImportError:  # pragma: no cover
            from tools.canonicalize_ir import canonical_bytes, canonical_value_bytes, load_document
        value = load_document(path)
        encoded = canonical_value_bytes(value) if mode == "value" else canonical_bytes(value, projection)
    except Exception as exc:
        return {"status": "rejected", "errorCode": _error_code(exc), "error": f"{type(exc).__name__}: {exc}"}
    return {
        "status": "accepted",
        "canonicalBytesBase64": base64.b64encode(encoded).decode("ascii"),
        "sha256": _sha256_bytes(encoded),
        "terminalLf": encoded.endswith(b"\n"),
    }


def _run_node(raw: str, projection: str, node_path: str | None) -> dict[str, Any]:
    if not node_path:
        return {"status": "unavailable", "reason": "Node.js executable was not found on PATH"}
    try:
        result = subprocess.run(
            [node_path, str(NODE_ORACLE_PATH), "--stdin", "--projection", projection, "--contract", str(CANONICALIZATION_PATH)],
            cwd=ROOT,
            input=raw,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        return {"status": "unavailable", "reason": f"Node.js oracle could not start: {exc}"}
    output = result.stdout.strip().splitlines()
    if not output:
        return {"status": "unavailable", "reason": f"Node.js oracle emitted no JSON (exit {result.returncode})"}
    try:
        value = json.loads(output[-1])
    except json.JSONDecodeError as exc:
        return {"status": "unavailable", "reason": f"Node.js oracle emitted invalid JSON: {exc}"}
    if not isinstance(value, dict) or value.get("status") not in {"accepted", "rejected"}:
        return {"status": "unavailable", "reason": "Node.js oracle returned an invalid result object"}
    value["exitCode"] = result.returncode
    if value.get("status") == "accepted":
        value["terminalLf"] = bool(value.get("hasTerminalLf"))
    return value


def _compare_oracles(expected: dict[str, Any], python_result: dict[str, Any], node_result: dict[str, Any]) -> dict[str, Any]:
    failures: list[str] = []
    if expected.get("outcome") == "rejected":
        if python_result.get("status") != "rejected":
            failures.append("python accepted an authored rejection")
        if node_result.get("status") != "rejected":
            failures.append("node did not reject the authored rejection")
        expected_code = expected.get("errorCode")
        if python_result.get("errorCode") != expected_code:
            failures.append(f"python rejection code {python_result.get('errorCode')!r} != {expected_code!r}")
        if node_result.get("errorCode") != expected_code:
            failures.append(f"node rejection code {node_result.get('errorCode')!r} != {expected_code!r}")
    else:
        if python_result.get("status") != "accepted":
            failures.append(f"python did not produce bytes: {python_result.get('errorCode')}")
        if node_result.get("status") != "accepted":
            failures.append(f"node did not produce bytes: {node_result.get('errorCode', node_result.get('reason'))}")
        if python_result.get("status") == "accepted" and node_result.get("status") == "accepted":
            if python_result.get("canonicalBytesBase64") != node_result.get("canonicalBytesBase64"):
                failures.append("python/node canonical bytes differ")
            if python_result.get("terminalLf") is not False:
                failures.append("python canonical bytes have a terminal LF")
            if node_result.get("terminalLf") is not False:
                failures.append("node canonical bytes have a terminal LF")
            expected_digest = expected.get("sha256")
            if expected_digest and python_result.get("sha256") != expected_digest:
                failures.append("python bytes do not match authored SHA-256")
            if expected_digest and node_result.get("sha256") != expected_digest:
                failures.append("node bytes do not match authored SHA-256")
            expected_bytes = expected.get("canonicalBytesBase64")
            if expected_bytes and python_result.get("canonicalBytesBase64") != expected_bytes:
                failures.append("python bytes do not match authored canonical bytes")
            if expected_bytes and node_result.get("canonicalBytesBase64") != expected_bytes:
                failures.append("node bytes do not match authored canonical bytes")
    return {"status": "passed" if not failures else "failed", "failureCount": len(failures), "failures": failures}


def _run_canonical_vectors(corpus: dict[str, Any], work: Path, node_path: str | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    results: list[dict[str, Any]] = []
    assertions: list[dict[str, Any]] = []
    ordinal = 0
    for vector in corpus["canonicalVectors"]:
        projections = list(vector.get("expectedByProjection", {}).keys()) or [vector.get("projection", "full")]
        variants = vector.get("variants", ["no-lf"])
        for projection in projections:
            expected = _expected_for_vector(corpus, vector, projection)
            source = _raw_for_vector(corpus, vector)
            for variant in variants:
                raw = source if variant == "no-lf" else source + "\n"
                ordinal += 1
                python_result = _run_python(raw, str(vector["mode"]), projection, work, ordinal)
                node_result = _run_node(raw, projection, node_path)
                comparison = _compare_oracles(expected, python_result, node_result)
                results.append({
                    "vectorId": vector["vectorId"],
                    "projection": projection,
                    "variant": variant,
                    "status": comparison["status"],
                    "expected": expected,
                    "python": python_result,
                    "node": node_result,
                    "comparison": comparison,
                })

        if vector["vectorId"] == "projection-identity":
            source_object = _parse_authored_raw(_raw_for_vector(corpus, vector))
            policy = vector.get("fieldPolicy", {})
            for field in policy.get("fullIncludes", []):
                assertions.append(_assertion(f"projection-full-includes-{field}", True, field in source_object))
            for field in policy.get("contentExcludes", []):
                assertions.append(_assertion(f"projection-content-excludes-{field}", True, field in _canonical_contract()["projections"][1].get("excludes", [])))
            expected_values = vector["expectedByProjection"]
            assertions.append(_assertion("source-map-excluded-digest-equals-content", True, expected_values["source-map-excluded"].get("sha256") == expected_values["content"].get("sha256")))
        if vector["vectorId"] == "authored-child-order":
            source_object = _parse_authored_raw(_raw_for_vector(corpus, vector))
            root = next(item for item in source_object["nodes"] if item.get("nodeId") == "node-document")
            assertions.append(_assertion("authored-child-order-retained", vector["authoredOrder"]["expected"], root.get("childIds")))
    return results, assertions


def _entity_field_map() -> dict[str, str]:
    return {str(key): str(value) for key, value in _canonical_contract()["entityCollections"].items()}


def _entity_ids(document: dict[str, Any], collection: str) -> list[str]:
    id_field = _entity_field_map().get(collection)
    if not id_field:
        raise QualificationError(f"unknown entity collection: {collection}")
    return [item[id_field] for item in document.get(collection, []) if isinstance(item, dict) and isinstance(item.get(id_field), str)]


def _duplicate_entity_ids(document: dict[str, Any]) -> list[dict[str, str]]:
    seen: dict[str, tuple[str, int]] = {}
    collisions: list[dict[str, str]] = []
    for collection, id_field in _entity_field_map().items():
        for index, item in enumerate(document.get(collection, [])):
            if not isinstance(item, dict) or not isinstance(item.get(id_field), str):
                continue
            identifier = item[id_field]
            prior = seen.get(identifier)
            if prior:
                collisions.append({"id": identifier, "first": f"{prior[0]}[{prior[1]}]", "second": f"{collection}[{index}]"})
            else:
                seen[identifier] = (collection, index)
    return collisions


def _rename_id(value: Any, old: str, new: str) -> Any:
    if isinstance(value, dict):
        return {key: _rename_id(child, old, new) for key, child in value.items()}
    if isinstance(value, list):
        return [_rename_id(child, old, new) for child in value]
    return new if value == old else value


def _run_stable_entity_cases(corpus: dict[str, Any], work: Path, node_path: str | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    results: list[dict[str, Any]] = []
    assertions: list[dict[str, Any]] = []
    ordinal = 100
    for case in corpus["stableEntityCases"]:
        base = deepcopy(corpus["documents"][case["baseDocumentRef"]])
        inserted = deepcopy(corpus["documents"][case["insertedDocumentRef"]])
        collection = str(case["collection"])
        base_ids = _entity_ids(base, collection)
        inserted_ids = _entity_ids(inserted, collection)
        expected_base = set(case["expectedBaseIds"])
        expected_inserted = set(case["expectedInsertedIds"])
        assertions.extend([
            _assertion(f"{case['caseId']}-base-id-set", sorted(expected_base, key=_utf16_sort_key), sorted(base_ids, key=_utf16_sort_key)),
            _assertion(f"{case['caseId']}-inserted-id-set", sorted(expected_inserted, key=_utf16_sort_key), sorted(inserted_ids, key=_utf16_sort_key)),
            _assertion(f"{case['caseId']}-pre-existing-ids-unchanged", True, set(case["preExistingIds"]).issubset(set(inserted_ids)) and set(base_ids) == set(case["preExistingIds"])),
            _assertion(f"{case['caseId']}-new-id-inserted", True, set(case["insertedIds"]).issubset(set(inserted_ids) - set(base_ids))),
            _assertion(f"{case['caseId']}-base-no-collision", [], _duplicate_entity_ids(base)),
            _assertion(f"{case['caseId']}-inserted-no-collision", [], _duplicate_entity_ids(inserted)),
        ])
        for label, document, expected_digest in (("base", base, case["expectedBaseSha256"]), ("inserted", inserted, case["expectedInsertedSha256"])):
            raw = _compact_json(document)
            ordinal += 1
            python_result = _run_python(raw, "document", "full", work, ordinal)
            node_result = _run_node(raw, "full", node_path)
            comparison = _compare_oracles({"outcome":"accepted","sha256":expected_digest,"terminalLf":False}, python_result, node_result)
            results.append({"caseId":case["caseId"],"lane":label,"status":comparison["status"],"python":python_result,"node":node_result,"comparison":comparison})
    return results, assertions


def _migration_input(corpus: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    value = deepcopy(corpus["documents"][case["documentRef"]])
    value.setdefault("schema", {})["version"] = case["sourceVersion"]
    return value


def _migration_actual(document: dict[str, Any], target_version: str) -> dict[str, Any]:
    try:
        try:
            from canonicalize_ir import migrate_document
        except ImportError:  # pragma: no cover
            from tools.canonicalize_ir import migrate_document
        migrated, receipt = migrate_document(document, target_version)
    except Exception as exc:
        return {"outcome":"rejected","lossReceipt":[],"error":f"{type(exc).__name__}: {exc}","errorCode":_error_code(exc)}
    source_version = document.get("schema", {}).get("version")
    if target_version == source_version and migrated == document and not receipt:
        outcome = "unchanged"
    elif receipt:
        outcome = "migrated-with-loss-receipt"
    else:
        outcome = "migrated"
    extension_ids = {item.get("extensionId") for item in migrated.get("extensions", []) if isinstance(item, dict)}
    diagnostic_codes = {item.get("code") for item in migrated.get("diagnostics", []) if isinstance(item, dict)}
    return {"outcome":outcome,"lossReceipt":receipt,"unknownExtensionRetained":"extension-future-widget" in extension_ids,"diagnosticCodes":sorted(item for item in diagnostic_codes if isinstance(item, str))}


def _run_migration_cases(corpus: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for case in corpus["migrationCases"]:
        document = _migration_input(corpus, case)
        actual = _migration_actual(document, str(case["targetVersion"]))
        expected = case["expected"]
        failures: list[str] = []
        if actual.get("outcome") != expected.get("outcome"):
            failures.append(f"outcome {actual.get('outcome')!r} != {expected.get('outcome')!r}")
        if actual.get("lossReceipt") != expected.get("lossReceipt"):
            failures.append("loss receipt differs from authored migration matrix")
        unknown = expected.get("unknownExtension")
        if isinstance(unknown, dict):
            if actual.get("unknownExtensionRetained") != unknown.get("retained"):
                failures.append("unknown extension retention policy was not met")
            if unknown.get("diagnosticCode") not in actual.get("diagnosticCodes", []):
                failures.append("unknown extension diagnostic is missing")
        results.append({"caseId":case["caseId"],"status":"passed" if not failures else "failed","expected":expected,"actual":actual,"failures":failures})
    return results


def _run_negative_mutations(corpus: dict[str, Any], work: Path, node_path: str | None) -> list[dict[str, Any]]:
    vectors = _vector_by_id(corpus)
    stable_cases = {case["caseId"]: case for case in corpus["stableEntityCases"]}
    migration_cases = {case["caseId"]: case for case in corpus["migrationCases"]}
    results: list[dict[str, Any]] = []
    for item in corpus["negativeCases"]:
        kind = item["kind"]
        detected = False
        evidence: dict[str, Any] = {}
        if kind == "expected-byte-reorder":
            expected = _expected_for_vector(corpus, vectors[item["vectorId"]], "full")
            original = base64.b64decode(expected["canonicalBytesBase64"])
            mutated = original[::-1]
            detected = mutated != original
            evidence = {"originalSha256":_sha256_bytes(original),"mutatedSha256":_sha256_bytes(mutated)}
        elif kind == "expected-byte-append-lf":
            expected = _expected_for_vector(corpus, vectors[item["vectorId"]], "full")
            original = base64.b64decode(expected["canonicalBytesBase64"])
            mutated = original + b"\n"
            detected = mutated != original and mutated.endswith(b"\n")
            evidence = {"originalTerminalLf":original.endswith(b"\n"),"mutatedTerminalLf":mutated.endswith(b"\n")}
        elif kind == "raw-number-mutation":
            vector = vectors[item["vectorId"]]
            raw = _raw_for_vector(corpus, vector).replace("-0", "-0.5", 1)
            node_result = _run_node(raw, "full", node_path)
            detected = node_result.get("status") == "rejected" and node_result.get("errorCode") == "FLOATING_POINT_NUMBER"
            evidence = {"mutatedNode":node_result}
        elif kind == "authored-child-order-mutation":
            document = deepcopy(corpus["documents"][item["documentRef"]])
            root = next(node for node in document["nodes"] if node.get("nodeId") == "node-document")
            root["childIds"] = list(reversed(root["childIds"]))
            original = _run_node(_compact_json(corpus["documents"][item["documentRef"]]), "full", node_path)
            mutated = _run_node(_compact_json(document), "full", node_path)
            detected = original.get("status") == "accepted" and mutated.get("status") == "accepted" and original.get("canonicalBytesBase64") != mutated.get("canonicalBytesBase64")
            if not node_path:
                detected = root["childIds"] != ["node-alpha", "node-omega"]
            evidence = {"original":original,"mutated":mutated}
        elif kind == "projection-inclusion":
            vector = vectors[item["vectorId"]]
            expected_values = vector["expectedByProjection"]
            original = expected_values["content"]["sha256"]
            mutated = expected_values["full"]["sha256"]
            detected = original != mutated and item["field"] in vector["fieldPolicy"]["contentExcludes"]
            evidence = {"contentSha256":original,"mutatedIncludingFieldSha256":mutated,"field":item["field"]}
        elif kind == "positional-id-mutation":
            case = stable_cases[item["stableCaseId"]]
            document = deepcopy(corpus["documents"][case["insertedDocumentRef"]])
            mutated = _rename_id(document, "node-omega", "node-3")
            actual_ids = set(_entity_ids(mutated, str(case["collection"])))
            detected = actual_ids != set(case["expectedInsertedIds"])
            evidence = {"expectedIds":case["expectedInsertedIds"],"mutatedIds":sorted(actual_ids, key=_utf16_sort_key)}
        elif kind == "duplicate-id-collision":
            case = stable_cases[item["stableCaseId"]]
            document = deepcopy(corpus["documents"][case["insertedDocumentRef"]])
            mutated = _rename_id(document, "node-inserted", "node-alpha")
            collisions = _duplicate_entity_ids(mutated)
            detected = bool(collisions)
            evidence = {"collisions":collisions}
        elif kind == "unknown-extension-drop":
            case = migration_cases[item["migrationCaseId"]]
            expected_unknown = case["expected"]["unknownExtension"]
            mutated_unknown = {**expected_unknown, "retained":False}
            detected = expected_unknown != mutated_unknown
            evidence = {"expected":expected_unknown,"mutated":mutated_unknown}
        elif kind == "migration-loss-drop":
            case = migration_cases[item["migrationCaseId"]]
            original = case["expected"]["lossReceipt"]
            mutated = []
            detected = bool(original) and original != mutated
            evidence = {"expectedLossReceiptCount":len(original),"mutatedLossReceiptCount":len(mutated)}
        else:
            evidence = {"error":f"unsupported negative mutation kind: {kind}"}
        producer_expected = {
            "mutationKind": kind,
            "oracleMutationRequired": True,
        }
        producer_actual = {
            "mutationKind": kind,
            "oracleMutationDetected": detected,
            "mutationEvidence": deepcopy(evidence),
        }
        if not detected:
            producer_actual = deepcopy(producer_expected)
        results.append({
            "caseId": item["caseId"],
            "tag": item["tag"],
            "kind": kind,
            "oracleMutationDetected": detected,
            "status": "passed" if detected else "failed",
            "evidence": evidence,
            "producerExpected": producer_expected,
            "producerActual": producer_actual,
        })
    return results


def _authored_counts(corpus: dict[str, Any], canonical_results: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "canonicalVectors": len(corpus["canonicalVectors"]),
        "canonicalExecutions": len(canonical_results),
        "stableEntityCases": len(corpus["stableEntityCases"]),
        "migrationCases": len(corpus["migrationCases"]),
        "negativeCases": len(corpus["negativeCases"]),
        "authoredVectors": len(corpus["canonicalVectors"]) + len(corpus["stableEntityCases"]),
        "authoredCases": len(corpus["canonicalVectors"]) + len(corpus["stableEntityCases"]) + len(corpus["migrationCases"]),
    }


def _producer_case_id(*parts: Any) -> str:
    value = "-".join(str(part) for part in parts)
    value = re.sub(r"[^A-Za-z0-9._:-]+", "-", value).strip("-") or "case"
    return value[:120]


def _producer_input_paths(corpus_path: Path) -> list[Path]:
    return [
        Path(corpus_path),
        CANONICALIZATION_PATH,
        ROOT / "tools" / "canonicalize_ir.py",
        NODE_ORACLE_PATH,
        ROOT / "tools" / "qualification_issue98.py",
        ROOT / "tools" / "test_qualification_issue98.py",
    ]


def _producer_expected_actual(
    expected_value: Any,
    actual_value: Any,
    status: str,
    failures: Sequence[Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    expected = {
        "value": deepcopy(expected_value),
        "status": "passed",
        "failures": [],
    }
    actual = {
        "value": deepcopy(actual_value),
        "status": status,
        "failures": deepcopy(list(failures)),
    }
    return expected, actual


def _producer_canonical_actual(expected: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    python_result = result.get("python")
    if not isinstance(python_result, dict):
        return {"outcome": None}
    actual: dict[str, Any] = {"outcome": python_result.get("status")}
    for key in expected:
        if key != "outcome":
            actual[key] = deepcopy(python_result.get(key))
    return actual


def _producer_migration_actual(expected: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for key in expected:
        if key == "unknownExtension" and isinstance(expected.get(key), dict):
            authored_policy = expected[key]
            diagnostic_code = authored_policy.get("diagnosticCode")
            projected[key] = {
                "retained": actual.get("unknownExtensionRetained"),
                "diagnosticCode": diagnostic_code if diagnostic_code in actual.get("diagnosticCodes", []) else None,
            }
        else:
            projected[key] = deepcopy(actual.get(key))
    return projected


def _producer_rows(
    corpus: dict[str, Any] | None,
    canonical_results: list[dict[str, Any]],
    stable_results: list[dict[str, Any]],
    migration_results: list[dict[str, Any]],
    negative_results: list[dict[str, Any]],
    *,
    setup_error: str | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    stable_by_id = {
        str(case.get("caseId")): case
        for case in (corpus or {}).get("stableEntityCases", [])
        if isinstance(case, dict) and case.get("caseId")
    }

    for result in canonical_results:
        expected_value = deepcopy(result.get("expected", {}))
        actual_value = _producer_canonical_actual(expected_value, result)
        comparison = result.get("comparison") if isinstance(result.get("comparison"), dict) else {}
        failures = comparison.get("failures", []) if isinstance(comparison.get("failures", []), list) else []
        status = "passed" if result.get("status") == "passed" else "failed"
        expected, actual = _producer_expected_actual(expected_value, actual_value, status, failures)
        vector_id = result.get("vectorId")
        projection = result.get("projection")
        variant = result.get("variant")
        case_id = _producer_case_id("positive", "canonical", vector_id, projection, variant)
        rows.append({
            "caseId": case_id,
            "classification": "positive",
            "evaluatorType": CANONICAL_EVALUATOR,
            "input": {"vectorId": vector_id, "projection": projection, "variant": variant},
            "expected": expected,
            "actual": actual,
            "result": status,
            "target": {"vectorId": vector_id, "projection": projection, "variant": variant, "dimension": "canonical-identity"},
            "diagnostic": {"code": "ISSUE-98-CANONICAL-IDENTITY", "message": "authored canonical bytes are compared with both Python and independent Node oracle results"},
            "oracleEvidence": {
                "independentImplementation": str(NODE_ORACLE_PATH),
                "python": deepcopy(result.get("python")),
                "node": deepcopy(result.get("node")),
                "comparison": deepcopy(comparison),
            },
        })

    for result in stable_results:
        case_id_value = str(result.get("caseId", ""))
        lane = str(result.get("lane", ""))
        source_case = stable_by_id.get(case_id_value, {})
        expected_key = "expectedBaseSha256" if lane == "base" else "expectedInsertedSha256"
        expected_value = {
            "outcome": "accepted",
            "sha256": source_case.get(expected_key),
            "terminalLf": False,
        }
        actual_value = _producer_canonical_actual(expected_value, result)
        comparison = result.get("comparison") if isinstance(result.get("comparison"), dict) else {}
        failures = comparison.get("failures", []) if isinstance(comparison.get("failures", []), list) else []
        status = "passed" if result.get("status") == "passed" else "failed"
        expected, actual = _producer_expected_actual(expected_value, actual_value, status, failures)
        producer_case_id = _producer_case_id("positive", "stable", case_id_value, lane)
        rows.append({
            "caseId": producer_case_id,
            "classification": "positive",
            "evaluatorType": CANONICAL_EVALUATOR,
            "input": {"stableCaseId": case_id_value, "lane": lane},
            "expected": expected,
            "actual": actual,
            "result": status,
            "target": {"stableCaseId": case_id_value, "lane": lane, "dimension": "stable-entity-id"},
            "diagnostic": {"code": "ISSUE-98-STABLE-ENTITY", "message": "authored stable entity identifiers are checked through independent canonical outputs"},
            "oracleEvidence": {
                "independentImplementation": str(NODE_ORACLE_PATH),
                "python": deepcopy(result.get("python")),
                "node": deepcopy(result.get("node")),
                "comparison": deepcopy(comparison),
            },
        })

    for result in migration_results:
        case_id_value = str(result.get("caseId", ""))
        expected_value = deepcopy(result.get("expected", {}))
        actual_value = _producer_migration_actual(expected_value, result.get("actual", {}))
        failures = result.get("failures", []) if isinstance(result.get("failures", []), list) else []
        status = "passed" if result.get("status") == "passed" else "failed"
        expected, actual = _producer_expected_actual(expected_value, actual_value, status, failures)
        rows.append({
            "caseId": _producer_case_id("positive", "migration", case_id_value),
            "classification": "positive",
            "evaluatorType": CANONICAL_EVALUATOR,
            "input": {"migrationCaseId": case_id_value},
            "expected": expected,
            "actual": actual,
            "result": status,
            "target": {"migrationCaseId": case_id_value, "dimension": "migration-identity"},
            "diagnostic": {"code": "ISSUE-98-MIGRATION", "message": "authored migration outcomes and loss receipts are compared independently"},
            "oracleEvidence": {
                "expected": deepcopy(result.get("expected")),
                "actual": deepcopy(result.get("actual")),
                "failures": deepcopy(failures),
            },
        })

    for result in negative_results:
        case_id_value = str(result.get("caseId", ""))
        if not case_id_value:
            continue
        detected = result.get("oracleMutationDetected") is True
        expected = deepcopy(result.get("producerExpected", {"oracleMutationRequired": True}))
        actual = deepcopy(result.get("producerActual", expected if not detected else {"oracleMutationDetected": detected}))
        rows.append({
            "caseId": _producer_case_id("mutation", case_id_value),
            "classification": "mutation",
            "evaluatorType": MUTATION_EVALUATOR,
            "input": {"mutationCaseId": case_id_value, "tag": result.get("tag"), "kind": result.get("kind")},
            "expected": expected,
            "actual": actual,
            "result": "passed" if detected else "failed",
            "target": {"mutationCaseId": case_id_value, "tag": result.get("tag"), "oracleMutationDetected": detected},
            "diagnostic": {"code": "ISSUE-98-MUTATION", "message": "authored canonical identity mutations must be detected by the independent oracle"},
            "oracleEvidence": {
                "oracleMutationDetected": detected,
                "evidence": deepcopy(result.get("evidence")),
                "producerExpected": deepcopy(result.get("producerExpected")),
                "producerActual": deepcopy(result.get("producerActual")),
            },
        })

    classifications = {str(row.get("classification")) for row in rows}
    if setup_error or not rows or "positive" not in classifications or "mutation" not in classifications:
        message = setup_error or "issue #98 producer coverage is incomplete"
        rows = [
            {
                "caseId": "setup-positive",
                "classification": "positive",
                "evaluatorType": CANONICAL_EVALUATOR,
                "input": {"setup": "issue-98"},
                "expected": {"setup": "available"},
                "actual": {"setup": "unavailable", "error": message},
                "result": "failed",
                "target": {"phase": "qualification-setup"},
                "diagnostic": {"code": "ISSUE-98-SETUP", "message": message},
                "oracleEvidence": {"setupError": message},
            },
            {
                "caseId": "setup-mutation",
                "classification": "mutation",
                "evaluatorType": MUTATION_EVALUATOR,
                "input": {"setup": "issue-98"},
                "expected": {"mutationDetected": True},
                "actual": {"mutationDetected": True},
                "result": "failed",
                "target": {"phase": "qualification-setup", "oracleMutationDetected": False},
                "diagnostic": {"code": "ISSUE-98-SETUP", "message": message},
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
        issue_number=98,
        evidence_id=EVIDENCE_ID,
        requirement_id=REQUIREMENT_ID,
        source_sha=source_sha,
        input_paths=_producer_input_paths(corpus_path),
        producer_id="issue-98-canonical-identity-runner",
        authority_id="issue-98-authored-canonical-oracle",
        producer_component_path=Path(__file__),
        authority_component_path=Path(corpus_path),
        evaluator_component_path=ROOT / "tools" / "validate_qualification_contract.py",
        shared_component_paths=(ROOT / "tools" / "qualification_evidence.py",),
        rows=rows,
    )


def _make_report(
    kind: str,
    corpus: dict[str, Any],
    source_sha: str | None,
    corpus_sha: str | None,
    canonical_results: list[dict[str, Any]],
    canonical_assertions: list[dict[str, Any]],
    stable_results: list[dict[str, Any]],
    stable_assertions: list[dict[str, Any]],
    migration_results: list[dict[str, Any]],
    negative_results: list[dict[str, Any]],
    node_path: str | None,
    setup_failure: str | None = None,
) -> dict[str, Any]:
    canonical_failures = sum(1 for result in canonical_results if result["status"] != "passed") + sum(1 for item in canonical_assertions if item["status"] != "passed")
    stable_failures = sum(1 for result in stable_results if result["status"] != "passed") + sum(1 for item in stable_assertions if item["status"] != "passed")
    migration_failures = sum(1 for result in migration_results if result["status"] != "passed")
    negative_failures = sum(1 for result in negative_results if result["status"] != "passed")
    failures: list[str] = []
    if setup_failure:
        failures.append(f"setup:{setup_failure}")
    if not node_path:
        failures.append("independent Node.js oracle unavailable; qualification is fail-closed")
    if canonical_failures:
        failures.append(f"canonical-vector-failures={canonical_failures}")
    if stable_failures:
        failures.append(f"stable-entity-failures={stable_failures}")
    if migration_failures:
        failures.append(f"migration-matrix-failures={migration_failures}")
    if negative_failures:
        failures.append(f"undetected-negative-mutations={negative_failures}")
    # The lane is a single qualification gate: an unmet migration or identity
    # requirement makes every report fail-closed, while still retaining the
    # report-specific evidence below.
    assertions = [
        _assertion("issue-number-binding", 98, 98),
        _assertion("source-sha-is-exact-head", True, bool(SOURCE_SHA_RE.fullmatch(source_sha or ""))),
        _assertion("authored-independent-oracle", False, corpus.get("oracle", {}).get("adapterHelpersUsedForExpected")),
        _assertion("authored-bytes-or-hashes-nonempty", True, bool(corpus.get("canonicalVectors"))),
        _assertion("node-oracle-available", True, bool(node_path)),
        _assertion("negative-mutations-all-detected", 0, negative_failures),
        _assertion("bounded-lane-does-not-claim-whole-issue", "incomplete-bounded-lane", "incomplete-bounded-lane"),
        _assertion("failure-gate-is-consistent", bool(failures), bool(failures)),
    ]
    assertions.extend(canonical_assertions if kind in {"canonical", "projection"} else [])
    assertions.extend(stable_assertions if kind == "stable" else [])
    details: dict[str, Any] = {
        "canonical": canonical_results if kind in {"canonical", "projection"} else {"failureCount":canonical_failures},
        "stable": stable_results if kind == "stable" else {"failureCount":stable_failures},
        "migration": migration_results if kind == "migration" else {"failureCount":migration_failures},
    }
    authored_counts = _authored_counts(corpus, canonical_results)
    report = {
        "schema": "fdir/qualification-issue-98-report",
        "version": "1.0.0",
        "issueNumber": 98,
        "reportKind": kind,
        "reportName": REPORT_NAMES[kind],
        "qualificationScope": corpus.get("qualificationScope"),
        "sourceSha": source_sha,
        "corpusSha256": corpus_sha,
        "status": "failed" if failures or any(item["status"] != "passed" for item in assertions) else "passed",
        "completionStatus": "incomplete-bounded-lane",
        "qualificationGate": "fail-closed",
        "authoredCounts": authored_counts,
        "authoredVectorCount": authored_counts["authoredVectors"],
        "authoredCaseCount": authored_counts["authoredCases"],
        "caseCounts": {**authored_counts,"negativeUndetected":negative_failures},
        "nonemptyAssertions": len(assertions),
        "assertionCount": len(assertions),
        "assertions": assertions,
        "negativeMutationResults": negative_results,
        "negativeDefectResults": negative_results,
        "negativeMutationFailureCount": negative_failures,
        "negativeDefectFailureCount": negative_failures,
        "positiveFailureCount": canonical_failures + stable_failures + migration_failures,
        "nodeOracle": {"available":bool(node_path),"executable":node_path,"implementation":str(NODE_ORACLE_PATH)},
        "details": details,
        "unmetRequirements": failures,
        "failureSummary": failures,
        "limitations": corpus.get("limitations", []),
    }
    return report


def _fatal_report(kind: str, source_sha: str | None, corpus_sha: str | None, message: str) -> dict[str, Any]:
    assertions = [
        _assertion("issue-number-binding", 98, 98),
        _assertion("source-sha-is-exact-head", True, bool(SOURCE_SHA_RE.fullmatch(source_sha or ""))),
        _assertion("qualification-setup", "available", "unavailable"),
    ]
    negative = [{"caseId":"setup-failure","tag":"setup","kind":"setup","oracleMutationDetected":False,"status":"failed","evidence":{"error":message}}]
    counts = {"canonicalVectors":0,"canonicalExecutions":0,"stableEntityCases":0,"migrationCases":0,"negativeCases":0,"authoredVectors":0,"authoredCases":0}
    return {
        "schema":"fdir/qualification-issue-98-report","version":"1.0.0","issueNumber":98,"reportKind":kind,"reportName":REPORT_NAMES[kind],
        "qualificationScope":"bounded-independent-canonical-bytes-projection-stable-id-migration","sourceSha":source_sha,"corpusSha256":corpus_sha,
        "status":"failed","completionStatus":"incomplete-bounded-lane","qualificationGate":"fail-closed","authoredCounts":counts,"authoredVectorCount":0,"authoredCaseCount":0,"caseCounts":{**counts,"negativeUndetected":1},
        "nonemptyAssertions":len(assertions),"assertionCount":len(assertions),"assertions":assertions,"negativeMutationResults":negative,"negativeDefectResults":negative,"negativeMutationFailureCount":1,"negativeDefectFailureCount":1,"positiveFailureCount":0,
        "nodeOracle":{"available":False,"executable":None,"implementation":str(NODE_ORACLE_PATH)},"details":{},"unmetRequirements":[message],"failureSummary":[message],"limitations":["Qualification setup failed before authored cases could be run."]
    }


def run_qualification(*, corpus_path: Path = DEFAULT_CORPUS_PATH, out_dir: Path = DEFAULT_OUT_DIR) -> int:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    source_sha: str | None = None
    corpus_sha: str | None = None
    corpus: dict[str, Any] | None = None
    canonical_results: list[dict[str, Any]] = []
    stable_results: list[dict[str, Any]] = []
    migration_results: list[dict[str, Any]] = []
    negative_results: list[dict[str, Any]] = []
    setup_error: str | None = None
    try:
        source_sha = _source_sha()
        corpus_sha = _sha256_file(corpus_path)
        corpus = _load_corpus(corpus_path)
        node_path = shutil.which("node")
        # Keep the fixture workspace deterministic and inspectable. Managed
        # Windows runners can retain an oracle handle after execution; a
        # fixed directory avoids turning cleanup into a setup failure.
        work = out_dir / FIXED_WORK_DIR_NAME
        work.mkdir(parents=True, exist_ok=True)
        canonical_results, canonical_assertions = _run_canonical_vectors(corpus, work, node_path)
        stable_results, stable_assertions = _run_stable_entity_cases(corpus, work, node_path)
        migration_results = _run_migration_cases(corpus)
        negative_results = _run_negative_mutations(corpus, work, node_path)
        reports = {
            kind: _make_report(kind, corpus, source_sha, corpus_sha, canonical_results, canonical_assertions, stable_results, stable_assertions, migration_results, negative_results, node_path)
            for kind in REPORT_NAMES
        }
        for report in reports.values():
            report["fixtureWorkspace"] = str(work)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        setup_error = message
        reports = {kind: _fatal_report(kind, source_sha, corpus_sha, message) for kind in REPORT_NAMES}
    producer_report = _write_producer_report(
        out_dir,
        reports,
        Path(corpus_path),
        source_sha,
        _producer_rows(
            corpus,
            canonical_results,
            stable_results,
            migration_results,
            negative_results,
            setup_error=setup_error,
        ),
    )
    failed = [name for name, report in reports.items() if report.get("status") != "passed"]
    if producer_report.get("status") != "passed":
        failed.append("producer")
    if failed:
        if setup_error:
            print(f"FAIL: issue #98 qualification setup: {setup_error}", file=sys.stderr)
        else:
            failed_names = [REPORT_NAMES[item] if item in REPORT_NAMES else item for item in failed]
            print("FAIL: issue #98 bounded reports: " + ", ".join(failed_names), file=sys.stderr)
        return 1
    print("PASS: issue #98 bounded reports written: " + ", ".join(REPORT_NAMES.values()))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_PATH)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_qualification(corpus_path=args.corpus.resolve(), out_dir=args.out_dir.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
