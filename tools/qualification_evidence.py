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
from pathlib import Path
import re
from typing import Any
import zipfile

try:
    from ir_validation import COLLECTION_KEYS
    from query_ir import get_entity, list_entities, rebuild_index
    from adapter_pdf import _decode_stream, _pdf_font_mappings, _pdf_objects, _pdf_references, _pdf_operations
except ImportError:  # pragma: no cover
    from tools.ir_validation import COLLECTION_KEYS
    from tools.query_ir import get_entity, list_entities, rebuild_index
    from tools.adapter_pdf import _decode_stream, _pdf_font_mappings, _pdf_objects, _pdf_references, _pdf_operations


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


def _inventory_map(document: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for item in document.get("conversion", {}).get("featureInventory", []):
        if isinstance(item, dict) and isinstance(item.get("feature"), str) and isinstance(item.get("status"), str):
            result[(item["feature"], item["status"])] = item
    return result


def _target_ids(document: dict[str, Any], feature: str, explicit: str | None) -> list[str]:
    if explicit:
        return [explicit]
    aliases = {
        "document": {"document"},
        "paragraph": {"paragraph", "heading"},
        "heading": {"heading"},
        "table": {"table"},
        "worksheet": {"section"},
        "worksheet-view": {"section"},
        "workbook": {"document"},
        "pages": {"document"},
        "path": {"path"},
        "clipping": {"path"},
        "block-structure": {"document"},
    }
    kinds = aliases.get(feature)
    if not kinds:
        return []
    return [item["nodeId"] for item in document.get("nodes", []) if item.get("kind") in kinds]


def feature_dispositions(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand conversion features into occurrence-level disposition evidence."""

    inventory = _inventory_map(document)
    source_maps = {
        item.get("targetId"): item.get("locator")
        for item in document.get("sourceMaps", [])
        if isinstance(item, dict) and isinstance(item.get("targetId"), str)
    }
    records: list[dict[str, Any]] = []
    source_name = document.get("sourceFormat", {}).get("name", "unknown")
    for ordinal, item in enumerate(document.get("conversion", {}).get("features", [])):
        if not isinstance(item, dict):
            continue
        feature = str(item.get("feature", "unknown"))
        status = str(item.get("status", "failed"))
        summary = inventory.get((feature, status), {})
        disposition = item.get("disposition") or summary.get("disposition")
        if not isinstance(disposition, str):
            if status == "unavailable":
                disposition = "observation"
            elif status in {"approximated", "ambiguous", "unsupported", "omitted-by-policy", "failed"}:
                disposition = "non-preserved"
            elif feature.endswith("-extension") or feature == "extension":
                disposition = "extension"
            else:
                disposition = "core"
        targets = _target_ids(document, feature, item.get("targetId") if isinstance(item.get("targetId"), str) else None)
        diagnostics = [value for value in item.get("diagnosticIds", []) if isinstance(value, str)]
        records.append({
            "featureId": f"source-feature:{source_name}:{ordinal}:{feature}",
            "feature": feature,
            "occurrence": ordinal,
            "status": status,
            "disposition": disposition,
            "targetIds": targets,
            "diagnosticIds": diagnostics,
            "sourceLocators": [source_maps[target] for target in targets if target in source_maps],
            "residual": None if disposition in {"core", "extension", "observation"} else "explicit-status-or-diagnostic",
        })
    return records


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


def _package_source_features(path: Path, document: dict[str, Any]) -> list[dict[str, Any]]:
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


def _pdf_source_features(path: Path, document: dict[str, Any]) -> list[dict[str, Any]]:
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


def _markdown_source_features(path: Path) -> list[dict[str, Any]]:
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


def source_feature_inventory(path: Path, format_name: str, document: dict[str, Any]) -> list[dict[str, Any]]:
    """Inventory independently observable source constructs for a report."""

    path = Path(path)
    if format_name in {"docx", "xlsx"}:
        return _package_source_features(path, document)
    if format_name == "pdf":
        return _pdf_source_features(path, document)
    if format_name == "markdown":
        return _markdown_source_features(path)
    return []


def case_evidence(path: Path, format_name: str, document: dict[str, Any]) -> dict[str, Any]:
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
