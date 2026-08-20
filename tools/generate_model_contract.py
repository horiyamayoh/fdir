"""Generate the machine-readable FDIR model contract from current sources.

The contract is deliberately derived from the checked-in JSON Schema,
reference registry, and runtime validator.  No third-party package is needed;
the output is deterministic so a caller can use ``--check`` as a generated
artifact drift gate.
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


GENERATOR_VERSION = "1.0.0"
CONTRACT_SCHEMA_ID = "https://github.com/horiyamayoh/fdir/machine/model-contract/1.0.0"
CONTRACT_SCHEMA_NAME = "fdir/document-form-model-contract"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA_PATH = PROJECT_ROOT / "schemas" / "document-form-ir.schema.json"
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "machine" / "reference-registry.json"
DEFAULT_RUNTIME_PATH = PROJECT_ROOT / "tools" / "ir_validation.py"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "machine" / "model-contract.json"


class ContractGenerationError(ValueError):
    """Raised when the source model cannot produce a coherent contract."""


# These are the graph rules implemented by the current runtime validator.
# They are recorded here rather than modifying that validator.  Its source
# digest is included in the contract, so a runtime edit forces regeneration.
CYCLE_RULES: Dict[str, Dict[str, Any]] = {
    "node-containment": {
        "fields": ["node.parentId", "node.childIds"],
        "scope": "nodes",
        "policy": "forbidden",
        "diagnosticCodes": [
            "DFIR-GRAPH-CYCLE",
            "DFIR-GRAPH-MULTIPLE-PARENT",
            "DFIR-GRAPH-ORPHAN",
            "DFIR-GRAPH-UNREACHABLE",
        ],
    },
    "part-parent": {
        "fields": ["part.parentPartId"],
        "scope": "parts",
        "policy": "forbidden",
        "diagnosticCodes": ["DFIR-PART-CYCLE"],
    },
    "style-based-on": {
        "fields": ["style.basedOn"],
        "scope": "styles",
        "policy": "forbidden",
        "diagnosticCodes": ["DFIR-STYLE-CYCLE"],
    },
    "coordinate-space-parent": {
        "fields": ["coordinateSpace.parentSpaceId"],
        "scope": "coordinateSpaces",
        "policy": "forbidden",
        "diagnosticCodes": ["DFIR-SPACE-CYCLE"],
    },
}


# These are the reciprocal checks visible in the current runtime validator.
# The explicit registry reciprocal is also retained on the generated entry.
RECIPROCITY_RULES: Dict[str, Dict[str, Any]] = {
    "node.parentId": {
        "ruleId": "node-parent-child",
        "fields": ["node.childIds"],
        "policy": "required",
        "source": "runtime-validator",
    },
    "node.childIds": {
        "ruleId": "node-parent-child",
        "fields": ["node.parentId"],
        "policy": "required",
        "source": "reference-registry-and-runtime-validator",
    },
    "part.relationshipIds": {
        "ruleId": "part-relation-from",
        "fields": ["relation.fromId"],
        "policy": "required-when-relation-from-is-part",
        "source": "runtime-validator",
    },
    "relation.fromId": {
        "ruleId": "part-relation-from",
        "fields": ["part.relationshipIds"],
        "policy": "required-when-relation-from-is-part",
        "source": "runtime-validator",
    },
    "table.rowIds": {
        "ruleId": "table-row-containment",
        "fields": ["node.parentId", "node.childIds"],
        "policy": "required-through-node-containment",
        "source": "runtime-validator",
    },
    "table.columnIds": {
        "ruleId": "table-column-containment",
        "fields": ["node.parentId", "node.childIds"],
        "policy": "required-through-node-containment",
        "source": "runtime-validator",
    },
    "table.cellIds": {
        "ruleId": "table-cell-containment",
        "fields": ["node.parentId", "node.childIds"],
        "policy": "required-through-node-containment",
        "source": "runtime-validator",
    },
}


UNIQUE_REFERENCE_FIELDS = {
    "node.childIds",
    "node.textIds",
    "node.styleIds",
    "node.layoutIds",
    "node.annotationIds",
    "node.resourceIds",
    "table.rowIds",
    "table.columnIds",
    "table.cellIds",
    "order.items.id",
}


def _canonical_bytes(value: Any) -> bytes:
    """Return the stable JSON bytes used for artifact and manifest digests."""

    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False)
        + "\n"
    ).encode("utf-8")


def _manifest_bytes(value: Any) -> bytes:
    """Return stable bytes for a digest whose object key order is immaterial."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise ContractGenerationError(f"cannot read source {path}: {exc}") from exc


def _read_json(path: Path, label: str) -> Dict[str, Any]:
    try:
        # utf-8-sig accepts both ordinary UTF-8 and a UTF-8 BOM while the
        # digest still remains the digest of the original bytes.
        value = json.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractGenerationError(f"cannot load {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractGenerationError(f"{label} root must be an object: {path}")
    return value


def _resolve_path(path: Path, root: Path) -> Path:
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _ref_name(schema_node: Any) -> Optional[str]:
    if not isinstance(schema_node, dict):
        return None
    reference = schema_node.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/$defs/"):
        return reference[len("#/$defs/") :]
    return None


def _defs(schema: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    value = schema.get("$defs")
    if not isinstance(value, dict) or not all(isinstance(item, dict) for item in value.values()):
        raise ContractGenerationError("schema.$defs must be an object of definition objects")
    return value  # type: ignore[return-value]


def _resolve_local(schema_node: Any, definitions: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    current = schema_node
    visited: Set[str] = set()
    while True:
        name = _ref_name(current)
        if name is None:
            if not isinstance(current, dict):
                raise ContractGenerationError("schema reference resolved to a non-object")
            return current
        if name in visited:
            raise ContractGenerationError(f"cyclic schema reference at #/$defs/{name}")
        visited.add(name)
        if name not in definitions:
            raise ContractGenerationError(f"unresolved schema reference #/$defs/{name}")
        current = definitions[name]


def _kind_spec(
    definition: Dict[str, Any], definition_name: Optional[str] = None
) -> Dict[str, Any]:
    properties = definition.get("properties")
    kind = properties.get("kind") if isinstance(properties, dict) else None
    if not isinstance(kind, dict):
        if definition_name is not None:
            return {
                "field": None,
                "values": [definition_name],
                "closed": True,
                "policy": "definition-name",
                "type": None,
            }
        return {"field": None, "values": None, "closed": False, "policy": "not-defined"}
    values = kind.get("enum")
    if not isinstance(values, list):
        values = None
    return {
        "field": "kind",
        "values": copy.deepcopy(values),
        "closed": values is not None,
        "policy": "schema-enum" if values is not None else "schema-open-string",
        "type": copy.deepcopy(kind.get("type")),
    }


def _collection_id_field(definition_name: str, definition: Dict[str, Any]) -> Optional[str]:
    properties = definition.get("properties")
    if not isinstance(properties, dict):
        return None
    expected = definition_name + "Id"
    if expected in properties:
        return expected
    candidates = [name for name in properties if name.endswith("Id")]
    return candidates[0] if len(candidates) == 1 else None


def _collection_metadata(
    schema: Dict[str, Any], definitions: Dict[str, Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[str, str]]:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ContractGenerationError("schema.properties must be an object")
    collections: List[Dict[str, Any]] = []
    by_collection: Dict[str, Dict[str, Any]] = {}
    by_definition: Dict[str, str] = {}
    for collection, property_schema in properties.items():
        if not isinstance(property_schema, dict):
            continue
        items = property_schema.get("items")
        definition_name = _ref_name(items)
        if definition_name is None or definition_name not in definitions:
            continue
        definition = definitions[definition_name]
        id_field = _collection_id_field(definition_name, definition)
        entry = {
            "name": collection,
            "definition": definition_name,
            "idField": id_field,
            "kind": _kind_spec(definition, definition_name),
            "statusField": "status" if "status" in definition.get("properties", {}) else None,
            "array": True,
        }
        collections.append(entry)
        by_collection[collection] = entry
        by_definition.setdefault(definition_name, collection)
    return collections, by_collection, by_definition


def _schema_summary(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {"type": "invalid"}
    summary: Dict[str, Any] = {}
    reference = _ref_name(value)
    if reference is not None:
        summary["ref"] = f"#/$defs/{reference}"
    if "type" in value:
        summary["type"] = copy.deepcopy(value["type"])
    if "const" in value:
        summary["const"] = copy.deepcopy(value["const"])
    if isinstance(value.get("enum"), list):
        summary["enum"] = copy.deepcopy(value["enum"])
    if isinstance(value.get("properties"), dict):
        summary["properties"] = list(value["properties"].keys())
    if "required" in value and isinstance(value["required"], list):
        summary["required"] = copy.deepcopy(value["required"])
    if "items" in value:
        summary["items"] = _schema_summary(value["items"])
    if "additionalProperties" in value:
        additional = value["additionalProperties"]
        summary["additionalProperties"] = (
            additional if isinstance(additional, bool) else _schema_summary(additional)
        )
    for keyword in ("oneOf", "anyOf", "allOf"):
        branches = value.get(keyword)
        if isinstance(branches, list):
            summary[keyword] = len(branches)
    for keyword in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"):
        if keyword in value:
            summary[keyword] = copy.deepcopy(value[keyword])
    return summary


def _condition_summary(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    properties = value.get("properties")
    if not isinstance(properties, dict):
        return {}
    result: Dict[str, Any] = {}
    for name, rule in properties.items():
        if not isinstance(rule, dict):
            continue
        if "const" in rule:
            result[name] = {"const": copy.deepcopy(rule["const"])}
        elif isinstance(rule.get("enum"), list):
            result[name] = {"enum": copy.deepcopy(rule["enum"])}
    return result


def _required_fields(value: Any) -> List[str]:
    if not isinstance(value, dict) or not isinstance(value.get("required"), list):
        return []
    return [item for item in value["required"] if isinstance(item, str)]


def _variant_inventory(definition: Dict[str, Any]) -> List[Dict[str, Any]]:
    variants: List[Dict[str, Any]] = []
    for keyword in ("oneOf", "anyOf", "allOf"):
        branches = definition.get(keyword)
        if not isinstance(branches, list):
            continue
        for index, branch in enumerate(branches):
            if not isinstance(branch, dict):
                continue
            condition = branch.get("if", branch)
            discriminator = _condition_summary(condition)
            then = branch.get("then") if isinstance(branch.get("then"), dict) else branch
            forbidden: List[str] = []
            not_rule = then.get("not") if isinstance(then, dict) else None
            if isinstance(not_rule, dict):
                for any_rule in not_rule.get("anyOf", []):
                    forbidden.extend(_required_fields(any_rule))
                forbidden.extend(_required_fields(not_rule))
            if discriminator or _required_fields(then) or forbidden:
                variants.append(
                    {
                        "keyword": keyword,
                        "index": index,
                        "when": discriminator,
                        "required": _required_fields(then),
                        "forbidden": sorted(set(forbidden)),
                    }
                )
    return variants


def _definition_inventory(
    definitions: Dict[str, Dict[str, Any]],
    by_definition: Dict[str, str],
) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for name, definition in definitions.items():
        properties = definition.get("properties")
        if not isinstance(properties, dict):
            properties = {}
        required = set(_required_fields(definition))
        fields: List[Dict[str, Any]] = []
        for field_name, field_schema in properties.items():
            fields.append(
                {
                    "name": field_name,
                    "required": field_name in required,
                    "schema": _schema_summary(field_schema),
                }
            )
        result.append(
            {
                "name": name,
                "collection": by_definition.get(name),
                "idField": _collection_id_field(name, definition),
                "kind": _kind_spec(definition, name if name in by_definition else None),
                "required": _required_fields(definition),
                "additionalProperties": definition.get("additionalProperties", True),
                "fields": fields,
                "variants": _variant_inventory(definition),
            }
        )
    return result


def _resolve_field_schema(
    definitions: Dict[str, Dict[str, Any]], owner_definition: str, field_path: str
) -> Tuple[Dict[str, Any], str]:
    if owner_definition not in definitions:
        raise ContractGenerationError(f"unknown owner definition {owner_definition}")
    current: Any = definitions[owner_definition]
    schema_path = f"#/$defs/{owner_definition}"
    for segment in field_path.split("."):
        current = _resolve_local(current, definitions)
        # Registry paths address fields inside array items as
        # order.items.id, without spelling an extra [] traversal.
        if (
            current.get("type") == "array"
            and isinstance(current.get("items"), dict)
        ):
            current = current["items"]
            schema_path += ".items"
            current = _resolve_local(current, definitions)
        properties = current.get("properties")
        if isinstance(properties, dict) and segment in properties:
            current = properties[segment]
            schema_path += f".properties.{segment}"
            continue
        # This branch is reserved for a future registry path that explicitly
        # names a schema items step.  The current registry uses order.items.id
        # where "items" is an actual property, handled above.
        if segment == "items" and isinstance(current.get("items"), dict):
            current = current["items"]
            schema_path += ".items"
            continue
        if not isinstance(properties, dict) or segment not in properties:
            raise ContractGenerationError(
                f"registry owner field does not exist: {owner_definition}.{field_path}"
            )
    return _resolve_local(current, definitions), schema_path


def _field_shape(field_schema: Dict[str, Any]) -> str:
    field_type = field_schema.get("type")
    if field_type == "array":
        return "array"
    if isinstance(field_type, list) and "array" in field_type:
        return "array"
    return "scalar"


def _parse_cardinality(value: Any) -> Dict[str, Any]:
    if not isinstance(value, str):
        raise ContractGenerationError(f"reference cardinality must be a string: {value!r}")
    if value == "1":
        minimum, maximum = 1, 1
    elif re.fullmatch(r"[0-9]+\.\.(?:[0-9]+|n)", value):
        minimum_text, maximum_text = value.split("..")
        minimum = int(minimum_text)
        maximum = None if maximum_text == "n" else int(maximum_text)
    else:
        raise ContractGenerationError(f"unsupported reference cardinality: {value!r}")
    if maximum is not None and maximum < minimum:
        raise ContractGenerationError(f"invalid reference cardinality: {value!r}")
    return {
        "notation": value,
        "min": minimum,
        "max": maximum,
        "optional": minimum == 0,
        "nullable": False,
    }


def _target_kind_domain(
    target_collection: str,
    explicit_kinds: Optional[List[Any]],
    by_collection: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    metadata = by_collection.get(target_collection)
    if metadata is None:
        raise ContractGenerationError(f"registry target collection does not exist: {target_collection}")
    if explicit_kinds is not None:
        return {
            "collection": target_collection,
            "field": "kind",
            "values": copy.deepcopy(explicit_kinds),
            "closed": True,
            "policy": "registry-targetKinds",
        }
    kind = metadata.get("kind")
    values = kind.get("values") if isinstance(kind, dict) else None
    return {
        "collection": target_collection,
        "field": kind.get("field") if isinstance(kind, dict) else None,
        "values": copy.deepcopy(values),
        "closed": values is not None,
        "policy": "schema-kind-domain" if values is not None else "collection-only",
    }


def _cycle_for(owner_path: str) -> Dict[str, Any]:
    for rule_id, rule in CYCLE_RULES.items():
        if owner_path in rule["fields"]:
            return {"ruleId": rule_id, "source": "runtime-validator", **copy.deepcopy(rule)}
    return {
        "ruleId": None,
        "source": "reference-registry",
        "policy": "allowed",
        "scope": "reference-field",
        "diagnosticCodes": [],
    }


def _reciprocity_for(owner_path: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(RECIPROCITY_RULES.get(owner_path))
    if result is None:
        result = {
            "ruleId": None,
            "fields": [],
            "policy": "not-declared",
            "source": "reference-registry",
        }
    if isinstance(raw.get("reciprocal"), str):
        result["registryField"] = raw["reciprocal"]
        if not result["fields"]:
            result["fields"] = [raw["reciprocal"]]
        if result["policy"] == "not-declared":
            result["policy"] = "required"
    return result


def _status_field(definition: Dict[str, Any]) -> Optional[str]:
    properties = definition.get("properties")
    if isinstance(properties, dict) and "status" in properties:
        return "status"
    return None


def _status_for(owner_definition: str, definition: Dict[str, Any], status_values: List[Any]) -> Dict[str, Any]:
    field = _status_field(definition)
    return {
        "field": f"{owner_definition}.{field}" if field else None,
        "allowed": copy.deepcopy(status_values) if field else [],
        "required": "status" in _required_fields(definition),
        "policy": "entity-status" if field else "not-applicable",
    }


def _registry_references(
    registry: Dict[str, Any],
    definitions: Dict[str, Dict[str, Any]],
    by_collection: Dict[str, Dict[str, Any]],
    by_definition: Dict[str, str],
    status_values: List[Any],
) -> List[Dict[str, Any]]:
    raw_references = registry.get("references")
    if not isinstance(raw_references, list):
        raise ContractGenerationError("reference registry must contain a references array")
    result: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for raw in raw_references:
        if not isinstance(raw, dict):
            raise ContractGenerationError("reference registry entries must be objects")
        owner_path = raw.get("owner")
        if not isinstance(owner_path, str) or "." not in owner_path:
            raise ContractGenerationError(f"invalid registry owner path: {owner_path!r}")
        if owner_path in seen:
            raise ContractGenerationError(f"duplicate registry owner path: {owner_path}")
        seen.add(owner_path)
        owner_definition, field_path = owner_path.split(".", 1)
        field_schema, schema_path = _resolve_field_schema(definitions, owner_definition, field_path)
        target_collections = raw.get("targetCollections")
        if not isinstance(target_collections, list) or not all(isinstance(item, str) for item in target_collections):
            raise ContractGenerationError(f"invalid targetCollections for {owner_path}")
        explicit_kinds = raw.get("targetKinds")
        if explicit_kinds is not None and (
            not isinstance(explicit_kinds, list) or not all(isinstance(item, str) for item in explicit_kinds)
        ):
            raise ContractGenerationError(f"invalid targetKinds for {owner_path}")
        target_kind_domains = [
            _target_kind_domain(collection, explicit_kinds, by_collection)
            for collection in target_collections
        ]
        owner_definition_schema = definitions[owner_definition]
        cardinality = _parse_cardinality(raw.get("cardinality"))
        # A nested registry path such as order.items.id resolves to the item
        # scalar, but its declared cardinality still describes the outer
        # array.  Preserve that wire shape in the contract.
        shape = "array" if cardinality["max"] is None else _field_shape(field_schema)
        cardinality["shape"] = shape
        cardinality["schemaRequired"] = field_path.split(".")[0] in set(
            _required_fields(owner_definition_schema)
        )
        entry = {
            "id": owner_path,
            "owner": {
                "path": owner_path,
                "definition": owner_definition,
                "collection": by_definition.get(owner_definition),
                "field": field_path,
                "kind": _kind_spec(owner_definition_schema, owner_definition),
            },
            "target": {
                "collections": copy.deepcopy(target_collections),
                "kind": {
                    "policy": "registry-targetKinds" if explicit_kinds is not None else "schema-kind-domain",
                    "byCollection": target_kind_domains,
                },
            },
            # Keep source vocabulary alongside the expanded form.  This makes
            # registry-to-contract comparison straightforward for consumers.
            "targetCollections": copy.deepcopy(target_collections),
            "targetKinds": copy.deepcopy(explicit_kinds),
            "cardinality": raw["cardinality"],
            "cardinalityRule": cardinality,
            "ordered": shape == "array",
            "uniqueness": {
                "policy": "unique" if owner_path in UNIQUE_REFERENCE_FIELDS else "not-declared",
                "enforcedBy": ["runtime-validator"] if owner_path in UNIQUE_REFERENCE_FIELDS else [],
            },
            "reciprocity": _reciprocity_for(owner_path, raw),
            "cycle": _cycle_for(owner_path),
            "status": _status_for(owner_definition, owner_definition_schema, status_values),
            "schema": {
                "path": schema_path,
                "shape": _schema_summary(field_schema),
            },
        }
        result.append(entry)
    return result


def _walk_id_references(
    value: Any,
    definitions: Dict[str, Dict[str, Any]],
    path: str,
    output: Set[str],
    active_refs: Optional[Set[str]] = None,
) -> None:
    if not isinstance(value, dict):
        return
    active_refs = set(active_refs or set())
    reference = _ref_name(value)
    if reference == "id":
        output.add(path)
        return
    if reference is not None:
        if reference in active_refs or reference not in definitions:
            return
        active_refs.add(reference)
        _walk_id_references(definitions[reference], definitions, path, output, active_refs)
        return
    properties = value.get("properties")
    if isinstance(properties, dict):
        for name, child in properties.items():
            _walk_id_references(child, definitions, f"{path}.{name}", output, active_refs)
    items = value.get("items")
    if isinstance(items, dict):
        _walk_id_references(items, definitions, path, output, active_refs)
    for keyword in ("oneOf", "anyOf", "allOf"):
        branches = value.get(keyword)
        if isinstance(branches, list):
            for branch in branches:
                _walk_id_references(branch, definitions, path, output, active_refs)
    for keyword in ("if", "then", "else", "not"):
        branch = value.get(keyword)
        if isinstance(branch, dict):
            _walk_id_references(branch, definitions, path, output, active_refs)


def _schema_reference_inventory(
    definitions: Dict[str, Dict[str, Any]], collections: List[Dict[str, Any]]
) -> List[str]:
    paths: Set[str] = set()
    for name, definition in definitions.items():
        _walk_id_references(definition, definitions, name, paths)
    identity_paths = {
        f"{item['definition']}.{item['idField']}"
        for item in collections
        if item.get("idField")
    }
    return sorted(path for path in paths if path not in identity_paths and path != "ref.id")


def _runtime_literal_assignments(runtime_path: Path) -> Dict[str, Any]:
    try:
        source = runtime_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(runtime_path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise ContractGenerationError(f"cannot inspect runtime validator {runtime_path}: {exc}") from exc
    assignments: Dict[str, Any] = {}
    for node in ast.walk(tree):
        target: Optional[ast.expr] = None
        value_node: Optional[ast.expr] = None
        if isinstance(node, ast.Assign) and node.targets:
            target = node.targets[0]
            value_node = node.value
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            value_node = node.value
        if isinstance(target, ast.Name) and target.id in {
            "COLLECTION_KEYS",
            "STATUS_VALUES",
            "PARTIAL_STATUSES",
            "NODE_KINDS",
        } and value_node is not None:
            try:
                assignments[target.id] = ast.literal_eval(value_node)
            except (ValueError, TypeError):
                continue
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
            if not isinstance(node.ops[0], ast.NotIn):
                continue
            left = node.left
            if not isinstance(left, ast.Call) or not isinstance(left.func, ast.Attribute):
                continue
            if left.func.attr != "get" or not left.args:
                continue
            if not isinstance(left.func.value, ast.Name) or left.func.value.id != "conversion":
                continue
            try:
                requested_key = ast.literal_eval(left.args[0])
                candidate = ast.literal_eval(node.comparators[0])
            except (ValueError, TypeError):
                continue
            if requested_key == "status" and isinstance(candidate, (set, list, tuple)):
                assignments["CONVERSION_STATUS_VALUES"] = candidate
    return assignments


def _runtime_contract(
    runtime_path: Path,
    schema_collections: List[Dict[str, Any]],
    schema_status_values: List[Any],
    schema_node_kinds: Optional[List[Any]],
) -> Dict[str, Any]:
    assignments = _runtime_literal_assignments(runtime_path)
    runtime_collections = assignments.get("COLLECTION_KEYS")
    runtime_status_values = assignments.get("STATUS_VALUES")
    runtime_partial_values = assignments.get("PARTIAL_STATUSES")
    runtime_node_kinds = assignments.get("NODE_KINDS")
    runtime_conversion_values = assignments.get("CONVERSION_STATUS_VALUES")
    if not isinstance(runtime_collections, dict):
        raise ContractGenerationError("runtime validator COLLECTION_KEYS is not a literal mapping")
    if not isinstance(runtime_status_values, (set, list, tuple)):
        raise ContractGenerationError("runtime validator STATUS_VALUES is not a literal collection")
    if not isinstance(runtime_partial_values, (set, list, tuple)):
        raise ContractGenerationError("runtime validator PARTIAL_STATUSES is not a literal collection")
    if not isinstance(runtime_conversion_values, (set, list, tuple)):
        raise ContractGenerationError(
            "runtime validator conversion status set is not a literal collection"
        )
    schema_collection_names = {item["name"] for item in schema_collections}
    if set(runtime_collections) != schema_collection_names:
        raise ContractGenerationError(
            "schema/runtime collection drift: "
            f"schema={sorted(schema_collection_names)} runtime={sorted(runtime_collections)}"
        )
    if set(runtime_status_values) != set(schema_status_values):
        raise ContractGenerationError(
            "schema/runtime status drift: "
            f"schema={sorted(schema_status_values)} runtime={sorted(runtime_status_values)}"
        )
    if schema_node_kinds is not None and set(runtime_node_kinds or ()) != set(schema_node_kinds):
        raise ContractGenerationError(
            "schema/runtime node kind drift: "
            f"schema={sorted(schema_node_kinds)} runtime={sorted(runtime_node_kinds or ())}"
        )
    return {
        "collectionIdFields": {name: runtime_collections[name] for name in sorted(runtime_collections)},
        "statusValues": sorted(runtime_status_values),
        "partialStatuses": sorted(runtime_partial_values),
        "nodeKinds": sorted(runtime_node_kinds or ()),
        "conversionStatuses": sorted(runtime_conversion_values),
    }


def _status_contract(
    schema: Dict[str, Any],
    definitions: Dict[str, Dict[str, Any]],
    runtime: Dict[str, Any],
) -> Dict[str, Any]:
    status_definition = definitions.get("status", {})
    status_values = status_definition.get("enum")
    if not isinstance(status_values, list):
        raise ContractGenerationError("schema $defs.status.enum must be a list")
    conversion = definitions.get("conversionReport", {})
    conversion_status = conversion.get("properties", {}).get("status", {})
    conversion_values = conversion_status.get("enum")
    if not isinstance(conversion_values, list):
        raise ContractGenerationError("schema conversion.status enum must be a list")
    if set(conversion_values) != set(runtime["conversionStatuses"]):
        raise ContractGenerationError(
            "schema/runtime conversion status drift: "
            f"schema={sorted(conversion_values)} runtime={sorted(runtime['conversionStatuses'])}"
        )
    diagnostic = definitions.get("diagnostic", {})
    severity = diagnostic.get("properties", {}).get("severity", {})
    severity_values = severity.get("enum")
    resource = definitions.get("resource", {})
    availability = resource.get("properties", {}).get("availability", {})
    availability_values = availability.get("enum")
    warning_evidence: Dict[str, Any]
    if "complete-with-warnings" in runtime["conversionStatuses"]:
        warning_evidence = {
            "policy": "supported",
            "warningStatus": "complete-with-warnings",
            "warningField": "conversion.warnings",
            "warningDiagnosticSeverities": ["info", "warning"],
            "completeStatusRequires": "conversion.warnings absent-or-empty",
        }
    else:
        warning_evidence = {"policy": "not-supported"}
    return {
        "entity": {
            "field": "*.status",
            "values": copy.deepcopy(status_values),
            "runtimeValues": copy.deepcopy(runtime["statusValues"]),
            "partialValues": copy.deepcopy(runtime["partialStatuses"]),
            "completeClaim": {
                "forbiddenEntityStatuses": copy.deepcopy(runtime["partialStatuses"]),
                "forbiddenDiagnosticSeverities": ["error", "fatal"],
            },
        },
        "feature": {
            "field": "conversion.features[].status",
            "values": copy.deepcopy(status_values),
        },
        "conversion": {
            "field": "conversion.status",
            "values": copy.deepcopy(conversion_values),
            "runtimeValues": copy.deepcopy(runtime["conversionStatuses"]),
            "completeWithWarnings": (
                "supported"
                if "complete-with-warnings" in runtime["conversionStatuses"]
                else "not-supported"
            ),
            "warningEvidence": warning_evidence,
            "partialRequires": "diagnostic-or-partial-entity-status",
            "failedRequires": "diagnostic",
        },
        "diagnosticSeverity": {
            "field": "diagnostics[].severity",
            "values": copy.deepcopy(severity_values),
        },
        "resourceAvailability": {
            "field": "resources[].availability",
            "values": copy.deepcopy(availability_values),
        },
        "statusFields": sorted(_status_paths(definitions)),
    }


def _status_paths(definitions: Dict[str, Dict[str, Any]]) -> Set[str]:
    paths: Set[str] = set()
    for name, definition in definitions.items():
        _walk_ref_paths(definition, name, "status", paths)
    return paths


def _walk_ref_paths(value: Any, path: str, target_ref: str, output: Set[str]) -> None:
    if not isinstance(value, dict):
        return
    reference = _ref_name(value)
    if reference == target_ref:
        output.add(path)
        return
    if reference is not None:
        # The caller only needs paths rooted at each definition.  Definitions
        # are expanded by the top-level loop in _status_paths below.
        return
    properties = value.get("properties")
    if isinstance(properties, dict):
        for name, child in properties.items():
            _walk_ref_paths(child, f"{path}.{name}", target_ref, output)
    for keyword in ("oneOf", "anyOf", "allOf"):
        branches = value.get(keyword)
        if isinstance(branches, list):
            for branch in branches:
                _walk_ref_paths(branch, path, target_ref, output)


def _relation_contract(
    definitions: Dict[str, Dict[str, Any]], references: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    relation = definitions.get("relation", {})
    kind_schema = relation.get("properties", {}).get("kind", {})
    kinds = kind_schema.get("enum")
    if not isinstance(kinds, list):
        return []
    endpoints = {item["id"]: item for item in references if item["id"] in {"relation.fromId", "relation.toId"}}
    if set(endpoints) != {"relation.fromId", "relation.toId"}:
        raise ContractGenerationError("relation endpoint references are missing from the registry")
    result: List[Dict[str, Any]] = []
    for kind in kinds:
        result.append(
            {
                "kind": kind,
                "from": copy.deepcopy(endpoints["relation.fromId"]["target"]),
                "to": copy.deepcopy(endpoints["relation.toId"]["target"]),
                "policy": "collection-wide-current-registry",
                "closed": False,
                "note": "The current registry does not narrow endpoint kinds by relation kind.",
            }
        )
    return result


def _reciprocity_inventory() -> List[Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    for owner_path, rule in RECIPROCITY_RULES.items():
        rule_id = rule.get("ruleId")
        if not isinstance(rule_id, str):
            continue
        entry = grouped.setdefault(
            rule_id,
            {
                "ruleId": rule_id,
                "fields": set(),
                "policies": set(),
                "sources": set(),
            },
        )
        entry["fields"].add(owner_path)
        entry["fields"].update(field for field in rule.get("fields", []) if isinstance(field, str))
        if isinstance(rule.get("policy"), str):
            entry["policies"].add(rule["policy"])
        if isinstance(rule.get("source"), str):
            entry["sources"].add(rule["source"])
    result: List[Dict[str, Any]] = []
    for rule_id in sorted(grouped):
        entry = grouped[rule_id]
        result.append(
            {
                "ruleId": rule_id,
                "fields": sorted(entry["fields"]),
                "policies": sorted(entry["policies"]),
                "sources": sorted(entry["sources"]),
            }
        )
    return result


def _source_records(
    root: Path,
    schema_path: Path,
    registry_path: Path,
    runtime_path: Path,
    generator_path: Path,
) -> Tuple[List[Dict[str, str]], str]:
    records = [
        {"role": "schema", "path": _relative_path(schema_path, root), "sha256": _sha256_file(schema_path)},
        {
            "role": "referenceRegistry",
            "path": _relative_path(registry_path, root),
            "sha256": _sha256_file(registry_path),
        },
        {
            "role": "runtimeValidator",
            "path": _relative_path(runtime_path, root),
            "sha256": _sha256_file(runtime_path),
        },
        {
            "role": "generator",
            "path": _relative_path(generator_path, root),
            "sha256": _sha256_file(generator_path),
        },
    ]
    return records, _sha256_bytes(_manifest_bytes(records))


def build_contract(
    schema_path: Optional[Path] = None,
    registry_path: Optional[Path] = None,
    runtime_path: Optional[Path] = None,
    generator_path: Optional[Path] = None,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Build a deterministic contract from the current repository sources."""

    root = (root or PROJECT_ROOT).resolve()
    schema_path = _resolve_path(schema_path or DEFAULT_SCHEMA_PATH, root)
    registry_path = _resolve_path(registry_path or DEFAULT_REGISTRY_PATH, root)
    runtime_path = _resolve_path(runtime_path or DEFAULT_RUNTIME_PATH, root)
    generator_path = _resolve_path(generator_path or Path(__file__), root)
    schema = _read_json(schema_path, "schema")
    registry = _read_json(registry_path, "reference registry")
    definitions = _defs(schema)
    collections, by_collection, by_definition = _collection_metadata(schema, definitions)
    if not collections:
        raise ContractGenerationError("schema has no array-backed model collections")
    status_definition = definitions.get("status", {})
    status_values = status_definition.get("enum")
    if not isinstance(status_values, list):
        raise ContractGenerationError("schema $defs.status.enum must be a list")
    node_kind_schema = definitions.get("node", {}).get("properties", {}).get("kind", {})
    node_kinds = node_kind_schema.get("enum") if isinstance(node_kind_schema, dict) else None
    runtime = _runtime_contract(runtime_path, collections, status_values, node_kinds)
    references = _registry_references(
        registry, definitions, by_collection, by_definition, status_values
    )
    schema_reference_paths = _schema_reference_inventory(definitions, collections)
    registry_reference_paths = sorted(item["id"] for item in references)
    source_records, source_digest = _source_records(
        root, schema_path, registry_path, runtime_path, generator_path
    )
    contract = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": CONTRACT_SCHEMA_ID,
        "schema": CONTRACT_SCHEMA_NAME,
        "version": "1.0.0",
        "generator": {
            "path": _relative_path(generator_path, root),
            "version": GENERATOR_VERSION,
            "deterministic": True,
        },
        "sourceDigestAlgorithm": "sha256(canonical-json(sources))",
        "sourceDigest": source_digest,
        "sources": source_records,
        "authority": {
            "kind": "derived-model-contract",
            "normativeInputs": [
                "schemas/document-form-ir.schema.json",
                "machine/reference-registry.json",
            ],
            "runtimeParityInput": "tools/ir_validation.py",
            "generatedArtifact": "machine/model-contract.json",
        },
        "collections": collections,
        "definitions": _definition_inventory(definitions, by_definition),
        "references": references,
        "reciprocityRules": _reciprocity_inventory(),
        "cycleRules": [
            {"ruleId": rule_id, "source": "runtime-validator", **copy.deepcopy(rule)}
            for rule_id, rule in CYCLE_RULES.items()
        ],
        "relationKinds": _relation_contract(definitions, references),
        "status": _status_contract(schema, definitions, runtime),
        "runtime": runtime,
        "coverage": {
            "registryReferenceCount": len(references),
            "schemaReferenceFieldCount": len(schema_reference_paths),
            "registryReferencePaths": registry_reference_paths,
            "schemaReferencePathsNotInRegistry": sorted(
                set(schema_reference_paths) - set(registry_reference_paths)
            ),
            "registryPathsNotInSchema": sorted(
                set(registry_reference_paths) - set(schema_reference_paths)
            ),
        },
    }
    return contract


def write_contract(path: Path, contract: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_canonical_bytes(contract))
    except OSError as exc:
        raise ContractGenerationError(f"cannot write contract {path}: {exc}") from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA_PATH)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY_PATH)
    parser.add_argument("--runtime-validator", type=Path, default=DEFAULT_RUNTIME_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail unless the output bytes equal the freshly generated contract",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        output = _resolve_path(args.output, PROJECT_ROOT)
        contract = build_contract(
            schema_path=args.schema,
            registry_path=args.registry,
            runtime_path=args.runtime_validator,
            generator_path=Path(__file__),
            root=PROJECT_ROOT,
        )
        expected = _canonical_bytes(contract)
        if args.check:
            if not output.is_file():
                raise ContractGenerationError(f"generated contract is missing: {output}")
            actual = output.read_bytes()
            if actual != expected:
                raise ContractGenerationError(
                    f"generated contract drift detected: {output}; regenerate it"
                )
            print(f"model contract is current: {output}")
        else:
            write_contract(output, contract)
            print(f"generated model contract: {output}")
        return 0
    except ContractGenerationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
