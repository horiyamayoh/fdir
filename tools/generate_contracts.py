
#!/usr/bin/env python3
"""Generate FDIR 2.1 core contracts from machine/logical-model.yaml."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

GENERATED = (
    "schemas/fdir.schema.json",
    "schemas/fdir.cddl",
    "schemas/fdir.sql",
    "schemas/context.jsonld",
    "spec/generated/logical-model.md",
)


def load_model(root: Path) -> dict[str, Any]:
    return json.loads((root / "machine/logical-model.yaml").read_text(encoding="utf-8"))


def json_schema(model: dict[str, Any]) -> str:
    defs: dict[str, Any] = {}
    enums = model["enums"]
    for entity in model["entities"]:
        props: dict[str, Any] = {}
        for name, descriptor in entity["properties"].items():
            kind = descriptor["type"]
            schema: dict[str, Any]
            if kind in {"id", "string", "digest", "ref"}:
                schema = {"type": "string", "minLength": 1}
                if kind == "digest":
                    schema["pattern"] = "^[a-z0-9-]+:[0-9a-f]+$"
            elif kind == "integer":
                schema = {"type": "integer"}
            elif kind == "number":
                schema = {"type": "number"}
            elif kind == "object":
                schema = {"type": "object"}
            elif kind == "any":
                schema = {}
            elif kind == "const":
                schema = {"const": descriptor["value"]}
            elif kind == "enum":
                schema = {"enum": enums[descriptor["enum"]]}
            elif kind in {"id-array", "ref-array"}:
                schema = {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": True}
            elif kind == "number-array":
                schema = {"type": "array", "items": {"type": "number"}}
            elif kind == "entity-array":
                schema = {"type": "array", "items": {"$ref": f"#/$defs/{descriptor['target']}"}}
            else:
                raise ValueError(f"unsupported logical type: {kind}")
            for key in ("minimum", "maximum", "minItems", "maxItems"):
                if key in descriptor:
                    schema[key] = descriptor[key]
            props[name] = schema
        defs[entity["name"]] = {
            "type": "object",
            "description": entity["description"],
            "required": entity.get("required", []),
            "properties": props,
            "additionalProperties": False,
        }
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://fdir.dev/schema/2.1/fdir.schema.json",
        "title": "FDIR 2.1 Snapshot",
        "$ref": f"#/$defs/{model['rootEntity']}",
        "$defs": defs,
    }
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def cddl(model: dict[str, Any]) -> str:
    lines = ["; Generated from machine/logical-model.yaml. Do not edit.", "fdir-snapshot = snapshot", ""]
    for enum_name, values in sorted(model["enums"].items()):
        lines.append(f"{enum_name.lower()} = " + " / ".join(json.dumps(v) for v in values))
    lines.append("")
    for entity in model["entities"]:
        required = set(entity.get("required", []))
        lines.append(f"{entity['name'].lower()} = {{")
        for prop_name, desc in entity["properties"].items():
            prefix = "" if prop_name in required else "? "
            kind = desc["type"]
            if kind in {"id", "string", "digest", "ref"}:
                type_name = "tstr"
            elif kind == "integer":
                type_name = "int"
            elif kind == "number":
                type_name = "float / int"
            elif kind == "object":
                type_name = "{ * tstr => any }"
            elif kind == "any":
                type_name = "any"
            elif kind == "const":
                type_name = json.dumps(desc["value"])
            elif kind == "enum":
                type_name = desc["enum"].lower()
            elif kind in {"id-array", "ref-array"}:
                type_name = "[ * tstr ]"
            elif kind == "number-array":
                type_name = "[ 4*4 (float / int) ]"
            elif kind == "entity-array":
                type_name = f"[ * {desc['target'].lower()} ]"
            else:
                raise ValueError(kind)
            lines.append(f"  {prefix}{prop_name}: {type_name},")
        lines.append("}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def sql(model: dict[str, Any]) -> str:
    entities = {item["name"]: item for item in model["entities"]}
    tables = {
        "artifacts": "Artifact", "carriers": "Carrier", "occurrences": "Occurrence",
        "units": "InformationUnit", "assertions": "RecordAssertion", "relations": "InformationRelation",
        "inventory_domains": "InventoryDomain", "accounting_items": "AccountingItem",
        "guarantee_statuses": "GuaranteeStatus", "diagnostics": "Diagnostic",
    }
    lines = [
        "-- Generated from machine/logical-model.yaml. Rebuildable projection only.",
        "PRAGMA foreign_keys = ON;",
        "CREATE TABLE snapshot_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);",
    ]
    for table, entity_name in tables.items():
        entity = entities[entity_name]
        identity = entity["identity"]
        lines.append(f"CREATE TABLE {table} ({identity} TEXT PRIMARY KEY, json TEXT NOT NULL CHECK(json_valid(json)));" )
    lines.extend([
        "CREATE INDEX assertions_by_unit ON assertions(json_extract(json, '$.unitId'));",
        "CREATE INDEX occurrences_by_carrier ON occurrences(json_extract(json, '$.carrierId'));",
        "CREATE INDEX accounting_by_domain ON accounting_items(json_extract(json, '$.inventoryDomainId'));",
        "CREATE INDEX diagnostics_by_code ON diagnostics(json_extract(json, '$.code'));",
    ])
    return "\n".join(lines) + "\n"


def context(model: dict[str, Any]) -> str:
    terms: dict[str, Any] = {"@version": 1.1, "fdir": "https://fdir.dev/vocab/2.1#", "id": "@id", "type": "@type"}
    for entity in model["entities"]:
        terms[entity["name"]] = f"fdir:{entity['name']}"
        for prop in entity["properties"]:
            terms.setdefault(prop, f"fdir:{prop}")
    return json.dumps({"@context": terms}, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def human_reference(model: dict[str, Any]) -> str:
    lines = [
        "# Generated logical-model reference",
        "",
        "> Generated from `machine/logical-model.yaml`; do not edit manually.",
        "",
        f"Model: `{model['modelId']}`",
        f"Version: `{model['version']}`",
        f"Root entity: `{model['rootEntity']}`",
        "",
        "## Enumerations",
        "",
    ]
    for name, values in sorted(model["enums"].items()):
        lines.append(f"### {name}")
        lines.append("")
        lines.append(", ".join(f"`{value}`" for value in values))
        lines.append("")
    lines.extend(["## Entities", ""])
    for entity in model["entities"]:
        lines.append(f"### {entity['name']}")
        lines.append("")
        lines.append(entity["description"])
        lines.append("")
        lines.append("| Property | Type | Required |")
        lines.append("|---|---|---|")
        required = set(entity.get("required", []))
        for prop, desc in entity["properties"].items():
            logical_type = desc["type"]
            if "target" in desc:
                logical_type += f" → {desc['target']}"
            if "enum" in desc:
                logical_type += f" ({desc['enum']})"
            lines.append(f"| `{prop}` | `{logical_type}` | {'yes' if prop in required else 'no'} |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def outputs(root: Path) -> dict[str, str]:
    model = load_model(root)
    result = {
        "schemas/fdir.schema.json": json_schema(model),
        "schemas/fdir.cddl": cddl(model),
        "schemas/fdir.sql": sql(model),
        "schemas/context.jsonld": context(model),
        "spec/generated/logical-model.md": human_reference(model),
    }
    manifest = {
        "generator": "tools/generate_contracts.py",
        "logicalModel": "machine/logical-model.yaml",
        "version": model["version"],
        "files": {path: hashlib.sha256(content.encode("utf-8")).hexdigest() for path, content in sorted(result.items())},
    }
    result["schemas/generated-manifest.json"] = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    return result


def run(root: Path, check: bool) -> int:
    failures: list[str] = []
    for relative, content in outputs(root).items():
        path = root / relative
        if check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                failures.append(relative)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    if failures:
        print("generated contract mismatch:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("generated contracts: ok" if check else "generated contracts: written")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args()
    return run(Path(args.root).resolve(), args.check)


if __name__ == "__main__":
    raise SystemExit(main())
