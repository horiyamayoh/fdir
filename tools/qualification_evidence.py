"""Shared evidence builders for fail-closed format qualification.

The qualification reports are deliberately kept outside the authoritative IR.
They may contain input digests and execution summaries, while the IR itself
continues to contain typed form facts only.  Every helper in this module works
from the public converter output and the actual input path; it does not accept
hand-authored IR as a substitute for conversion evidence.
"""

from __future__ import annotations

from hashlib import sha256
import json
import posixpath
from pathlib import Path
import re
from typing import Any
import xml.etree.ElementTree as ET
import zipfile

try:
    from ir_validation import COLLECTION_KEYS
    from query_ir import get_entity, list_entities, rebuild_index
    from adapter_pdf import _decode_stream, _pdf_font_mappings, _pdf_objects, _pdf_references, _pdf_operations
except ImportError:  # pragma: no cover
    from tools.ir_validation import COLLECTION_KEYS
    from tools.query_ir import get_entity, list_entities, rebuild_index
    from tools.adapter_pdf import _decode_stream, _pdf_font_mappings, _pdf_objects, _pdf_references, _pdf_operations


PARTIAL_STATUSES = {"approximated", "ambiguous", "unsupported", "omitted-by-policy", "failed"}
ENTITY_COLLECTIONS = tuple(COLLECTION_KEYS)
ROOT_SOURCE_KINDS = {"package-container", "package-input", "pdf-container", "pdf-input", "markdown-document", "markdown-input"}


def file_digest(path: Path) -> str:
    """Return the digest of the bytes that the public converter consumed."""

    digest = sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_digest(path: Path) -> str:
    """Return a deterministic digest of a file or a hand-authored package tree.

    A directory digest is independent of filesystem traversal order and ZIP
    timestamps.  This lets the independent corpus prove which source material
    was used without treating the generated ZIP container as source truth.
    """

    path = Path(path)
    if path.is_file():
        return file_digest(path)
    digest = sha256()
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        relative = child.relative_to(path).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(file_digest(child)))
    return digest.hexdigest()


def _json_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value))


def _occurrence_id(format_name: str, kind: str, locator: Any, signature: Any) -> str:
    identity = _json_key({"format": format_name, "kind": kind, "locator": locator, "signature": signature})
    return f"source-occurrence:{format_name}:{sha256(identity.encode('utf-8')).hexdigest()[:24]}"


def _entity_ref(document: dict[str, Any], identifier: Any) -> str | None:
    if not isinstance(identifier, str) or not identifier:
        return None
    for collection, identifier_key in COLLECTION_KEYS.items():
        if any(isinstance(item, dict) and item.get(identifier_key) == identifier for item in document.get(collection, [])):
            return f"{collection}/{identifier}"
    return None


def _split_entity_ref(reference: str) -> tuple[str, str] | None:
    if not isinstance(reference, str) or "/" not in reference:
        return None
    collection, identifier = reference.split("/", 1)
    return (collection, identifier) if collection in COLLECTION_KEYS and identifier else None


def _entity_item(document: dict[str, Any], reference: str) -> dict[str, Any] | None:
    split = _split_entity_ref(reference)
    if split is None:
        return None
    collection, identifier = split
    identifier_key = COLLECTION_KEYS[collection]
    return next((item for item in document.get(collection, []) if isinstance(item, dict) and item.get(identifier_key) == identifier), None)


def _entity_disposition(item: dict[str, Any], collection: str) -> str:
    status = item.get("status")
    if collection == "extensions":
        return "extension"
    if status == "unavailable":
        return "observation"
    if status in PARTIAL_STATUSES:
        return str(status)
    if status in {"preserved", "normalized", None}:
        return "core"
    return "non-preserved"


def _entity_refs_for_targets(document: dict[str, Any], target_ids: list[str], *, extra: list[str] | None = None) -> list[str]:
    """Return emitted IR entities that are explicitly reachable from targets.

    This is intentionally based on actual IDs and typed references.  It does
    not infer an entity from a feature name or from an expected token.
    """

    targets = {target for target in target_ids if isinstance(target, str) and target}
    # A source mapper may start from an extension ID.  The extension itself
    # is emitted, and its target is the semantic entity that makes the
    # occurrence queryable.  Follow that explicit reference; never infer a
    # target from the extension type.
    for target in list(targets):
        extension = _entity_item(document, f"extensions/{target}")
        if isinstance(extension, dict) and isinstance(extension.get("targetId"), str):
            targets.add(extension["targetId"])
    references: list[str] = []

    def add(identifier: Any) -> None:
        reference = _entity_ref(document, identifier)
        if reference is not None and reference not in references:
            collection = reference.split("/", 1)[0]
            if collection not in {"diagnostics", "sourceMaps"}:
                references.append(reference)

    for target in sorted(targets):
        add(target)
    for collection, identifier_key in COLLECTION_KEYS.items():
        if collection in {"diagnostics", "sourceMaps"}:
            continue
        for item in document.get(collection, []):
            if not isinstance(item, dict):
                continue
            if item.get("targetId") in targets:
                add(item.get(identifier_key))
            if item.get("ownerNodeId") in targets or item.get("ownerCellId") in targets or item.get("nodeId") in targets:
                add(item.get(identifier_key))
            for field in ("geometryId", "formulaId", "fieldId", "tableId", "partId", "surfaceId", "coordinateSpaceId"):
                if item.get(field) in targets:
                    add(item.get(identifier_key))
            for field in ("textIds", "styleIds", "resourceIds", "relationshipIds", "childIds"):
                values = item.get(field, [])
                if isinstance(values, list) and any(value in targets for value in values):
                    add(item.get(identifier_key))
    for reference in extra or []:
        add(reference.split("/", 1)[1] if "/" in reference else reference)
    return sorted(references)


def _node_ancestor_refs(document: dict[str, Any], node_ids: list[str]) -> list[str]:
    by_id = {item.get("nodeId"): item for item in document.get("nodes", []) if isinstance(item, dict) and isinstance(item.get("nodeId"), str)}
    result: list[str] = []
    for node_id in node_ids:
        current = by_id.get(node_id)
        while isinstance(current, dict):
            parent_id = current.get("parentId")
            if not isinstance(parent_id, str):
                break
            reference = _entity_ref(document, parent_id)
            if reference and reference not in result:
                result.append(reference)
            current = by_id.get(parent_id)
    return result


def _source_map_locators(document: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for item in document.get("sourceMaps", []):
        if not isinstance(item, dict) or not isinstance(item.get("targetId"), str):
            continue
        result.setdefault(item["targetId"], []).append(item.get("locator", {}))
    return result


def _source_maps_by_id(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index emitted source maps by their stable identity."""

    return {
        item["sourceMapId"]: item
        for item in document.get("sourceMaps", [])
        if isinstance(item, dict)
        and isinstance(item.get("sourceMapId"), str)
        and isinstance(item.get("targetId"), str)
        and isinstance(item.get("locator"), dict)
    }


def _markdown_source_map_matches_signature(source_map: dict[str, Any], line: int, source_text: str) -> bool:
    """Require a resolved Markdown source map to identify the signed line."""

    locator = source_map.get("locator")
    if not isinstance(locator, dict):
        return False
    line_start = locator.get("lineStart")
    line_end = locator.get("lineEnd", line_start)
    if not isinstance(line_start, int) or not isinstance(line_end, int) or not line_start <= line <= line_end:
        return False
    # If the map exposes columns, the signed line must intersect the mapped
    # source span.  This prevents a document/root map from being accepted as
    # a line mapping merely because it covers the whole input.
    column_start = locator.get("columnStart")
    column_end = locator.get("columnEnd")
    if isinstance(column_start, int) and isinstance(column_end, int):
        signed_end = len(source_text) + 1
        if column_end < 1 or column_start > signed_end:
            return False
    if "\x00" in source_text:
        nul_column = source_text.index("\x00") + 1
        if isinstance(column_start, int) and isinstance(column_end, int) and not column_start <= nul_column < column_end:
            return False
    return True


def _feature_disposition(status: str, feature: str, summary: dict[str, Any]) -> str:
    disposition = summary.get("disposition")
    if isinstance(disposition, str) and disposition:
        return disposition
    if status == "unavailable":
        return "observation"
    if status in PARTIAL_STATUSES:
        return "non-preserved"
    if feature.endswith("-extension") or feature == "extension":
        return "extension"
    return "core"


def _feature_extra_refs(document: dict[str, Any], feature: str) -> list[str]:
    refs: list[str] = []
    if feature in {"package-relationships", "package-relationship", "pdf-object-graph"}:
        refs.extend(f"relations/{item['relationId']}" for item in document.get("relations", []) if isinstance(item, dict) and isinstance(item.get("relationId"), str))
    if feature == "font-mapping":
        refs.extend(f"extensions/{item['extensionId']}" for item in document.get("extensions", []) if isinstance(item, dict) and item.get("type") == "font-cmap" and isinstance(item.get("extensionId"), str))
        refs.extend(f"resources/{item['resourceId']}" for item in document.get("resources", []) if isinstance(item, dict) and item.get("kind") == "font" and isinstance(item.get("resourceId"), str))
    if feature == "graphics-state":
        refs.extend(f"extensions/{item['extensionId']}" for item in document.get("extensions", []) if isinstance(item, dict) and item.get("type") == "graphics-state" and isinstance(item.get("extensionId"), str))
    return refs


def feature_dispositions(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Build disposition records from the actual conversion feature entries."""

    inventory = {
        (item.get("feature"), item.get("status")): item
        for item in document.get("conversion", {}).get("featureInventory", [])
        if isinstance(item, dict) and isinstance(item.get("feature"), str) and isinstance(item.get("status"), str)
    }
    locators = _source_map_locators(document)
    source_name = document.get("sourceFormat", {}).get("name", "unknown")
    records: list[dict[str, Any]] = []
    for ordinal, item in enumerate(document.get("conversion", {}).get("features", [])):
        if not isinstance(item, dict):
            continue
        feature = str(item.get("feature", "unknown"))
        status = str(item.get("status", "failed"))
        target_ids = [item["targetId"]] if isinstance(item.get("targetId"), str) else []
        disposition = _feature_disposition(status, feature, inventory.get((feature, status), {}))
        diagnostics = _dedupe([value for value in item.get("diagnosticIds", []) if isinstance(value, str)])
        if not diagnostics and target_ids:
            diagnostics = _dedupe([
                diagnostic.get("diagnosticId")
                for diagnostic in document.get("diagnostics", [])
                if isinstance(diagnostic, dict)
                and diagnostic.get("targetId") in target_ids
                and isinstance(diagnostic.get("diagnosticId"), str)
            ])
        emitted = _entity_refs_for_targets(document, target_ids, extra=_feature_extra_refs(document, feature))
        queryable = list(emitted)
        ir_id = f"ir-disposition:{source_name}:{ordinal}:{feature}"
        mapping = {
            "sourceOccurrenceIds": [],
            "emittedEntityIds": emitted,
            "queryEntityIds": queryable,
            "diagnosticIds": diagnostics,
        }
        records.append({
            "irDispositionId": ir_id,
            "featureId": ir_id,
            "feature": feature,
            "occurrence": ordinal,
            "status": status,
            "disposition": disposition,
            "targetIds": target_ids,
            "diagnosticIds": diagnostics,
            "emittedEntityIds": emitted,
            "queryEntityIds": queryable,
            "sourceOccurrenceIds": [],
            "sourceLocators": [locator for target in target_ids for locator in locators.get(target, [])],
            "mapping": mapping,
            "residual": None if disposition in {"core", "extension", "observation"} else "explicit-status-or-diagnostic",
        })
    return records


def _source_record(
    format_name: str,
    kind: str,
    locator: dict[str, Any],
    signature: Any,
    disposition: str,
    reason: str,
    *,
    emitted: list[str] | None = None,
    diagnostics: list[str] | None = None,
    feature_hints: list[str] | None = None,
    semantic_disposition: str | None = None,
    policy_id: str | None = None,
) -> dict[str, Any]:
    occurrence_id = _occurrence_id(format_name, kind, locator, signature)
    emitted_ids = sorted(_dedupe(emitted or []))
    diagnostic_ids = sorted(_dedupe(diagnostics or []))
    mapping = {
        "sourceTargetIds": [],
        "irDispositionIds": [],
        "emittedEntityIds": emitted_ids,
        "queryEntityIds": list(emitted_ids),
        "diagnosticIds": diagnostic_ids,
    }
    record: dict[str, Any] = {
        "sourceOccurrenceId": occurrence_id,
        "featureId": occurrence_id,
        "sourceKind": kind,
        "kind": kind,
        "sourceLocator": locator,
        "locator": locator,
        "sourceSignature": signature,
        "disposition": disposition,
        "sourceDisposition": disposition,
        "semanticDisposition": semantic_disposition or disposition,
        "reason": reason,
        "diagnosticIds": diagnostic_ids,
        "featureHints": sorted(_dedupe(feature_hints or [])),
        "emittedEntityIds": emitted_ids,
        "queryEntityIds": list(emitted_ids),
        "mapping": mapping,
        "residual": None if disposition in {"core", "extension", "observation"} and (semantic_disposition or disposition) in {"core", "extension", "observation"} else reason,
    }
    if policy_id:
        record["policyId"] = policy_id
    return record


def _source_direct_target_ids(document: dict[str, Any], record: dict[str, Any]) -> list[str]:
    """Resolve only the entities directly addressed by one source occurrence.

    Ancestors and container roots are intentionally excluded unless the source
    occurrence itself is the document/package/PDF container.  Emitted entity
    closure remains useful for queryability, but it is not a source-to-feature
    identity and therefore must not drive disposition mapping.
    """

    kind = record.get("sourceKind")
    locator = record.get("sourceLocator", {}) if isinstance(record.get("sourceLocator"), dict) else {}
    signature = record.get("sourceSignature", {}) if isinstance(record.get("sourceSignature"), dict) else {}
    direct: list[str] = []
    root_id = document.get("rootNodeId")
    root_allowed = kind in ROOT_SOURCE_KINDS

    def add(identifier: Any) -> None:
        if not isinstance(identifier, str) or not identifier:
            return
        if identifier == root_id and not root_allowed:
            return
        if _entity_ref(document, identifier) is not None and identifier not in direct:
            direct.append(identifier)

    def add_reference(reference: Any) -> None:
        split = _split_entity_ref(reference) if isinstance(reference, str) else None
        if split is not None:
            add(split[1])

    def add_source_map_targets(name: str) -> None:
        for item in document.get("sourceMaps", []):
            if not isinstance(item, dict) or not isinstance(item.get("targetId"), str):
                continue
            if name in _json_key(item.get("locator", {})):
                add(item["targetId"])

    if kind in ROOT_SOURCE_KINDS:
        add(document.get("rootNodeId"))
        if kind in {"package-container", "package-input"}:
            for item in document.get("parts", []):
                if isinstance(item, dict) and item.get("kind") == "package":
                    add(item.get("partId"))
        if kind == "pdf-container":
            for item in document.get("parts", []):
                if isinstance(item, dict) and item.get("name") == "PDF document":
                    add(item.get("partId"))
    elif kind == "package-part":
        name = locator.get("path")
        if isinstance(name, str):
            part_id = _part_id_by_name(document, name)
            add(part_id)
            if isinstance(part_id, str):
                for collection, identifier_key in COLLECTION_KEYS.items():
                    if collection in {"diagnostics", "sourceMaps"}:
                        continue
                    for item in document.get(collection, []):
                        if isinstance(item, dict) and item.get("partId") == part_id:
                            add(item.get(identifier_key))
            add_source_map_targets(name)
    elif kind == "package-relationship-part":
        source_name = locator.get("sourcePart")
        if isinstance(source_name, str):
            add(_part_id_by_name(document, source_name))
    elif kind == "package-relationship":
        source_name = locator.get("sourcePart")
        target_name = locator.get("target")
        if isinstance(source_name, str):
            if source_name == "[package]":
                add(next((item.get("partId") for item in document.get("parts", []) if isinstance(item, dict) and item.get("kind") == "package"), None))
            else:
                add(_part_id_by_name(document, source_name))
        if isinstance(target_name, str):
            add(_part_id_by_name(document, target_name))
        relationship_id = signature.get("relationshipId")
        if isinstance(relationship_id, str):
            for item in document.get("relations", []):
                if isinstance(item, dict) and relationship_id in str(item.get("relationId", "")):
                    add(item.get("relationId"))
    elif kind == "pdf-object":
        object_name = signature.get("object")
        if isinstance(object_name, str):
            add(next((item.get("partId") for item in document.get("parts", []) if isinstance(item, dict) and item.get("name") == f"{object_name} obj"), None))
    elif kind == "pdf-reference":
        for key in ("fromObject", "toObject"):
            object_name = signature.get(key)
            if isinstance(object_name, str):
                add(next((item.get("partId") for item in document.get("parts", []) if isinstance(item, dict) and item.get("name") == f"{object_name} obj"), None))
        for item in document.get("relations", []):
            if isinstance(item, dict) and isinstance(item.get("relationId"), str):
                if signature.get("from") and signature.get("to") and signature.get("from") in item["relationId"] and signature.get("to") in item["relationId"]:
                    add(item["relationId"])
    elif kind == "pdf-font-cmap":
        font_object = signature.get("fontObject")
        if isinstance(font_object, str):
            for item in document.get("extensions", []):
                if isinstance(item, dict) and item.get("type") == "font-cmap" and item.get("payload", {}).get("fontObject") == font_object:
                    add(item.get("extensionId"))
            for item in document.get("resources", []):
                if isinstance(item, dict) and item.get("kind") == "font" and item.get("derivedHandle") == f"object:{font_object}":
                    add(item.get("resourceId"))
            number_generation = tuple(int(value) for value in font_object.split(" ", 1)) if " " in font_object and all(value.isdigit() for value in font_object.split(" ", 1)) else None
            if number_generation:
                add(next((item.get("partId") for item in document.get("parts", []) if isinstance(item, dict) and item.get("name") == f"{number_generation[0]} {number_generation[1]} obj"), None))
    elif kind == "pdf-operator":
        page = locator.get("page")
        if isinstance(page, int):
            for target in _pdf_page_targets(document, page):
                add(target)
        operator = signature.get("operator")
        for reference in record.get("mapping", {}).get("emittedEntityIds", []):
            item = _entity_item(document, reference) if isinstance(reference, str) else None
            if not isinstance(item, dict):
                continue
            if item.get("type") == "graphics-state" and item.get("payload", {}).get("page") == page and item.get("payload", {}).get("operator") == operator:
                add(item.get("extensionId"))
            if item.get("kind") in {"glyph", "path"}:
                add(item.get("nodeId"))
    elif kind == "markdown-line-token":
        line = locator.get("lineStart")
        if isinstance(line, int):
            for target in _markdown_line_targets(document, line):
                add(target)
        source_text = signature.get("text")
        if isinstance(source_text, str):
            for text_item in document.get("texts", []):
                if not isinstance(text_item, dict) or not isinstance(text_item.get("value"), str) or source_text not in text_item["value"].splitlines():
                    continue
                for node in document.get("nodes", []):
                    if isinstance(node, dict) and text_item.get("textId") in node.get("textIds", []):
                        add(node.get("nodeId"))
            for annotation in document.get("annotations", []):
                if isinstance(annotation, dict) and isinstance(annotation.get("body"), str) and annotation["body"] and annotation["body"] in source_text:
                    add(annotation.get("annotationId"))
        for reference in record.get("mapping", {}).get("emittedEntityIds", []):
            item = _entity_item(document, reference) if isinstance(reference, str) else None
            if isinstance(item, dict) and item.get("type") in {"front-matter", "unsupported-directive", "code-block", "reference-definition"}:
                add(item.get("extensionId"))
    return sorted(_dedupe(direct))


def _attach_entity_dispositions(document: dict[str, Any], records: list[dict[str, Any]]) -> None:
    for record in records:
        values: list[dict[str, Any]] = []
        for entity_id in record.get("mapping", {}).get("emittedEntityIds", []):
            split = _split_entity_ref(entity_id)
            item = _entity_item(document, entity_id)
            if split is None or item is None:
                continue
            values.append({"entityId": entity_id, "status": item.get("status", "preserved"), "disposition": _entity_disposition(item, split[0])})
        record["emittedEntityDispositions"] = values


def _feature_hints(document: dict[str, Any], targets: list[str], diagnostics: list[str]) -> list[str]:
    result: list[str] = []
    target_set = set(targets)
    diagnostic_set = set(diagnostics)
    for item in document.get("conversion", {}).get("features", []):
        if not isinstance(item, dict):
            continue
        if item.get("targetId") in target_set or diagnostic_set.intersection(set(item.get("diagnosticIds", []))):
            if isinstance(item.get("feature"), str):
                result.append(item["feature"])
    return sorted(_dedupe(result))


def _ir_direct_emitted_identifiers(document: dict[str, Any], ir: dict[str, Any]) -> set[str]:
    """Return emitted IDs that are not merely ancestors of an IR target."""

    target_ids = {value for value in ir.get("targetIds", []) if isinstance(value, str)}
    ancestor_ids: set[str] = set()
    for target_id in target_ids:
        ancestor_ids.update(_split_entity_ref(reference)[1] for reference in _node_ancestor_refs(document, [target_id]) if _split_entity_ref(reference) is not None)
        target_item = _entity_item(document, _entity_ref(document, target_id) or "")
        if isinstance(target_item, dict) and isinstance(target_item.get("partId"), str):
            ancestor_ids.add(target_item["partId"])
    result: set[str] = set()
    for reference in ir.get("mapping", {}).get("emittedEntityIds", []):
        if not isinstance(reference, str):
            continue
        split = _split_entity_ref(reference)
        if split is None:
            continue
        if split[1] in target_ids or split[1] not in ancestor_ids:
            result.add(split[1])
    return result


def _attach_mappings(document: dict[str, Any], source_records: list[dict[str, Any]], ir_records: list[dict[str, Any]]) -> None:
    for source in source_records:
        source_mapping = source.setdefault("mapping", {})
        source_targets = set(_source_direct_target_ids(document, source))
        source_mapping["sourceTargetIds"] = sorted(source_targets)
        source_diagnostics = set(source_mapping.get("diagnosticIds", []))
        linked: list[str] = []
        for ir in ir_records:
            ir_mapping = ir.get("mapping", {})
            ir_targets = set(value for value in ir.get("targetIds", []) if isinstance(value, str))
            ir_diagnostics = set(ir_mapping.get("diagnosticIds", []))
            direct_match = source_targets.intersection(ir_targets)
            ir_emitted_ids = _ir_direct_emitted_identifiers(document, ir)
            root_target_only = (
                document.get("rootNodeId") in ir_targets
                and not (ir_targets - {document.get("rootNodeId")})
                and source.get("sourceKind") not in ROOT_SOURCE_KINDS
            )
            emitted_match = source_targets.intersection(ir_emitted_ids) if not root_target_only else set()
            diagnostic_match = source_diagnostics.intersection(ir_diagnostics) if not root_target_only else set()
            if direct_match or emitted_match or diagnostic_match:
                linked.append(str(ir["irDispositionId"]))
        source_mapping["irDispositionIds"] = sorted(_dedupe(linked))
        source["irDispositionIds"] = list(source_mapping["irDispositionIds"])
        source["emittedEntityIds"] = list(source_mapping.get("emittedEntityIds", []))
        source["queryEntityIds"] = list(source_mapping.get("queryEntityIds", []))
        source["diagnosticIds"] = list(source_mapping.get("diagnosticIds", []))
        for ir in ir_records:
            if ir.get("irDispositionId") not in linked:
                continue
            mapping = ir.setdefault("mapping", {})
            mapping.setdefault("sourceOccurrenceIds", []).append(str(source["sourceOccurrenceId"]))
            ir.setdefault("sourceOccurrenceIds", []).append(str(source["sourceOccurrenceId"]))
            ir.setdefault("sourceLocators", []).append(source.get("sourceLocator", {}))
    for ir in ir_records:
        mapping = ir.setdefault("mapping", {})
        mapping["sourceOccurrenceIds"] = sorted(_dedupe(mapping.get("sourceOccurrenceIds", [])))
        ir["sourceOccurrenceIds"] = list(mapping["sourceOccurrenceIds"])
        ir["sourceLocators"] = list({ _json_key(item): item for item in ir.get("sourceLocators", []) }.values())
    _attach_entity_dispositions(document, source_records)


def _part_id_by_name(document: dict[str, Any], name: str) -> str | None:
    for item in document.get("parts", []):
        if isinstance(item, dict) and item.get("name") == name and isinstance(item.get("partId"), str):
            return item["partId"]
    return None


def _package_part_refs(document: dict[str, Any], name: str) -> list[str]:
    part_id = _part_id_by_name(document, name)
    references: list[str] = []
    if part_id:
        references.append(f"parts/{part_id}")
    target_ids: list[str] = []
    for item in document.get("sourceMaps", []):
        if not isinstance(item, dict) or not isinstance(item.get("targetId"), str):
            continue
        locator_text = _json_key(item.get("locator", {}))
        if name in locator_text:
            target_ids.append(item["targetId"])
    for collection, identifier_key in COLLECTION_KEYS.items():
        if collection in {"diagnostics", "sourceMaps"}:
            continue
        for item in document.get(collection, []):
            if isinstance(item, dict) and item.get("partId") == part_id:
                target_ids.append(str(item.get(identifier_key)))
    references.extend(_entity_refs_for_targets(document, target_ids))
    references.extend(_node_ancestor_refs(document, target_ids))
    return sorted(_dedupe(references))


def _whole_input_failure_diagnostics(document: dict[str, Any]) -> list[str]:
    conversion = document.get("conversion", {})
    if not (
        isinstance(conversion, dict)
        and conversion.get("status") == "failed"
        and not document.get("parts")
        and not document.get("relations")
    ):
        return []
    return sorted(_dedupe(
        item["diagnosticId"]
        for item in document.get("diagnostics", [])
        if isinstance(item, dict)
        and isinstance(item.get("diagnosticId"), str)
        and isinstance(item.get("code"), str)
        and (
            "ADAPTER-FAILED" in item["code"]
            or item["code"].endswith("-PARSE-FAILED")
            or item["code"].endswith("-CONVERSION-FAILED")
        )
    ))


def _diagnostics_for_package_source(document: dict[str, Any], name: str, part_id: str | None) -> list[str]:
    failure_ids = _whole_input_failure_diagnostics(document)
    if failure_ids:
        # A size/decode/adapter failure can happen before the package is
        # opened.  In that state every ZIP entry is a source occurrence of
        # the same failed input, and the actual adapter diagnostic is the
        # only valid disposition evidence.  Do not invent a part/relation
        # emission for it.
        return failure_ids
    result: list[str] = []
    root_id = document.get("rootNodeId")
    for diagnostic in document.get("diagnostics", []):
        if not isinstance(diagnostic, dict) or not isinstance(diagnostic.get("diagnosticId"), str):
            continue
        target = diagnostic.get("targetId")
        message = str(diagnostic.get("message", ""))
        target_item = _entity_item(document, _entity_ref(document, target) or "")
        target_part = target_item.get("partId") if isinstance(target_item, dict) else None
        if name in message or target == part_id or target_part == part_id or (target == root_id and name in {"word/document.xml", "xl/workbook.xml"}):
            result.append(diagnostic["diagnosticId"])
    return sorted(_dedupe(result))


def _package_source_diagnostics(document: dict[str, Any], name: str, part_id: str | None) -> list[str]:
    """Resolve diagnostics for a package occurrence, including fail-closed input rejection.

    A resource-limited conversion intentionally emits no package parts or
    relationships.  Every source entry still needs an explicit disposition;
    the adapter-level input rejection diagnostic is therefore the authoritative
    residual for each unparsed package entry.
    """

    diagnostics = _diagnostics_for_package_source(document, name, part_id)
    if diagnostics:
        return diagnostics
    conversion = document.get("conversion", {})
    if conversion.get("status") == "failed" and not document.get("parts"):
        return sorted(
            item["diagnosticId"]
            for item in document.get("diagnostics", [])
            if isinstance(item, dict) and isinstance(item.get("diagnosticId"), str)
        )
    return []


def _diagnostic_feature_hints(document: dict[str, Any], diagnostic_ids: list[str], targets: list[str]) -> list[str]:
    return _feature_hints(document, targets, diagnostic_ids)


def _xml_local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _relationship_source_name(name: str) -> str:
    if name == "_rels/.rels":
        return "[package]"
    if "/_rels/" not in name:
        return "[package]"
    directory, relative = name.split("/_rels/", 1)
    return f"{directory}/{relative[:-5]}" if relative.endswith(".rels") else f"{directory}/{relative}"


def _relationship_target_name(source_name: str, target: str) -> str:
    target = target.replace("\\", "/")
    if target.startswith("/"):
        return posixpath.normpath(target.lstrip("/"))
    base = "" if source_name == "[package]" else source_name.rsplit("/", 1)[0] if "/" in source_name else ""
    return posixpath.normpath(posixpath.join(base, target))


def _package_relationship_refs(document: dict[str, Any], source_name: str, target_name: str) -> list[str]:
    source_id = _part_id_by_name(document, source_name) or _part_id_by_name(document, "OOXML package") if source_name == "[package]" else _part_id_by_name(document, source_name)
    target_ids: set[str] = set()
    target_part = _part_id_by_name(document, target_name)
    if target_part:
        target_ids.add(target_part)
    for resource in document.get("resources", []):
        if not isinstance(resource, dict):
            continue
        if resource.get("externalTarget") == target_name or resource.get("derivedHandle") == target_name:
            if isinstance(resource.get("resourceId"), str):
                target_ids.add(resource["resourceId"])
    refs: list[str] = []
    for relation in document.get("relations", []):
        if not isinstance(relation, dict) or not isinstance(relation.get("relationId"), str):
            continue
        if source_id and relation.get("fromId") == source_id and (not target_ids or relation.get("toId") in target_ids):
            refs.append(f"relations/{relation['relationId']}")
    return refs


def _package_container_record(document: dict[str, Any], format_name: str, diagnostics: list[str]) -> dict[str, Any]:
    root_ref = _entity_ref(document, document.get("rootNodeId"))
    package_ref = next((f"parts/{item['partId']}" for item in document.get("parts", []) if isinstance(item, dict) and item.get("kind") == "package" and isinstance(item.get("partId"), str)), None)
    diagnostic_targets = [
        item.get("targetId")
        for item in document.get("diagnostics", [])
        if isinstance(item, dict) and item.get("diagnosticId") in diagnostics and isinstance(item.get("targetId"), str)
    ]
    emitted = [value for value in [root_ref, package_ref] if value]
    emitted.extend(_entity_refs_for_targets(document, diagnostic_targets))
    emitted = sorted(_dedupe(emitted))
    disposition = "core" if emitted else "non-preserved"
    partial_conversion = document.get("conversion", {}).get("status") in {"partial", "failed"}
    if partial_conversion:
        disposition = "non-preserved"
    return _source_record(format_name, "package-container", {"path": "."}, {"container": format_name}, disposition, "package-root-emission" if emitted else "package-root-not-emitted", emitted=emitted, diagnostics=diagnostics, feature_hints=_diagnostic_feature_hints(document, diagnostics, [document.get("rootNodeId")] + diagnostic_targets if isinstance(document.get("rootNodeId"), str) else diagnostic_targets), semantic_disposition="non-preserved" if partial_conversion else None)


def _package_source_features(path: Path, document: dict[str, Any], format_name: str) -> list[dict[str, Any]]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = sorted(name.replace("\\", "/") for name in archive.namelist())
    except (OSError, zipfile.BadZipFile):
        diagnostics = [item.get("diagnosticId") for item in document.get("diagnostics", []) if isinstance(item, dict) and isinstance(item.get("diagnosticId"), str)]
        return [_source_record(format_name, "package-input", {"path": Path(path).name}, {"path": Path(path).name}, "non-preserved", "malformed-package", emitted=[_entity_ref(document, document.get("rootNodeId"))] if _entity_ref(document, document.get("rootNodeId")) else [], diagnostics=diagnostics)]

    records = [_package_container_record(document, format_name, [item.get("diagnosticId") for item in document.get("diagnostics", []) if isinstance(item, dict) and isinstance(item.get("diagnosticId"), str)])]
    for name in names:
        if name == "[Content_Types].xml":
            records.append(_source_record(format_name, "package-part", {"path": name}, name, "omitted-by-policy", "package-control-metadata-is-not-a-form-entity", policy_id="package-control-metadata-not-form-entity"))
            continue
        if name.endswith(".rels") or name == "_rels/.rels":
            source_name = _relationship_source_name(name)
            try:
                with zipfile.ZipFile(path) as archive:
                    root = ET.fromstring(archive.read(name))
            except (OSError, KeyError, ET.ParseError):
                diagnostics = _package_source_diagnostics(document, name, _part_id_by_name(document, source_name))
                records.append(_source_record(format_name, "package-relationship-part", {"path": name, "sourcePart": source_name}, name, "non-preserved", "relationship-part-could-not-be-parsed", diagnostics=diagnostics))
                continue
            relationship_count = 0
            for relationship in list(root):
                if _xml_local(relationship.tag) != "Relationship":
                    continue
                relationship_count += 1
                relationship_id = relationship.attrib.get("Id", "")
                target = relationship.attrib.get("Target", "")
                target_mode = relationship.attrib.get("TargetMode", "")
                target_name = target if target_mode == "External" else _relationship_target_name(source_name, target)
                relation_refs = _package_relationship_refs(document, source_name, target_name)
                source_id = _part_id_by_name(document, source_name) if source_name != "[package]" else next((item.get("partId") for item in document.get("parts", []) if isinstance(item, dict) and item.get("kind") == "package"), None)
                source_refs = [f"parts/{source_id}"] if isinstance(source_id, str) else []
                target_refs = [f"parts/{target_id}"] if isinstance(target_id := _part_id_by_name(document, target_name), str) else []
                emitted = sorted(_dedupe(source_refs + target_refs + relation_refs))
                relation_statuses = [_entity_item(document, reference).get("status") for reference in relation_refs if _entity_item(document, reference) is not None]
                diagnostics = _package_source_diagnostics(document, target_name, source_id)
                diagnostic_targets = [
                    item.get("targetId")
                    for item in document.get("diagnostics", [])
                    if isinstance(item, dict) and item.get("diagnosticId") in diagnostics and isinstance(item.get("targetId"), str)
                ]
                emitted.extend(_entity_refs_for_targets(document, diagnostic_targets))
                emitted = sorted(_dedupe(emitted))
                hints = _diagnostic_feature_hints(document, diagnostics, [source_id] if isinstance(source_id, str) else [])
                if relation_refs and all(status in {None, "preserved", "normalized"} for status in relation_statuses):
                    disposition = "core"
                else:
                    disposition = "non-preserved"
                semantic = "unsupported" if any(document_feature.get("status") in PARTIAL_STATUSES for document_feature in document.get("conversion", {}).get("features", []) if isinstance(document_feature, dict) and set(document_feature.get("diagnosticIds", [])).intersection(diagnostics)) else disposition
                records.append(_source_record(format_name, "package-relationship", {"path": name, "sourcePart": source_name, "relationshipId": relationship_id, "target": target_name}, {"path": name, "relationshipId": relationship_id, "target": target_name}, disposition, "emitted-relationship" if relation_refs else "relationship-not-emitted", emitted=emitted, diagnostics=diagnostics, feature_hints=hints, semantic_disposition=semantic))
            if relationship_count == 0:
                diagnostics = _package_source_diagnostics(document, name, _part_id_by_name(document, source_name))
                records.append(_source_record(format_name, "package-relationship-part", {"path": name, "sourcePart": source_name}, name, "non-preserved", "relationship-part-has-no-emitted-relationship", diagnostics=diagnostics))
            continue
        part_id = _part_id_by_name(document, name)
        emitted = _package_part_refs(document, name)
        diagnostics = _package_source_diagnostics(document, name, part_id)
        diagnostic_targets = [
            item.get("targetId")
            for item in document.get("diagnostics", [])
            if isinstance(item, dict) and item.get("diagnosticId") in diagnostics and isinstance(item.get("targetId"), str)
        ]
        emitted.extend(_entity_refs_for_targets(document, diagnostic_targets))
        emitted = sorted(_dedupe(emitted))
        hints = _diagnostic_feature_hints(document, diagnostics, [part_id] if isinstance(part_id, str) else [])
        part = _entity_item(document, f"parts/{part_id}") if part_id else None
        disposition = _entity_disposition(part, "parts") if part is not None else "non-preserved"
        semantic = "unsupported" if any(item.get("status") in PARTIAL_STATUSES for item in document.get("conversion", {}).get("features", []) if isinstance(item, dict) and set(item.get("diagnosticIds", [])).intersection(diagnostics)) else disposition
        records.append(_source_record(format_name, "package-part", {"path": name}, name, disposition, "emitted-package-part" if part is not None else "package-entry-not-represented", emitted=emitted, diagnostics=diagnostics, feature_hints=hints, semantic_disposition=semantic))
    return records


def _pdf_part_ref(document: dict[str, Any], identifier: str) -> str | None:
    return next((f"parts/{item['partId']}" for item in document.get("parts", []) if isinstance(item, dict) and item.get("name") == f"{identifier} obj" and isinstance(item.get("partId"), str)), None)


def _pdf_page_targets(document: dict[str, Any], page: int) -> list[str]:
    return [item["nodeId"] for item in document.get("nodes", []) if isinstance(item, dict) and item.get("kind") == "section" and any(locator.get("page") == page for locator in _source_map_locators(document).get(item.get("nodeId"), []))]


def _pdf_page_streams(objects: dict[tuple[int, int], bytes]) -> list[tuple[int, tuple[int, int], bytes]]:
    pages = [(key, value) for key, value in sorted(objects.items()) if re.search(rb"/Type\s*/Page\b", value)]
    result: list[tuple[int, tuple[int, int], bytes]] = []
    for page, (page_key, page_data) in enumerate(pages, start=1):
        references = _pdf_references(page_data)
        streams = [(reference, _decode_stream(objects[reference])) for reference in references if reference in objects and _decode_stream(objects[reference])]
        if streams:
            for reference, stream in streams:
                result.append((page, reference, stream))
        else:
            result.append((page, page_key, page_data))
    return result


def _pdf_diagnostic_ids(document: dict[str, Any], code: str, page_targets: list[str] | None = None) -> list[str]:
    page_set = set(page_targets or [])
    return [item["diagnosticId"] for item in document.get("diagnostics", []) if isinstance(item, dict) and item.get("code") == code and (not page_set or item.get("targetId") in page_set) and isinstance(item.get("diagnosticId"), str)]


def _pdf_source_features(path: Path, document: dict[str, Any]) -> list[dict[str, Any]]:
    data = Path(path).read_bytes()
    objects = _pdf_objects(data)
    if not objects or not document.get("parts"):
        diagnostics = [item.get("diagnosticId") for item in document.get("diagnostics", []) if isinstance(item, dict) and isinstance(item.get("diagnosticId"), str)]
        root_ref = _entity_ref(document, document.get("rootNodeId"))
        return [_source_record("pdf", "pdf-input", {"path": Path(path).name}, {"path": Path(path).name}, "non-preserved", "no-emitted-pdf-object", emitted=[root_ref] if root_ref else [], diagnostics=diagnostics)]
    records: list[dict[str, Any]] = []
    root_ref = _entity_ref(document, document.get("rootNodeId"))
    document_part_ref = next((f"parts/{item['partId']}" for item in document.get("parts", []) if isinstance(item, dict) and item.get("name") == "PDF document" and isinstance(item.get("partId"), str)), None)
    records.append(_source_record("pdf", "pdf-container", {"path": "PDF document"}, {"container": "pdf"}, "core" if root_ref or document_part_ref else "non-preserved", "pdf-document-root", emitted=[value for value in [root_ref, document_part_ref] if value]))
    part_by_object = {key: _pdf_part_ref(document, f"{key[0]} {key[1]}") for key in objects}
    missing_reference_diagnostics = _pdf_diagnostic_ids(document, "DFIR-PDF-OBJECT-REFERENCE-MISSING")
    for (number, generation), object_data in sorted(objects.items()):
        identifier = f"{number} {generation}"
        part_ref = part_by_object.get((number, generation))
        records.append(_source_record("pdf", "pdf-object", {"object": number, "generation": generation}, {"object": identifier}, "core" if part_ref else "non-preserved", "emitted-indirect-object" if part_ref else "indirect-object-not-emitted", emitted=[part_ref] if part_ref else [], feature_hints=["pdf-object-graph"] if part_ref else []))
        for target_number, target_generation in _pdf_references(object_data):
            target = f"{target_number} {target_generation}"
            target_part = part_by_object.get((target_number, target_generation))
            relation_refs = [f"relations/{item['relationId']}" for item in document.get("relations", []) if isinstance(item, dict) and item.get("fromId") == (part_ref.split("/", 1)[1] if part_ref else None) and item.get("toId") in ({target_part.split("/", 1)[1]} if target_part else set()) and isinstance(item.get("relationId"), str)]
            resource_refs = [f"resources/{item['resourceId']}" for item in document.get("resources", []) if isinstance(item, dict) and item.get("derivedHandle") == f"{target_number} {target_generation} R" and isinstance(item.get("resourceId"), str)]
            emitted = sorted(_dedupe(([part_ref] if part_ref else []) + ([target_part] if target_part else []) + relation_refs + resource_refs))
            diagnostics = [] if target_part else missing_reference_diagnostics
            records.append(_source_record("pdf", "pdf-reference", {"fromObject": identifier, "toObject": target}, {"from": identifier, "to": target}, "core" if relation_refs and target_part else "non-preserved", "resolved-object-reference" if relation_refs and target_part else "missing-object-reference", emitted=emitted, diagnostics=diagnostics, feature_hints=["pdf-object-graph"], semantic_disposition="non-preserved" if not target_part else None))
    fonts = _pdf_font_mappings(objects)
    for font in fonts:
        identifier = f"{font['object'][0]} {font['object'][1]}"
        ext_refs = [f"extensions/{item['extensionId']}" for item in document.get("extensions", []) if isinstance(item, dict) and item.get("type") == "font-cmap" and isinstance(item.get("extensionId"), str) and isinstance(item.get("payload"), dict) and item["payload"].get("fontObject") == identifier]
        resource_refs = [f"resources/{item['resourceId']}" for item in document.get("resources", []) if isinstance(item, dict) and item.get("kind") == "font" and item.get("derivedHandle") == f"object:{identifier}" and isinstance(item.get("resourceId"), str)]
        diagnostics = _pdf_diagnostic_ids(document, "DFIR-PDF-FONT-CMAP-UNAVAILABLE") if font.get("mappingStatus") != "preserved" else []
        disposition = "extension" if ext_refs else "non-preserved"
        diagnostic_targets = [
            item.get("targetId")
            for item in document.get("diagnostics", [])
            if isinstance(item, dict) and item.get("diagnosticId") in diagnostics and isinstance(item.get("targetId"), str)
        ]
        emitted = ext_refs + resource_refs + ([part_by_object.get(font["object"])] if part_by_object.get(font["object"]) else [])
        emitted.extend(_entity_refs_for_targets(document, diagnostic_targets))
        records.append(_source_record("pdf", "pdf-font-cmap", {"fontObject": identifier}, {"fontObject": identifier, "mappingStatus": font.get("mappingStatus"), "diagnosticCodes": sorted(_dedupe([item.get("code") for item in document.get("diagnostics", []) if isinstance(item, dict) and item.get("diagnosticId") in diagnostics and isinstance(item.get("code"), str)]))}, disposition, "emitted-font-cmap-extension" if ext_refs else "font-cmap-not-emitted", emitted=sorted(_dedupe(emitted)), diagnostics=diagnostics, feature_hints=["font-mapping"], semantic_disposition="non-preserved" if font.get("mappingStatus") != "preserved" else "extension"))
    page_paths: dict[int, list[str]] = {}
    page_glyphs: dict[int, list[str]] = {}
    for node in document.get("nodes", []):
        if not isinstance(node, dict) or not isinstance(node.get("nodeId"), str):
            continue
        locators = _source_map_locators(document).get(node["nodeId"], [])
        for locator in locators:
            if not isinstance(locator, dict) or not isinstance(locator.get("page"), int):
                continue
            if node.get("kind") == "path":
                page_paths.setdefault(locator["page"], []).append(node["nodeId"])
            if node.get("kind") == "glyph":
                page_glyphs.setdefault(locator["page"], []).append(node["nodeId"])
    graphics_extensions: dict[tuple[int, str], list[str]] = {}
    for extension in document.get("extensions", []):
        if not isinstance(extension, dict) or extension.get("type") != "graphics-state" or not isinstance(extension.get("extensionId"), str):
            continue
        payload = extension.get("payload", {})
        if isinstance(payload, dict) and isinstance(payload.get("page"), int) and isinstance(payload.get("operator"), str):
            graphics_extensions.setdefault((payload["page"], payload["operator"]), []).append(f"extensions/{extension['extensionId']}")
    text_operators = {"Tj", "TJ", "'", '"', "BT", "ET", "Tf", "Td", "TD", "Tm", "T*"}
    path_operators = {"m", "l", "c", "v", "y", "re", "h", "W", "W*", "n", "S", "s", "f", "F", "f*", "B", "B*", "b", "b*"}
    graphics_operators = {"q", "Q", "cm", "rg", "RG", "g", "G", "k", "K", "w", "J", "j", "M", "d", "ri", "gs", "sh"}
    supported = text_operators | path_operators | graphics_operators
    for page, object_key, stream in _pdf_page_streams(objects):
        operations = _pdf_operations(stream.decode("latin-1", errors="replace"))
        page_targets = _pdf_page_targets(document, page)
        page_ref = _entity_ref(document, page_targets[0]) if page_targets else root_ref
        glyphs = page_glyphs.get(page, [])
        paths = page_paths.get(page, [])
        glyph_cursor = 0
        path_cursor = 0
        active_path: int | None = None
        for operation_index, (operator, operands) in enumerate(operations, start=1):
            locator = {"page": page, "object": f"{object_key[0]} {object_key[1]}", "operatorIndex": operation_index}
            signature = {"page": page, "object": f"{object_key[0]} {object_key[1]}", "operatorIndex": operation_index, "operator": operator, "operands": [str(value) for value in operands]}
            emitted: list[str] = [page_ref] if page_ref else []
            hints: list[str] = []
            diagnostics: list[str] = []
            disposition = "non-preserved"
            reason = "operator-not-represented"
            if operator in graphics_operators:
                extension_ref = (graphics_extensions.get((page, operator)) or [None]).pop(0)
                if extension_ref:
                    emitted.append(extension_ref)
                    disposition = "extension"
                    reason = "emitted-graphics-state-extension"
                    hints.append("graphics-state")
            elif operator in text_operators and glyphs:
                selected = glyphs[min(glyph_cursor, len(glyphs) - 1)]
                glyph_cursor += 1 if operator in {"Tj", "TJ", "'", '"'} else 0
                emitted.extend(_entity_refs_for_targets(document, [selected]))
                disposition = "approximated"
                reason = "emitted-glyph-and-provenance"
                hints.append("text-glyph")
                glyph_item = _entity_item(document, _entity_ref(document, selected) or "")
                diagnostics.extend([item.get("diagnosticId") for item in document.get("diagnostics", []) if isinstance(item, dict) and item.get("targetId") == selected and isinstance(item.get("diagnosticId"), str)])
            elif operator in path_operators and paths:
                selected = paths[min(path_cursor, len(paths) - 1)]
                emitted.extend(_entity_refs_for_targets(document, [selected]))
                disposition = "core"
                reason = "emitted-path-or-clipping-geometry"
                hints.append("clipping" if any(item.get("targetId") == selected and item.get("feature") == "clipping" for item in document.get("conversion", {}).get("features", []) if isinstance(item, dict)) else "path")
                active_path = path_cursor
                if operator in {"n", "S", "s", "f", "F", "f*", "B", "B*", "b", "b*"}:
                    path_cursor += 1
            elif operator not in supported:
                diagnostics = _pdf_diagnostic_ids(document, "DFIR-PDF-OPERATOR-UNSUPPORTED", page_targets)
                emitted = [page_ref] if page_ref else []
                disposition = "unsupported"
                reason = "unsupported-operator-diagnostic"
                hints.append("unsupported-operator")
            records.append(_source_record("pdf", "pdf-operator", locator, signature, disposition, reason, emitted=sorted(_dedupe(emitted)), diagnostics=diagnostics, feature_hints=hints, semantic_disposition=disposition))
    return records


def _markdown_kind(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("#"):
        return "heading"
    if stripped.startswith(":::"):
        return "directive"
    if stripped.startswith(("- ", "* ", "+ ")):
        return "list-item"
    if stripped.startswith(("```", "~~~")):
        return "code-fence"
    if stripped.startswith(">"):
        return "blockquote"
    if stripped.startswith("[") and "]:" in stripped:
        return "reference-definition"
    if "|" in stripped:
        return "table-or-inline-pipe"
    return "text"


def _markdown_line_targets(document: dict[str, Any], line_number: int) -> list[str]:
    targets: list[str] = []
    for item in document.get("sourceMaps", []):
        if not isinstance(item, dict) or not isinstance(item.get("targetId"), str):
            continue
        locator = item.get("locator", {})
        if isinstance(locator, dict) and locator.get("kind") == "markdown" and isinstance(locator.get("lineStart"), int) and isinstance(locator.get("lineEnd"), int) and locator["lineStart"] <= line_number <= locator["lineEnd"]:
            targets.append(item["targetId"])
    return _dedupe(targets)


def _markdown_extension_targets(document: dict[str, Any], line_number: int, line: str, ranges: dict[str, tuple[int, int]]) -> list[str]:
    targets: list[str] = []
    text = line.strip()
    for extension in document.get("extensions", []):
        if not isinstance(extension, dict) or not isinstance(extension.get("extensionId"), str):
            continue
        payload = extension.get("payload", {})
        payload_text = _json_key(payload)
        extension_type = extension.get("type")
        include = line in payload_text or text in payload_text
        if extension_type == "front-matter" and ranges.get("front-matter", (0, -1))[0] <= line_number <= ranges.get("front-matter", (0, -1))[1]:
            include = True
        if extension_type == "unsupported-directive" and ranges.get("directive", (0, -1))[0] <= line_number <= ranges.get("directive", (0, -1))[1]:
            include = True
        if extension_type == "code-block" and ranges.get("code-block", (0, -1))[0] <= line_number <= ranges.get("code-block", (0, -1))[1]:
            include = True
        if extension_type == "reference-definition" and isinstance(payload, dict) and any(str(payload.get(key, "")) and str(payload.get(key, "")) in line for key in ("label", "destination", "title")):
            include = True
        if include:
            targets.append(extension["extensionId"])
    return _dedupe(targets)


def _markdown_line_diagnostics(
    document: dict[str, Any],
    line_number: int,
    line: str,
    targets: list[str],
    lines: list[str],
) -> list[dict[str, Any]]:
    """Return parser diagnostics whose actual source locator is this line.

    Parser-level diagnostics often have only a document/root target rather
    than a token target.  In that case the source bytes are the authority for
    locating the occurrence.  The NUL-character diagnostic is deliberately
    matched against the actual NUL byte, so a diagnostic cannot be attached to
    an arbitrary Markdown line merely because the report contains a token.
    """

    source_maps = _source_map_locators(document)
    source_maps_by_id = _source_maps_by_id(document)
    target_set = set(targets)
    root_id = document.get("rootNodeId")
    nul_lines = {index for index, value in enumerate(lines, start=1) if "\x00" in value}
    nonempty_lines = [index for index, value in enumerate(lines, start=1) if value.strip()]
    directive_opening_lines = {
        index
        for index, value in enumerate(lines, start=1)
        if any(
            isinstance(extension, dict)
            and extension.get("type") == "unsupported-directive"
            and isinstance(extension.get("payload"), dict)
            and extension["payload"].get("opening") == value
            for extension in document.get("extensions", [])
        )
    }
    result: list[dict[str, Any]] = []
    for diagnostic in document.get("diagnostics", []):
        if not isinstance(diagnostic, dict) or not isinstance(diagnostic.get("diagnosticId"), str):
            continue
        diagnostic_id = diagnostic["diagnosticId"]
        code = str(diagnostic.get("code", ""))
        message = str(diagnostic.get("message", ""))
        target = diagnostic.get("targetId")
        source_map_id = diagnostic.get("sourceMapId")
        has_source_map_id = isinstance(source_map_id, str)
        source_map = source_maps_by_id.get(source_map_id) if has_source_map_id else None
        resolved_target = source_map.get("targetId") if isinstance(source_map, dict) else None
        source_map_invalid = has_source_map_id and not isinstance(source_map, dict)
        if isinstance(target, str) and isinstance(resolved_target, str) and target != resolved_target:
            # A diagnostic cannot claim two different source identities.
            source_map = None
            resolved_target = None
            source_map_invalid = True
        effective_target = target if isinstance(target, str) else resolved_target
        explicit_locator = diagnostic.get("sourceLocator", diagnostic.get("locator", {}))
        explicit_line_match = False
        if isinstance(explicit_locator, dict):
            explicit_line = explicit_locator.get("line", explicit_locator.get("lineStart"))
            explicit_line_end = explicit_locator.get("lineEnd", explicit_line)
            explicit_line_match = isinstance(explicit_line, int) and isinstance(explicit_line_end, int) and explicit_line <= line_number <= explicit_line_end
        diagnostic_code = code.upper()
        source_abort_diagnostic = "NUL" in diagnostic_code or "SOURCE-ABORTED" in diagnostic_code or "SOURCE_ABORTED" in diagnostic_code
        source_map_line_match = isinstance(source_map, dict) and _markdown_source_map_matches_signature(source_map, line_number, line)
        target_match = isinstance(effective_target, str) and effective_target in target_set
        if has_source_map_id:
            # Once a diagnostic names a source map, only that map's locator is
            # authoritative.  Never widen it to every map for the target.
            target_match = not source_map_invalid and target_match and source_map_line_match
        elif isinstance(effective_target, str):
            for locator in source_maps.get(effective_target, []):
                if isinstance(locator, dict) and locator.get("lineStart", 0) <= line_number <= locator.get("lineEnd", -1):
                    target_match = True
        nul_match = line_number in nul_lines and ("NUL" in diagnostic_code or "NUL" in message.upper() or "CHARACTER" in diagnostic_code)
        source_cause_match = line_number in nul_lines
        document_fallback = isinstance(effective_target, str) and effective_target == root_id and len(nonempty_lines) == 1 and line_number == nonempty_lines[0]
        if source_abort_diagnostic:
            # NUL/source-aborted diagnostics are source-cause diagnostics.  A
            # root target or a document-wide source map is not a locator for
            # every line; use the actual NUL line, a single-line source map,
            # or an explicit diagnostic locator only.
            matches_line = (
                (explicit_line_match and not has_source_map_id)
                or (source_cause_match and not source_map_invalid and (not has_source_map_id or source_map_line_match))
            )
        else:
            matches_line = target_match or (explicit_line_match and not has_source_map_id) or (document_fallback and not has_source_map_id)
        if "DIRECTIVE" in diagnostic_code and line_number not in directive_opening_lines:
            # Directive diagnostics describe the opening syntax.  The body
            # and closing delimiter are separate source occurrences of the
            # emitted unsupported-directive extension, not duplicate parser
            # diagnostic locations.
            matches_line = False
        if matches_line:
            result.append({
                "diagnosticId": diagnostic_id,
                "code": code,
                "targetId": effective_target,
                "sourceMapId": diagnostic.get("sourceMapId"),
                "message": message,
            })
    return result


def _markdown_source_features(path: Path, document: dict[str, Any]) -> list[dict[str, Any]]:
    text = Path(path).read_text(encoding="utf-8")
    lines = text.splitlines()
    conversion = document.get("conversion", {})
    input_features = conversion.get("features", []) if isinstance(conversion, dict) else []
    input_failed = (
        isinstance(conversion, dict)
        and conversion.get("status") == "failed"
        and any(
            isinstance(item, dict)
            and item.get("feature") == "input"
            and item.get("status") == "failed"
            for item in input_features
        )
    )
    input_diagnostics = [
        item.get("diagnosticId")
        for item in document.get("diagnostics", [])
        if isinstance(item, dict)
        and isinstance(item.get("diagnosticId"), str)
        and (
            item.get("code") == "DFIR-MARKDOWN-ADAPTER-FAILED"
            or item.get("diagnosticId") in {
                diagnostic_id
                for feature in input_features
                if isinstance(feature, dict) and feature.get("feature") == "input"
                for diagnostic_id in feature.get("diagnosticIds", [])
                if isinstance(diagnostic_id, str)
            }
        )
    ]
    if input_failed and input_diagnostics:
        # The adapter failed before Markdown parsing.  The byte input is one
        # failed source occurrence; inventing ordinary line occurrences would
        # falsely claim that those lines were diagnosed or emitted.
        root_ref = _entity_ref(document, document.get("rootNodeId"))
        return [_source_record(
            "markdown",
            "markdown-input",
            {"path": Path(path).name},
            {"path": Path(path).name, "status": "failed", "diagnosticCodes": [
                item.get("code")
                for item in document.get("diagnostics", [])
                if isinstance(item, dict) and item.get("diagnosticId") in input_diagnostics and isinstance(item.get("code"), str)
            ]},
            "non-preserved",
            "input-adapter-failed-before-markdown-parse",
            emitted=[root_ref] if root_ref else [],
            diagnostics=sorted(_dedupe(input_diagnostics)),
            feature_hints=["input"],
            semantic_disposition="non-preserved",
        )]
    if not lines or not document.get("nodes"):
        diagnostics = [item.get("diagnosticId") for item in document.get("diagnostics", []) if isinstance(item, dict) and isinstance(item.get("diagnosticId"), str)]
        root_ref = _entity_ref(document, document.get("rootNodeId"))
        return [_source_record("markdown", "markdown-input", {"path": Path(path).name}, {"path": Path(path).name}, "non-preserved", "no-emitted-markdown-construct", emitted=[root_ref] if root_ref else [], diagnostics=diagnostics)]
    ranges: dict[str, tuple[int, int]] = {}
    if lines and lines[0].strip() == "---":
        end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
        if end is not None:
            ranges["front-matter"] = (1, end + 1)
    directive_start = next((index + 1 for index, line in enumerate(lines) if line.startswith(":::")), None)
    if directive_start is not None:
        directive_end = next((index + 1 for index in range(directive_start, len(lines)) if lines[index].startswith(":::")), len(lines))
        ranges["directive"] = (directive_start, directive_end)
    fence_start = next((index + 1 for index, line in enumerate(lines) if line.lstrip().startswith(("```", "~~~"))), None)
    if fence_start is not None:
        fence_end = next((index + 1 for index in range(fence_start, len(lines)) if lines[index].lstrip().startswith(("```", "~~~"))), len(lines))
        ranges["code-block"] = (fence_start, fence_end)
    root_ref = _entity_ref(document, document.get("rootNodeId"))
    records: list[dict[str, Any]] = [_source_record("markdown", "markdown-document", {"path": Path(path).name}, {"document": Path(path).name}, "core" if root_ref else "non-preserved", "document-root-emission" if root_ref else "document-root-not-emitted", emitted=[root_ref] if root_ref else [])]
    source_maps = _source_map_locators(document)
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        kind = _markdown_kind(line)
        targets = _markdown_line_targets(document, line_number)
        # A paragraph can span multiple source lines while its adapter source
        # map is anchored at the first line.  Recover the owning run/node from
        # the emitted source text value, not from token presence in the report.
        for text_item in document.get("texts", []):
            if not isinstance(text_item, dict) or not isinstance(text_item.get("value"), str) or line not in text_item["value"].splitlines():
                continue
            for node in document.get("nodes", []):
                if not isinstance(node, dict) or text_item.get("textId") not in node.get("textIds", []):
                    continue
                if isinstance(node.get("nodeId"), str):
                    targets.append(node["nodeId"])
        extension_ids = _markdown_extension_targets(document, line_number, line, ranges)
        parser_diagnostics = _markdown_line_diagnostics(document, line_number, line, targets, lines)
        diagnostic_ids = [item["diagnosticId"] for item in parser_diagnostics]
        diagnostic_targets = [item["targetId"] for item in parser_diagnostics if isinstance(item.get("targetId"), str)]
        for annotation in document.get("annotations", []):
            if isinstance(annotation, dict) and isinstance(annotation.get("annotationId"), str) and isinstance(annotation.get("body"), str) and annotation["body"] and annotation["body"] in line:
                targets.append(annotation["annotationId"])
        target_refs = _entity_refs_for_targets(document, targets + extension_ids + diagnostic_targets) + _node_ancestor_refs(document, targets)
        if kind in {"directive", "code-fence", "reference-definition"} and root_ref:
            target_refs.append(root_ref)
        feature_hints = _feature_hints(document, targets + diagnostic_targets, diagnostic_ids)
        diagnostics: list[str] = list(diagnostic_ids)
        if kind == "directive":
            feature_hints.append("directive")
        target_refs = sorted(_dedupe(target_refs))
        unsupported_directive_target = any(
            isinstance(_entity_item(document, f"extensions/{extension_id}"), dict)
            and _entity_item(document, f"extensions/{extension_id}").get("type") == "unsupported-directive"
            for extension_id in extension_ids
        )
        if parser_diagnostics:
            # A parser diagnostic owns this source occurrence even when the
            # adapter also emitted a recoverable text/node representation.
            # The occurrence is therefore not falsely advertised as core.
            disposition = "unsupported" if kind == "directive" else "non-preserved"
            reason = "parser-diagnostic-source-occurrence"
        elif kind == "directive" and not unsupported_directive_target:
            disposition = "unsupported"
            reason = "unsupported-directive-diagnostic-and-extension"
        elif unsupported_directive_target:
            # The opening line owns the parser diagnostic.  Body and closing
            # lines are preserved as opaque extension payload facts and must
            # not inherit that diagnostic merely because the block target is
            # shared.
            disposition = "extension"
            reason = "emitted-unsupported-directive-extension"
        elif target_refs:
            disposition = "extension" if extension_ids and not targets else "core"
            reason = "emitted-source-map-and-extension" if extension_ids else "emitted-source-map"
        else:
            disposition = "non-preserved"
            reason = "source-line-not-mapped-to-ir"
        semantic = "unsupported" if disposition == "unsupported" else disposition
        locator = {"lineStart": line_number, "lineEnd": line_number, "columnStart": 1, "columnEnd": len(line) + 1, "tokenStart": 0, "tokenEnd": len(line)}
        diagnostic_codes = [
            item.get("code")
            for item in document.get("diagnostics", [])
            if isinstance(item, dict) and item.get("diagnosticId") in diagnostics and isinstance(item.get("code"), str)
        ]
        signature = {
            "line": line_number,
            "text": line,
            "kind": kind,
            "diagnosticCodes": sorted(_dedupe(diagnostic_codes)),
        }
        records.append(_source_record("markdown", "markdown-line-token", locator, signature, disposition, reason, emitted=target_refs, diagnostics=sorted(_dedupe(diagnostics)), feature_hints=sorted(_dedupe(feature_hints)), semantic_disposition=semantic))
    return records


def source_feature_inventory(path: Path, format_name: str, document: dict[str, Any]) -> list[dict[str, Any]]:
    """Inventory source occurrences and attach only actual IR entity IDs."""

    path = Path(path)
    if format_name in {"docx", "xlsx"}:
        return _package_source_features(path, document, format_name)
    if format_name == "pdf":
        return _pdf_source_features(path, document)
    if format_name == "markdown":
        return _markdown_source_features(path, document)
    return []


def _validate_reference_set(document: dict[str, Any], references: list[str], index_ids: set[tuple[str, str]], label: str, failures: list[dict[str, Any]]) -> None:
    for reference in references:
        split = _split_entity_ref(reference)
        if split is None:
            failures.append({"code": "ENTITY_REFERENCE_MALFORMED", "label": label, "reference": reference})
            continue
        collection, identifier = split
        if _entity_item(document, reference) is None:
            failures.append({"code": "ENTITY_NOT_EMITTED", "label": label, "reference": reference})
        if (collection, identifier) not in index_ids:
            failures.append({"code": "ENTITY_NOT_QUERYABLE", "label": label, "reference": reference})


def _markdown_parser_diagnostic_mapping(document: dict[str, Any], record: dict[str, Any], emitted: list[str]) -> bool:
    """Check a Markdown parser diagnostic as a source occurrence mapping.

    This is the explicit fallback for parser diagnostics whose target has no
    source map or text entity.  A matching diagnostic ID alone is insufficient:
    the diagnostic code must be declared by the source signature, its target
    must be one of the emitted entities, and the locator must identify exactly
    the signed source line.  NUL diagnostics additionally require the actual
    NUL character in that line.
    """

    if record.get("sourceKind") != "markdown-line-token":
        return False
    source_disposition = record.get("sourceDisposition")
    if source_disposition not in {"non-preserved", "unsupported", "failed"}:
        return False
    mapping = record.get("mapping", {})
    diagnostic_ids = mapping.get("diagnosticIds", []) if isinstance(mapping, dict) else []
    if not isinstance(diagnostic_ids, list) or not diagnostic_ids:
        return False
    signature = record.get("sourceSignature", {})
    locator = record.get("sourceLocator", {})
    if not isinstance(signature, dict) or not isinstance(locator, dict):
        return False
    line = signature.get("line")
    source_text = signature.get("text")
    if not isinstance(line, int) or not isinstance(source_text, str):
        return False
    if not (
        locator.get("lineStart") == line
        and locator.get("lineEnd") == line
        and locator.get("columnStart") == 1
        and locator.get("columnEnd") == len(source_text) + 1
        and locator.get("tokenStart") == 0
        and locator.get("tokenEnd") == len(source_text)
    ):
        return False
    declared_codes = signature.get("diagnosticCodes", [])
    if not isinstance(declared_codes, list) or not declared_codes:
        return False
    emitted_identifiers = {
        split[1]
        for reference in emitted
        if isinstance(reference, str)
        for split in [_split_entity_ref(reference)]
        if split is not None
    }
    diagnostics = {
        item.get("diagnosticId"): item
        for item in document.get("diagnostics", [])
        if isinstance(item, dict) and isinstance(item.get("diagnosticId"), str)
    }
    source_maps = _source_maps_by_id(document)
    for diagnostic_id in diagnostic_ids:
        diagnostic = diagnostics.get(diagnostic_id)
        if not isinstance(diagnostic, dict):
            continue
        code = diagnostic.get("code")
        target_id = diagnostic.get("targetId")
        source_map_id = diagnostic.get("sourceMapId")
        source_map = source_maps.get(source_map_id) if isinstance(source_map_id, str) else None
        resolved_target = source_map.get("targetId") if isinstance(source_map, dict) else None
        if isinstance(target_id, str) and isinstance(resolved_target, str) and target_id != resolved_target:
            continue
        mapped_target = target_id if isinstance(target_id, str) else resolved_target
        if not isinstance(code, str) or code not in declared_codes or not isinstance(mapped_target, str) or mapped_target not in emitted_identifiers:
            continue
        if isinstance(source_map_id, str):
            if not isinstance(source_map, dict) or not _markdown_source_map_matches_signature(source_map, line, source_text):
                continue
        else:
            explicit_locator = diagnostic.get("sourceLocator", diagnostic.get("locator"))
            if isinstance(explicit_locator, dict):
                explicit_start = explicit_locator.get("line", explicit_locator.get("lineStart"))
                explicit_end = explicit_locator.get("lineEnd", explicit_start)
                if not isinstance(explicit_start, int) or not isinstance(explicit_end, int) or not explicit_start <= line <= explicit_end:
                    continue
            elif "NUL" not in code.upper() or "\x00" not in source_text:
                # A root target without a real source locator is not enough
                # to establish a parser source occurrence.  NUL is the sole
                # content-proven exception because the signed line itself
                # contains the exact parser cause.
                continue
        if "NUL" in code.upper() and "\x00" not in source_text:
            continue
        return True
    return False


def _is_unparsed_package_source(document: dict[str, Any], record: dict[str, Any]) -> bool:
    """Allow only diagnostic-backed package occurrences for whole-input failure."""

    if record.get("sourceKind") not in {"package-part", "package-relationship-part", "package-relationship"}:
        return False
    if record.get("sourceDisposition") in {"core", "extension", "observation", "omitted-by-policy"}:
        return False
    failure_ids = set(_whole_input_failure_diagnostics(document))
    mapping = record.get("mapping", {})
    record_diagnostics = set(mapping.get("diagnosticIds", [])) if isinstance(mapping, dict) else set()
    return bool(failure_ids and failure_ids.intersection(record_diagnostics))


def _validate_source_content(document: dict[str, Any], record: dict[str, Any], failures: list[dict[str, Any]]) -> None:
    kind = record.get("sourceKind")
    signature = record.get("sourceSignature", {})
    emitted = record.get("mapping", {}).get("emittedEntityIds", [])
    items = [_entity_item(document, reference) for reference in emitted]
    items = [item for item in items if isinstance(item, dict)]
    if document.get("conversion", {}).get("status") == "failed" and not document.get("parts"):
        # The input-failure diagnostic is the only authoritative result.  Do
        # not demand package-part/relation entities from a conversion that
        # deliberately stopped before parsing the package.
        return
    if kind == "package-part" and record.get("sourceDisposition") != "omitted-by-policy":
        path = record.get("sourceLocator", {}).get("path")
        if not any(item.get("name") == path for item in items if item.get("partId")):
            if not _is_unparsed_package_source(document, record):
                failures.append({"code": "PACKAGE_PART_MAPPING_CONTENT", "sourceOccurrenceId": record.get("sourceOccurrenceId"), "detail": f"no emitted part has name {path!r}"})
    elif kind == "package-relationship":
        relationship_id = signature.get("relationshipId") if isinstance(signature, dict) else None
        if relationship_id and not any(item.get("relationId") and relationship_id in str(item.get("relationId")) for item in items):
            # Relationship IDs are normalized by the adapter.  The emitted
            # relation still has to be present; a missing relation is never
            # accepted merely because the source XML token was found.
            if not any(item.get("relationId") for item in items) and not _is_unparsed_package_source(document, record):
                failures.append({"code": "PACKAGE_RELATION_MAPPING_CONTENT", "sourceOccurrenceId": record.get("sourceOccurrenceId"), "detail": "relationship source occurrence has no emitted relation"})
    elif kind == "pdf-object":
        object_name = f"{signature.get('object')} obj" if isinstance(signature, dict) else ""
        if not any(item.get("name") == object_name for item in items):
            failures.append({"code": "PDF_OBJECT_MAPPING_CONTENT", "sourceOccurrenceId": record.get("sourceOccurrenceId"), "detail": f"no emitted PDF object part {object_name!r}"})
    elif kind == "pdf-operator":
        operator = signature.get("operator") if isinstance(signature, dict) else None
        graphics = [item for item in items if item.get("type") == "graphics-state" and isinstance(item.get("payload"), dict)]
        if graphics and operator not in {item.get("payload", {}).get("operator") for item in graphics}:
            failures.append({"code": "PDF_OPERATOR_MAPPING_CONTENT", "sourceOccurrenceId": record.get("sourceOccurrenceId"), "detail": f"graphics-state operator {operator!r} was not emitted"})
        if record.get("sourceDisposition") in {"core", "extension", "approximated"} and not items:
            failures.append({"code": "PDF_OPERATOR_EMPTY_EMISSION", "sourceOccurrenceId": record.get("sourceOccurrenceId")})
    elif kind == "markdown-line-token":
        line = signature.get("line") if isinstance(signature, dict) else None
        source_text = signature.get("text", "") if isinstance(signature, dict) else ""
        locators = _source_map_locators(document)
        mapped = False
        for reference in emitted:
            split = _split_entity_ref(reference)
            if split is None:
                continue
            identifier = split[1]
            for locator in locators.get(identifier, []):
                if isinstance(locator, dict) and locator.get("lineStart", 0) <= line <= locator.get("lineEnd", -1):
                    mapped = True
            item = _entity_item(document, reference)
            if isinstance(item, dict) and item.get("type") == "unsupported-directive":
                payload = item.get("payload", {})
                mapped = mapped or (isinstance(payload, dict) and (payload.get("opening") == source_text or source_text in payload.get("body", [])))
            if isinstance(item, dict) and isinstance(item.get("textIds"), list):
                mapped = mapped or any(isinstance(text_item, dict) and text_item.get("textId") in item["textIds"] and isinstance(text_item.get("value"), str) and source_text in text_item["value"] for text_item in document.get("texts", []))
            if isinstance(item, dict) and split[0] == "annotations" and isinstance(item.get("body"), str):
                mapped = mapped or bool(item["body"]) and item["body"] in source_text
        if record.get("sourceDisposition") == "unsupported":
            mapped = mapped and any(_entity_item(document, reference).get("type") == "unsupported-directive" for reference in emitted if isinstance(_entity_item(document, reference), dict))
        if not mapped and any(isinstance(item, dict) and item.get("type") == "front-matter" for item in items):
            front_matter = next(item for item in items if isinstance(item, dict) and item.get("type") == "front-matter")
            entries = front_matter.get("payload", {}).get("entries", []) if isinstance(front_matter.get("payload"), dict) else []
            mapped = source_text.strip() in {"---", "..."} or any(isinstance(entry, dict) and (str(entry.get("key", "")) in source_text or str(entry.get("value", "")) in source_text) for entry in entries)
        if not mapped and any(isinstance(item, dict) and item.get("type") == "code-block" for item in items):
            code_block = next(item for item in items if isinstance(item, dict) and item.get("type") == "code-block")
            payload = code_block.get("payload", {}) if isinstance(code_block.get("payload"), dict) else {}
            mapped = source_text.strip().startswith(("```", "~~~")) or source_text in str(payload.get("content", "")) or source_text.strip() == str(payload.get("fence", ""))
        if not mapped and any(isinstance(item, dict) and item.get("type") == "reference-definition" for item in items):
            reference_extension = next(item for item in items if isinstance(item, dict) and item.get("type") == "reference-definition")
            payload = reference_extension.get("payload", {}) if isinstance(reference_extension.get("payload"), dict) else {}
            mapped = any(str(payload.get(key, "")) and str(payload.get(key, "")) in source_text for key in ("label", "destination", "title"))
        parser_diagnostic_mapped = _markdown_parser_diagnostic_mapping(document, record, emitted)
        parser_diagnostic_ids = [
            diagnostic_id
            for diagnostic_id in record.get("mapping", {}).get("diagnosticIds", [])
            if any(
                isinstance(item, dict)
                and item.get("diagnosticId") == diagnostic_id
                and isinstance(item.get("code"), str)
                and (
                    "NUL" in item["code"].upper()
                    or item.get("phase") == "parse"
                    or (record.get("sourceDisposition") == "unsupported" and item["code"].startswith("DFIR-MD-"))
                )
                for item in document.get("diagnostics", [])
            )
        ]
        if parser_diagnostic_ids and not parser_diagnostic_mapped:
            failures.append({
                "code": "MARKDOWN_PARSER_DIAGNOSTIC_MAPPING_CONTENT",
                "sourceOccurrenceId": record.get("sourceOccurrenceId"),
                "diagnosticIds": parser_diagnostic_ids,
                "detail": "parser diagnostic ID/code/target/locator/signature did not match the source occurrence",
            })
        mapped = mapped or parser_diagnostic_mapped
        if not mapped:
            failures.append({"code": "MARKDOWN_LINE_MAPPING_CONTENT", "sourceOccurrenceId": record.get("sourceOccurrenceId"), "detail": f"line {line} is not represented by its emitted source map/extension"})


def validate_source_feature_closure(document: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    """Fail-closed validation shared by corpus, E2E, and strict gates.

    The source inventory record is keyed by ``sourceOccurrenceId``.  Its
    ``mapping`` object contains ``irDispositionIds``, ``emittedEntityIds``,
    ``queryEntityIds``, and ``diagnosticIds``.  The corresponding IR
    disposition record is keyed by ``irDispositionId`` and contains the
    reverse ``mapping.sourceOccurrenceIds`` plus the same emission/query/
    diagnostic fields.  No token-presence or array-nonempty check can satisfy
    this function without the referenced entities actually existing in the IR
    and the rebuilt query index.
    """

    failures: list[dict[str, Any]] = []
    source_records = evidence.get("sourceFeatureInventory", [])
    ir_records = evidence.get("dispositions", [])
    if not isinstance(source_records, list) or not source_records:
        failures.append({"code": "SOURCE_OCCURRENCE_INVENTORY_EMPTY"})
        source_records = []
    if not isinstance(ir_records, list):
        failures.append({"code": "IR_DISPOSITION_INVENTORY_MALFORMED"})
        ir_records = []
    source_by_id = {item.get("sourceOccurrenceId"): item for item in source_records if isinstance(item, dict) and isinstance(item.get("sourceOccurrenceId"), str)}
    ir_by_id = {item.get("irDispositionId"): item for item in ir_records if isinstance(item, dict) and isinstance(item.get("irDispositionId"), str)}
    if len(source_by_id) != len(source_records):
        failures.append({"code": "SOURCE_OCCURRENCE_ID_NOT_UNIQUE"})
    if len(ir_by_id) != len(ir_records):
        failures.append({"code": "IR_DISPOSITION_ID_NOT_UNIQUE"})
    try:
        index = rebuild_index(document)
        index_ids = {(item.get("collection"), item.get("id")) for item in index.get("entities", []) if isinstance(item, dict)}
    except Exception as exc:  # pragma: no cover - defensive fail-closed path
        index_ids = set()
        failures.append({"code": "QUERY_INDEX_BUILD_FAILED", "detail": str(exc)})
    for source in source_records:
        if not isinstance(source, dict):
            failures.append({"code": "SOURCE_OCCURRENCE_RECORD_MALFORMED"})
            continue
        mapping = source.get("mapping")
        if not isinstance(mapping, dict):
            failures.append({"code": "SOURCE_MAPPING_MISSING", "sourceOccurrenceId": source.get("sourceOccurrenceId")})
            continue
        if not isinstance(source.get("sourceLocator"), dict) or not source.get("reason"):
            failures.append({"code": "SOURCE_LOCATOR_OR_REASON_MISSING", "sourceOccurrenceId": source.get("sourceOccurrenceId")})
        emitted = mapping.get("emittedEntityIds", [])
        queryable = mapping.get("queryEntityIds", [])
        diagnostics = mapping.get("diagnosticIds", [])
        source_targets = mapping.get("sourceTargetIds", [])
        if not isinstance(emitted, list) or not isinstance(queryable, list) or not isinstance(diagnostics, list) or not isinstance(source_targets, list):
            failures.append({"code": "SOURCE_MAPPING_FIELDS_MALFORMED", "sourceOccurrenceId": source.get("sourceOccurrenceId")})
            continue
        for target_id in source_targets:
            if not isinstance(target_id, str) or _entity_ref(document, target_id) is None:
                failures.append({"code": "SOURCE_DIRECT_TARGET_NOT_EMITTED", "sourceOccurrenceId": source.get("sourceOccurrenceId"), "targetId": target_id})
        _validate_reference_set(document, emitted, index_ids, f"source:{source.get('sourceOccurrenceId')}:emitted", failures)
        _validate_reference_set(document, queryable, index_ids, f"source:{source.get('sourceOccurrenceId')}:query", failures)
        if not set(emitted).issubset(set(queryable)):
            failures.append({"code": "SOURCE_QUERY_MAPPING_INCOMPLETE", "sourceOccurrenceId": source.get("sourceOccurrenceId")})
        diagnostic_ids = {item.get("diagnosticId") for item in document.get("diagnostics", []) if isinstance(item, dict)}
        if not set(diagnostics).issubset(diagnostic_ids):
            failures.append({"code": "SOURCE_DIAGNOSTIC_NOT_EMITTED", "sourceOccurrenceId": source.get("sourceOccurrenceId")})
        emitted_identifiers = {
            split[1]
            for reference in emitted
            if isinstance(reference, str)
            for split in [_split_entity_ref(reference)]
            if split is not None
        }
        signature = source.get("sourceSignature", {}) if isinstance(source.get("sourceSignature"), dict) else {}
        declared_codes = set(signature.get("diagnosticCodes", [])) if isinstance(signature.get("diagnosticCodes", []), list) else set()
        diagnostics_by_id = {
            item.get("diagnosticId"): item
            for item in document.get("diagnostics", [])
            if isinstance(item, dict) and isinstance(item.get("diagnosticId"), str)
        }
        for diagnostic_id in diagnostics:
            diagnostic = diagnostics_by_id.get(diagnostic_id)
            if not isinstance(diagnostic, dict):
                continue
            target_id = diagnostic.get("targetId")
            if emitted and isinstance(target_id, str) and target_id not in emitted_identifiers:
                failures.append({
                    "code": "SOURCE_DIAGNOSTIC_TARGET_NOT_MAPPED",
                    "sourceOccurrenceId": source.get("sourceOccurrenceId"),
                    "diagnosticId": diagnostic_id,
                    "targetId": target_id,
                })
            code = diagnostic.get("code")
            if declared_codes and isinstance(code, str) and code not in declared_codes:
                failures.append({
                    "code": "SOURCE_DIAGNOSTIC_CODE_NOT_IN_SIGNATURE",
                    "sourceOccurrenceId": source.get("sourceOccurrenceId"),
                    "diagnosticId": diagnostic_id,
                    "diagnosticCode": code,
                })
            if source.get("sourceKind") == "markdown-line-token" and isinstance(code, str) and "NUL" in code.upper():
                source_text = signature.get("text") if isinstance(signature.get("text"), str) else ""
                if "\x00" not in source_text:
                    failures.append({
                        "code": "MARKDOWN_DIAGNOSTIC_LOCATOR_CONTENT",
                        "sourceOccurrenceId": source.get("sourceOccurrenceId"),
                        "diagnosticId": diagnostic_id,
                        "detail": "NUL diagnostic is not located on a source line containing the NUL character",
                    })
        dispositions = {source.get("sourceDisposition"), source.get("semanticDisposition"), source.get("disposition")}
        if dispositions.intersection({"core", "extension", "observation"}) and not emitted:
            failures.append({"code": "PRESERVED_SOURCE_HAS_NO_EMISSION", "sourceOccurrenceId": source.get("sourceOccurrenceId")})
        if dispositions.intersection(PARTIAL_STATUSES | {"non-preserved"}) and source.get("sourceDisposition") != "omitted-by-policy" and not diagnostics:
            failures.append({"code": "UNPRESERVED_SOURCE_HAS_NO_DIAGNOSTIC", "sourceOccurrenceId": source.get("sourceOccurrenceId"), "disposition": source.get("sourceDisposition")})
        if source.get("sourceDisposition") == "omitted-by-policy" and not source.get("policyId"):
            failures.append({"code": "POLICY_OMISSION_UNIDENTIFIED", "sourceOccurrenceId": source.get("sourceOccurrenceId")})
        _validate_source_content(document, source, failures)
        for ir_id in mapping.get("irDispositionIds", []):
            if ir_id not in ir_by_id:
                failures.append({"code": "SOURCE_IR_DISPOSITION_NOT_FOUND", "sourceOccurrenceId": source.get("sourceOccurrenceId"), "irDispositionId": ir_id})
                continue
            ir = ir_by_id[ir_id]
            ir_targets = {value for value in ir.get("targetIds", []) if isinstance(value, str)}
            ir_diagnostics = set(ir.get("mapping", {}).get("diagnosticIds", []))
            ir_emitted_ids = _ir_direct_emitted_identifiers(document, ir)
            root_target_only = (
                document.get("rootNodeId") in ir_targets
                and not (ir_targets - {document.get("rootNodeId")})
                and source.get("sourceKind") not in ROOT_SOURCE_KINDS
            )
            emitted_match = set(source_targets).intersection(ir_emitted_ids) if not root_target_only else set()
            if root_target_only or (not set(source_targets).intersection(ir_targets) and not emitted_match and not set(diagnostics).intersection(ir_diagnostics)):
                failures.append({
                    "code": "SOURCE_IR_MAPPING_HAS_NO_DIRECT_IDENTITY",
                    "sourceOccurrenceId": source.get("sourceOccurrenceId"),
                    "irDispositionId": ir_id,
                })
    for ir in ir_records:
        if not isinstance(ir, dict):
            failures.append({"code": "IR_DISPOSITION_RECORD_MALFORMED"})
            continue
        mapping = ir.get("mapping")
        if not isinstance(mapping, dict):
            failures.append({"code": "IR_MAPPING_MISSING", "irDispositionId": ir.get("irDispositionId")})
            continue
        emitted = mapping.get("emittedEntityIds", [])
        queryable = mapping.get("queryEntityIds", [])
        diagnostics = mapping.get("diagnosticIds", [])
        source_ids = mapping.get("sourceOccurrenceIds", [])
        _validate_reference_set(document, emitted if isinstance(emitted, list) else [], index_ids, f"ir:{ir.get('irDispositionId')}:emitted", failures)
        _validate_reference_set(document, queryable if isinstance(queryable, list) else [], index_ids, f"ir:{ir.get('irDispositionId')}:query", failures)
        if isinstance(emitted, list) and isinstance(queryable, list) and not set(emitted).issubset(set(queryable)):
            failures.append({"code": "IR_QUERY_MAPPING_INCOMPLETE", "irDispositionId": ir.get("irDispositionId")})
        status = ir.get("status")
        if status in {"preserved", "normalized", "unavailable"} and not emitted:
            failures.append({"code": "IR_PRESERVED_DISPOSITION_HAS_NO_EMISSION", "irDispositionId": ir.get("irDispositionId")})
        if status in PARTIAL_STATUSES or status == "unavailable":
            if not isinstance(diagnostics, list) or not diagnostics:
                failures.append({"code": "IR_NON_PRESERVED_HAS_NO_DIAGNOSTIC", "irDispositionId": ir.get("irDispositionId")})
        if not isinstance(source_ids, list) or not source_ids:
            failures.append({"code": "IR_DISPOSITION_HAS_NO_SOURCE_OCCURRENCE", "irDispositionId": ir.get("irDispositionId")})
        for source_id in source_ids if isinstance(source_ids, list) else []:
            source = source_by_id.get(source_id)
            if source is None:
                failures.append({"code": "IR_SOURCE_OCCURRENCE_NOT_FOUND", "irDispositionId": ir.get("irDispositionId"), "sourceOccurrenceId": source_id})
                continue
            if ir.get("irDispositionId") not in source.get("mapping", {}).get("irDispositionIds", []):
                failures.append({"code": "IR_SOURCE_MAPPING_NOT_RECIPROCAL", "irDispositionId": ir.get("irDispositionId"), "sourceOccurrenceId": source_id})
            source_mapping = source.get("mapping", {})
            source_targets = set(source_mapping.get("sourceTargetIds", [])) if isinstance(source_mapping, dict) else set()
            ir_targets = {value for value in ir.get("targetIds", []) if isinstance(value, str)}
            ir_diagnostics = set(diagnostics) if isinstance(diagnostics, list) else set()
            source_diagnostics = set(source_mapping.get("diagnosticIds", [])) if isinstance(source_mapping, dict) else set()
            ir_emitted_ids = _ir_direct_emitted_identifiers(document, ir)
            root_target_only = (
                document.get("rootNodeId") in ir_targets
                and not (ir_targets - {document.get("rootNodeId")})
                and source.get("sourceKind") not in ROOT_SOURCE_KINDS
            )
            emitted_match = source_targets.intersection(ir_emitted_ids) if not root_target_only else set()
            if root_target_only or (not source_targets.intersection(ir_targets) and not emitted_match and not source_diagnostics.intersection(ir_diagnostics)):
                failures.append({
                    "code": "IR_SOURCE_MAPPING_HAS_NO_DIRECT_IDENTITY",
                    "irDispositionId": ir.get("irDispositionId"),
                    "sourceOccurrenceId": source_id,
                })
        if isinstance(source_ids, list) and source_ids and isinstance(diagnostics, list) and diagnostics and not emitted:
            diagnostic_sources = [
                source_by_id[source_id]
                for source_id in source_ids
                if source_id in source_by_id and isinstance(source_by_id[source_id], dict)
            ]
            if not any(
                set(diagnostics).intersection(set(source.get("mapping", {}).get("diagnosticIds", [])))
                for source in diagnostic_sources
            ):
                failures.append({
                    "code": "IR_DIAGNOSTIC_SOURCE_MAPPING_MISSING",
                    "irDispositionId": ir.get("irDispositionId"),
                    "diagnosticIds": diagnostics,
                })
    return {
        "status": "passed" if not failures else "failed",
        "checkedSourceOccurrences": len(source_records),
        "checkedIrDispositions": len(ir_records),
        "emittedEntityCount": len({reference for item in source_records if isinstance(item, dict) for reference in item.get("mapping", {}).get("emittedEntityIds", []) if isinstance(reference, str)}),
        "mappingFields": {
            "sourceInventory": ["sourceOccurrenceId", "sourceLocator", "sourceSignature", "mapping.sourceTargetIds", "mapping.irDispositionIds", "mapping.emittedEntityIds", "mapping.queryEntityIds", "mapping.diagnosticIds"],
            "irDisposition": ["irDispositionId", "mapping.sourceOccurrenceIds", "mapping.emittedEntityIds", "mapping.queryEntityIds", "mapping.diagnosticIds"],
        },
        "mismatches": failures,
    }


def query_parity(document: dict[str, Any]) -> dict[str, Any]:
    """Compare every typed direct entity lookup with the rebuilt index."""

    direct_ids: list[tuple[str, str]] = []
    direct_counts: dict[str, int] = {}
    operations: set[str] = set()
    for collection, identifier_key in COLLECTION_KEYS.items():
        values = list_entities(document, collection)
        operations.add(f"list_entities:{collection}")
        direct_counts[collection] = len(values)
        for item in values:
            identifier = item[identifier_key]
            if get_entity(document, collection, identifier)[identifier_key] != identifier:
                raise ValueError(f"typed lookup mismatch: {collection}/{identifier}")
            direct_ids.append((collection, identifier))
            operations.add(f"get_entity:{collection}")
    index = rebuild_index(document)
    index_ids = [(item["collection"], item["id"]) for item in index["entities"]]
    direct_ids.sort()
    index_ids.sort()
    if direct_ids != index_ids:
        missing = sorted(set(direct_ids) - set(index_ids))
        extra = sorted(set(index_ids) - set(direct_ids))
        raise ValueError(f"direct/index entity mismatch: missing={missing}, extra={extra}")
    return {
        "status": "passed",
        "operations": sorted(operations),
        "directEntityCount": len(direct_ids),
        "indexEntityCount": len(index_ids),
        "directEntityIds": [f"{collection}/{identifier}" for collection, identifier in direct_ids],
        "indexEntityIds": [f"{collection}/{identifier}" for collection, identifier in index_ids],
        "directCounts": direct_counts,
        "reverseReferenceCount": len(index["reverseReferences"]),
        "unqueryableFacts": [],
    }


def case_evidence(path: Path, format_name: str, document: dict[str, Any]) -> dict[str, Any]:
    """Build source/emission/query evidence for one converted input."""

    dispositions = feature_dispositions(document)
    parity = query_parity(document)
    source_features = source_feature_inventory(path, format_name, document)
    _attach_mappings(document, source_features, dispositions)
    evidence: dict[str, Any] = {
        "sourceDigest": source_digest(path),
        "inputDigest": file_digest(path) if Path(path).is_file() else None,
        "sourceFeatureIds": [item["sourceOccurrenceId"] for item in source_features],
        "sourceFeatureInventory": source_features,
        "featureInventory": document.get("conversion", {}).get("featureInventory", []),
        "dispositions": dispositions,
        "queryParity": parity,
    }
    closure = validate_source_feature_closure(document, evidence)
    evidence["sourceClosure"] = closure
    evidence["residuals"] = [item for item in dispositions if item.get("residual") is not None] + [item for item in source_features if item.get("residual") is not None] + closure.get("mismatches", [])
    return evidence


def _legacy_query_parity(document: dict[str, Any]) -> dict[str, Any]:
    """Compare every typed direct entity lookup with the rebuilt index."""

    direct_ids: list[tuple[str, str]] = []
    direct_counts: dict[str, int] = {}
    operations: set[str] = set()
    for collection, identifier_key in COLLECTION_KEYS.items():
        values = list_entities(document, collection)
        operations.add(f"list_entities:{collection}")
        direct_counts[collection] = len(values)
        for item in values:
            identifier = item[identifier_key]
            if get_entity(document, collection, identifier)[identifier_key] != identifier:
                raise ValueError(f"typed lookup mismatch: {collection}/{identifier}")
            direct_ids.append((collection, identifier))
            operations.add(f"get_entity:{collection}")
    index = rebuild_index(document)
    index_ids = [(item["collection"], item["id"]) for item in index["entities"]]
    direct_ids.sort()
    index_ids.sort()
    if direct_ids != index_ids:
        missing = sorted(set(direct_ids) - set(index_ids))
        extra = sorted(set(index_ids) - set(direct_ids))
        raise ValueError(f"direct/index entity mismatch: missing={missing}, extra={extra}")
    return {
        "status": "passed",
        "operations": sorted(operations),
        "directEntityCount": len(direct_ids),
        "indexEntityCount": len(index_ids),
        "directEntityIds": [f"{collection}/{identifier}" for collection, identifier in direct_ids],
        "indexEntityIds": [f"{collection}/{identifier}" for collection, identifier in index_ids],
        "directCounts": direct_counts,
        "reverseReferenceCount": len(index["reverseReferences"]),
        "unqueryableFacts": [],
    }


def _legacy_package_source_features(path: Path, document: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = sorted(archive.namelist())
    except (OSError, zipfile.BadZipFile):
        return [{"featureId": "source-package:unreadable", "kind": "package", "disposition": "non-preserved", "reason": "malformed-package"}]
    part_names = {item.get("name") for item in document.get("parts", []) if isinstance(item, dict)}
    records: list[dict[str, Any]] = []
    for name in names:
        if name in part_names:
            disposition = "core"
            reason = "emitted-part"
        elif name.endswith(".rels"):
            disposition = "core"
            reason = "emitted-relationship-graph"
        elif name == "[Content_Types].xml":
            disposition = "omitted-by-policy"
            reason = "package-control-metadata-is-not-a-form-entity"
        else:
            disposition = "non-preserved"
            reason = "package-entry-not-represented"
        records.append({"featureId": f"source-part:{name}", "kind": "package-part", "locator": name, "disposition": disposition, "reason": reason})
    return records


def _legacy_pdf_source_features(path: Path, document: dict[str, Any]) -> list[dict[str, Any]]:
    data = Path(path).read_bytes()
    objects = _pdf_objects(data)
    records: list[dict[str, Any]] = []
    cmap_objects = {font["toUnicodeObject"] for font in _pdf_font_mappings(objects) if font.get("toUnicodeObject") is not None}
    for (number, generation), object_data in sorted(objects.items()):
        identifier = f"{number} {generation}"
        records.append({"featureId": f"source-object:{identifier}", "kind": "indirect-object", "locator": {"object": number, "generation": generation}, "disposition": "core", "reason": "object-graph"})
        for target_number, target_generation in _pdf_references(object_data):
            target = f"{target_number} {target_generation}"
            present = (target_number, target_generation) in objects
            records.append({"featureId": f"source-reference:{identifier}->{target}", "kind": "indirect-reference", "locator": {"from": identifier, "to": target}, "disposition": "core" if present else "non-preserved", "reason": "resolved-object-reference" if present else "missing-object-reference"})
        stream = _decode_stream(object_data)
        if not stream or (number, generation) in cmap_objects:
            continue
        supported = {"BT", "ET", "Tf", "Td", "TD", "Tm", "Tj", "TJ", "m", "l", "c", "v", "y", "h", "W", "W*", "n", "S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "q", "Q", "cm", "re", "rg", "RG", "g", "G", "k", "K", "w", "J", "j", "M", "d", "ri", "gs", "sh"}
        operators = sorted({operator for operator, _ in _pdf_operations(stream.decode("latin-1", errors="replace"))})
        for operator in operators:
            records.append({"featureId": f"source-operator:{identifier}:{operator}", "kind": "content-operator", "locator": {"object": identifier, "operator": operator}, "disposition": "core" if operator in supported else "non-preserved", "reason": "operator-handler-or-diagnostic"})
    for font in _pdf_font_mappings(objects):
        identifier = f"{font['object'][0]} {font['object'][1]}"
        records.append({"featureId": f"source-font-cmap:{identifier}", "kind": "font-cmap", "locator": {"fontObject": identifier}, "disposition": "core" if font["mappingStatus"] == "preserved" else "non-preserved", "reason": "ToUnicode-map" if font["mappingStatus"] == "preserved" else "missing-ToUnicode-map"})
    if not records:
        records.append({"featureId": "source-input:pdf", "kind": "source-input", "locator": {"path": Path(path).name}, "disposition": "non-preserved", "reason": "no-observable-indirect-object"})
    return records


def _legacy_markdown_source_features(path: Path) -> list[dict[str, Any]]:
    text = Path(path).read_text(encoding="utf-8")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            kind = "heading"
        elif stripped.startswith(":::"):
            kind = "directive"
        elif stripped.startswith(("- ", "* ", "+ ")):
            kind = "list-item"
        elif stripped.startswith("```") or stripped.startswith("~~~"):
            kind = "code-fence"
        elif stripped.startswith(">"):
            kind = "blockquote"
        elif stripped.startswith("[") and "]:" in stripped:
            kind = "reference-definition"
        elif "|" in stripped:
            kind = "table-or-inline-pipe"
        else:
            kind = "text"
        records.append({"featureId": f"source-line:{line_number}:{kind}", "kind": kind, "locator": {"lineStart": line_number, "lineEnd": line_number}, "disposition": "core", "reason": "source-span"})
    if not records:
        records.append({"featureId": "source-input:markdown", "kind": "source-input", "locator": {"path": Path(path).name}, "disposition": "non-preserved", "reason": "no-observable-source-line"})
    return records


def _legacy_source_feature_inventory(path: Path, format_name: str, document: dict[str, Any]) -> list[dict[str, Any]]:
    """Inventory independently observable source constructs for a report."""

    path = Path(path)
    if format_name in {"docx", "xlsx"}:
        return _package_source_features(path, document)
    if format_name == "pdf":
        return _pdf_source_features(path, document)
    if format_name == "markdown":
        return _markdown_source_features(path)
    return []


def _legacy_case_evidence(path: Path, format_name: str, document: dict[str, Any]) -> dict[str, Any]:
    """Build the common evidence payload for one converted input."""

    features = feature_dispositions(document)
    parity = query_parity(document)
    source_features = source_feature_inventory(path, format_name, document)
    return {
        "sourceDigest": source_digest(path),
        "inputDigest": file_digest(path) if Path(path).is_file() else None,
        "sourceFeatureIds": [item["featureId"] for item in source_features],
        "sourceFeatureInventory": source_features,
        "featureInventory": document.get("conversion", {}).get("featureInventory", []),
        "dispositions": features,
        "queryParity": parity,
        "residuals": [item for item in features if item.get("residual") is not None] + [item for item in source_features if item.get("disposition") in {"non-preserved", "omitted-by-policy"}],
    }
