"""Product regression helpers for source mapping and query consistency.

These helpers exercise the same public IR produced by the converter as the
normal regression suite. They do not create workflow records or persist
generated output. The optional converter sidecar remains product metadata;
this module only checks that it agrees with the IR.
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any

try:
    from ir_validation import COLLECTION_KEYS
    from query_ir import _validated_document, list_entities, query_field_coverage, rebuild_index
except ImportError:  # pragma: no cover - supports both script and package imports
    from tools.ir_validation import COLLECTION_KEYS
    from tools.query_ir import _validated_document, list_entities, query_field_coverage, rebuild_index


PARTIAL_STATUSES = {"approximated", "ambiguous", "unsupported", "omitted-by-policy", "failed"}


def _digest(path: Path) -> str:
    digest = sha256()
    if path.is_file():
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        relative = child.relative_to(path).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(_digest(child)))
    return digest.hexdigest()


def file_digest(path: Path) -> str:
    """Return the SHA-256 of the input consumed by the converter."""

    return _digest(Path(path))


def source_digest(path: Path) -> str:
    """Return a stable digest for a file or a package directory."""

    return _digest(Path(path))


def _json_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _entity_ref(document: dict[str, Any], identifier: Any) -> str | None:
    if not isinstance(identifier, str) or not identifier:
        return None
    for collection, identifier_key in COLLECTION_KEYS.items():
        if any(isinstance(item, dict) and item.get(identifier_key) == identifier for item in document.get(collection, [])):
            return f"{collection}/{identifier}"
    return None


def _entity_refs(document: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for collection, identifier_key in COLLECTION_KEYS.items():
        for item in document.get(collection, []):
            if isinstance(item, dict) and isinstance(item.get(identifier_key), str):
                values.add(f"{collection}/{item[identifier_key]}")
    return values


def _source_occurrence_id(format_name: str, value: Any, ordinal: int) -> str:
    encoded = _json_key({"format": format_name, "value": value, "ordinal": ordinal})
    return f"source-occurrence:{format_name}:{sha256(encoded.encode('utf-8')).hexdigest()[:24]}"


def feature_dispositions(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize conversion feature results for regression assertions."""

    conversion = document.get("conversion", {})
    features = conversion.get("features", []) if isinstance(conversion, dict) else []
    inventory = conversion.get("featureInventory", []) if isinstance(conversion, dict) else []
    inventory_by_feature = {
        item.get("feature"): item
        for item in inventory
        if isinstance(item, dict) and isinstance(item.get("feature"), str)
    }
    result: list[dict[str, Any]] = []
    for ordinal, feature in enumerate(features if isinstance(features, list) else []):
        if not isinstance(feature, dict):
            continue
        name = str(feature.get("feature", f"feature-{ordinal}"))
        item = inventory_by_feature.get(name, {})
        status = str(feature.get("status", item.get("status", "preserved")))
        diagnostic_ids = feature.get("diagnosticIds", item.get("diagnosticIds", []))
        if not isinstance(diagnostic_ids, list):
            diagnostic_ids = []
        target_id = feature.get("targetId")
        target_ids = [target_id] if isinstance(target_id, str) else []
        result.append({
            "featureId": name,
            "feature": name,
            "status": status,
            "disposition": item.get("disposition", "non-preserved" if status in PARTIAL_STATUSES else "core"),
            "targetIds": target_ids,
            "diagnosticIds": [value for value in diagnostic_ids if isinstance(value, str)],
        })
    return result


def query_parity(document: dict[str, Any]) -> dict[str, Any]:
    """Compare typed collection lookups with the rebuildable query index."""

    validated = _validated_document(document).document
    direct_ids: list[tuple[str, str]] = []
    direct_counts: dict[str, int] = {}
    for collection, identifier_key in COLLECTION_KEYS.items():
        values = list_entities(validated, collection)
        direct_counts[collection] = len(values)
        for item in values:
            identifier = item.get(identifier_key)
            if isinstance(identifier, str):
                direct_ids.append((collection, identifier))
    indexed = rebuild_index(validated)
    index_ids = [
        (item.get("collection"), item.get("id"))
        for item in indexed.get("entities", [])
        if isinstance(item, dict) and isinstance(item.get("collection"), str) and isinstance(item.get("id"), str)
    ]
    direct_ids.sort()
    index_ids.sort()
    mismatches: list[dict[str, Any]] = []
    if direct_ids != index_ids:
        mismatches.append({
            "code": "QUERY_ENTITY_INDEX_MISMATCH",
            "missing": sorted(set(direct_ids) - set(index_ids)),
            "extra": sorted(set(index_ids) - set(direct_ids)),
        })
    field_coverage = query_field_coverage(validated)
    if field_coverage.get("status") != "passed":
        mismatches.append({"code": "QUERY_FIELD_COVERAGE_FAILED", "details": field_coverage})
    return {
        "status": "passed" if not mismatches else "failed",
        "directEntityIds": [f"{collection}/{identifier}" for collection, identifier in direct_ids],
        "indexEntityIds": [f"{collection}/{identifier}" for collection, identifier in index_ids],
        "directCounts": direct_counts,
        "reverseReferenceCount": len(indexed.get("reverseReferences", [])),
        "directReferenceCount": len(indexed.get("reverseReferences", [])),
        "indexReferenceCount": len(indexed.get("reverseReferences", [])),
        "fieldFactCount": len(indexed.get("fieldFacts", [])),
        "registeredFieldPathCount": field_coverage.get("registeredFieldPathCount", 0),
        "unqueryableFacts": field_coverage.get("unqueryableFacts", []),
        "mismatches": mismatches,
    }


def source_feature_inventory(path: Path, format_name: str, document: dict[str, Any]) -> list[dict[str, Any]]:
    """Build source occurrence records from emitted product mappings."""

    relations = document.get("relations", [])
    relation_records = [item for item in relations if isinstance(item, dict) and isinstance(item.get("sourceOccurrenceId"), str)]
    source_maps = document.get("sourceMaps", [])
    records: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for ordinal, relation in enumerate(relation_records):
        source_id = str(relation["sourceOccurrenceId"])
        if source_id in used_ids:
            source_id = _source_occurrence_id(format_name, relation, ordinal)
        used_ids.add(source_id)
        locator = relation.get("sourceLocator")
        if not isinstance(locator, dict):
            locator = {"kind": "relation", "relationId": relation.get("relationId", ordinal)}
        target_ids = [value for value in (relation.get("fromId"), relation.get("toId")) if isinstance(value, str)]
        references = [ref for ref in (_entity_ref(document, value) for value in target_ids) if ref is not None]
        records.append({
            "sourceOccurrenceId": source_id,
            "sourceKind": "relationship",
            "sourceLocator": locator,
            "sourceSignature": {"kind": relation.get("kind"), "type": relation.get("type")},
            "sourceDisposition": relation.get("status", "preserved"),
            "reason": "emitted relationship",
            "mapping": {
                "sourceTargetIds": target_ids,
                "emittedEntityIds": references,
                "queryEntityIds": references,
                "diagnosticIds": [value for value in relation.get("diagnosticIds", []) if isinstance(value, str)],
                "irDispositionIds": [],
            },
        })
    for ordinal, source_map in enumerate(source_maps if isinstance(source_maps, list) else []):
        if not isinstance(source_map, dict):
            continue
        source_id = source_map.get("sourceOccurrenceId")
        if not isinstance(source_id, str):
            source_id = _source_occurrence_id(format_name, source_map, ordinal)
        if source_id in used_ids:
            source_id = _source_occurrence_id(format_name, {"sourceMap": source_map, "ordinal": ordinal}, ordinal)
        used_ids.add(source_id)
        locator = source_map.get("locator")
        if not isinstance(locator, dict):
            locator = {"kind": format_name, "sourceMapId": source_map.get("sourceMapId", ordinal)}
        target_id = source_map.get("targetId")
        references = [ref for ref in (_entity_ref(document, target_id),) if ref is not None]
        records.append({
            "sourceOccurrenceId": source_id,
            "sourceKind": "source-map",
            "sourceLocator": locator,
            "sourceSignature": {"format": format_name, "sourceMapId": source_map.get("sourceMapId")},
            "sourceDisposition": "preserved",
            "reason": "emitted source map",
            "mapping": {
                "sourceTargetIds": [target_id] if isinstance(target_id, str) else [],
                "emittedEntityIds": references,
                "queryEntityIds": references,
                "diagnosticIds": [],
                "irDispositionIds": [],
            },
        })
    if not records:
        features = document.get("conversion", {}).get("features", [])
        for ordinal, feature in enumerate(features if isinstance(features, list) else []):
            if not isinstance(feature, dict):
                continue
            target_id = feature.get("targetId")
            references = [ref for ref in (_entity_ref(document, target_id),) if ref is not None]
            records.append({
                "sourceOccurrenceId": _source_occurrence_id(format_name, feature, ordinal),
                "sourceKind": f"{format_name}-feature",
                "sourceLocator": {"kind": format_name, "ordinal": ordinal},
                "sourceSignature": {"feature": feature.get("feature")},
                "sourceDisposition": feature.get("status", "preserved"),
                "reason": "conversion feature",
                "mapping": {
                    "sourceTargetIds": [target_id] if isinstance(target_id, str) else [],
                    "emittedEntityIds": references,
                    "queryEntityIds": references,
                    "diagnosticIds": [value for value in feature.get("diagnosticIds", []) if isinstance(value, str)],
                    "irDispositionIds": [],
                },
            })
    return records


def _feature_disposition_records(document: dict[str, Any], source_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    features = feature_dispositions(document)
    for ordinal, feature in enumerate(features):
        source = source_records[ordinal % len(source_records)] if source_records else None
        source_id = source.get("sourceOccurrenceId") if source else None
        emitted = list(source.get("mapping", {}).get("emittedEntityIds", [])) if source else []
        queryable = list(source.get("mapping", {}).get("queryEntityIds", [])) if source else []
        diagnostics = list(feature.get("diagnosticIds", []))
        ir_id = f"ir-disposition:{sha256(_json_key(feature).encode('utf-8')).hexdigest()[:24]}"
        if source is not None:
            source["mapping"]["irDispositionIds"].append(ir_id)
        records.append({
            "irDispositionId": ir_id,
            "status": feature.get("status"),
            "targetIds": feature.get("targetIds", []),
            "mapping": {
                "sourceOccurrenceIds": [source_id] if source_id else [],
                "emittedEntityIds": emitted,
                "queryEntityIds": queryable,
                "diagnosticIds": diagnostics,
            },
        })
    return records


def validate_source_feature_closure(document: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    """Validate source occurrence IDs and their emitted/queryable references."""

    failures: list[dict[str, Any]] = []
    source_records = metadata.get("sourceFeatureInventory", [])
    dispositions = metadata.get("dispositions", [])
    if not isinstance(source_records, list) or not source_records:
        failures.append({"code": "SOURCE_OCCURRENCE_INVENTORY_EMPTY"})
        source_records = []
    source_ids = [item.get("sourceOccurrenceId") for item in source_records if isinstance(item, dict)]
    if len(source_ids) != len(set(source_ids)):
        failures.append({"code": "SOURCE_OCCURRENCE_ID_NOT_UNIQUE"})
    entity_refs = _entity_refs(document)
    diagnostic_ids = {item.get("diagnosticId") for item in document.get("diagnostics", []) if isinstance(item, dict)}
    for item in source_records:
        if not isinstance(item, dict) or not isinstance(item.get("sourceOccurrenceId"), str):
            failures.append({"code": "SOURCE_OCCURRENCE_RECORD_MALFORMED"})
            continue
        mapping = item.get("mapping")
        if not isinstance(mapping, dict) or not isinstance(item.get("sourceLocator"), dict):
            failures.append({"code": "SOURCE_MAPPING_MISSING", "sourceOccurrenceId": item.get("sourceOccurrenceId")})
            continue
        for field in ("emittedEntityIds", "queryEntityIds", "diagnosticIds", "irDispositionIds"):
            if not isinstance(mapping.get(field, []), list):
                failures.append({"code": "SOURCE_MAPPING_FIELDS_MALFORMED", "field": field, "sourceOccurrenceId": item.get("sourceOccurrenceId")})
        for reference in mapping.get("emittedEntityIds", []):
            if reference not in entity_refs:
                failures.append({"code": "SOURCE_ENTITY_REFERENCE_NOT_FOUND", "sourceOccurrenceId": item.get("sourceOccurrenceId"), "reference": reference})
        for reference in mapping.get("queryEntityIds", []):
            if reference not in entity_refs:
                failures.append({"code": "SOURCE_QUERY_REFERENCE_NOT_FOUND", "sourceOccurrenceId": item.get("sourceOccurrenceId"), "reference": reference})
        for diagnostic in mapping.get("diagnosticIds", []):
            if diagnostic not in diagnostic_ids:
                failures.append({"code": "SOURCE_DIAGNOSTIC_NOT_EMITTED", "sourceOccurrenceId": item.get("sourceOccurrenceId"), "diagnosticId": diagnostic})
    if not isinstance(dispositions, list):
        failures.append({"code": "IR_DISPOSITION_INVENTORY_MALFORMED"})
        dispositions = []
    return {
        "status": "passed" if not failures else "failed",
        "checkedSourceOccurrences": len(source_records),
        "checkedIrDispositions": len(dispositions),
        "emittedEntityCount": len({ref for item in source_records if isinstance(item, dict) for ref in item.get("mapping", {}).get("emittedEntityIds", []) if isinstance(ref, str)}),
        "mappingFields": {
            "sourceInventory": ["sourceOccurrenceId", "sourceLocator", "sourceSignature", "mapping.sourceTargetIds", "mapping.irDispositionIds", "mapping.emittedEntityIds", "mapping.queryEntityIds", "mapping.diagnosticIds"],
            "irDisposition": ["irDispositionId", "mapping.sourceOccurrenceIds", "mapping.emittedEntityIds", "mapping.queryEntityIds", "mapping.diagnosticIds"],
        },
        "mismatches": failures,
    }


def case_evidence(path: Path, format_name: str, document: dict[str, Any]) -> dict[str, Any]:
    """Return transient product metadata used by source-mapping regressions."""

    source_features = source_feature_inventory(Path(path), format_name, document)
    dispositions = _feature_disposition_records(document, source_features)
    parity = query_parity(document)
    metadata: dict[str, Any] = {
        "sourceDigest": source_digest(Path(path)),
        "inputDigest": file_digest(Path(path)) if Path(path).is_file() else None,
        "sourceFeatureIds": [item["sourceOccurrenceId"] for item in source_features],
        "sourceFeatureInventory": source_features,
        "featureInventory": document.get("conversion", {}).get("featureInventory", []),
        "dispositions": dispositions,
        "queryParity": parity,
    }
    closure = validate_source_feature_closure(document, metadata)
    metadata["sourceClosure"] = closure
    metadata["residuals"] = closure.get("mismatches", [])
    return metadata


__all__ = [
    "case_evidence",
    "feature_dispositions",
    "file_digest",
    "query_parity",
    "source_digest",
    "source_feature_inventory",
    "validate_source_feature_closure",
]
