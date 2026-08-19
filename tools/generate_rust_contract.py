#!/usr/bin/env python3
"""Generate the typed Rust contract projection from the frozen FDIR authorities."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

GENERATED_PATH = Path("crates/fdir-contract/src/generated.rs")
MANIFEST_PATH = Path("quality/rust-generated-contract.json")
MANIFEST_SCHEMA = "fdir/rust-generated-contract/2"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def rust_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def pascal_case(value: str) -> str:
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value).replace("-", "_").split("_")
    if len(words) == 1:
        words = words[0].split()
    return "".join(word[:1].upper() + word[1:] for word in words if word)


def snake_case(value: str) -> str:
    value = value.replace("-", "_")
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return value.lower()


def enum_variant(value: str) -> str:
    return pascal_case(value)


def rust_slice(name: str, values: list[str], indent: str = "    ") -> list[str]:
    lines = [f"{indent}pub const {name}: &[&str] = &["]
    lines.extend(f"{indent}    {rust_string(value)}," for value in values)
    lines.append(f"{indent}];")
    return lines


def validate_model(model: Any) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    if not isinstance(model, dict):
        raise ValueError("logical model must contain an object")
    entities = model.get("entities")
    enums = model.get("enums")
    if not isinstance(entities, list) or not entities:
        raise ValueError("logical model has no entity registry")
    if not isinstance(enums, dict) or not enums:
        raise ValueError("logical model has no enumeration registry")
    entity_names: set[str] = set()
    for index, entity in enumerate(entities):
        if not isinstance(entity, dict) or not isinstance(entity.get("name"), str):
            raise ValueError(f"logical model entity {index} has no name")
        name = entity["name"]
        if name in entity_names:
            raise ValueError(f"duplicate logical model entity: {name}")
        entity_names.add(name)
        if not isinstance(entity.get("identity"), str):
            raise ValueError(f"logical model entity {name} has no identity")
        if not isinstance(entity.get("properties"), dict):
            raise ValueError(f"logical model entity {name} has no properties")
        if not isinstance(entity.get("required"), list):
            raise ValueError(f"logical model entity {name} has no required registry")
    normalized_enums: dict[str, list[str]] = {}
    for name, values in enums.items():
        if not isinstance(name, str) or not isinstance(values, list) or not values:
            raise ValueError(f"invalid enumeration registry: {name!r}")
        if not all(isinstance(value, str) and value for value in values):
            raise ValueError(f"invalid enumeration value registry: {name}")
        normalized_enums[name] = values
    return entities, normalized_enums


def identity_maps(entities: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, str]]:
    by_entity: dict[str, str] = {}
    by_field: dict[str, str] = {}
    for entity in entities:
        field = entity["identity"]
        type_name = pascal_case(field)
        by_entity[entity["name"]] = type_name
        by_field[field] = type_name
    return by_entity, by_field


def inferred_id_type(
    property_name: str,
    entity_name: str,
    entity_identity: str,
    by_field: dict[str, str],
) -> str:
    if property_name == entity_identity:
        return by_field[entity_identity]
    lowered = property_name.lower()
    for field, type_name in sorted(by_field.items(), key=lambda item: len(item[0]), reverse=True):
        if lowered.endswith(field.lower()):
            return type_name
        plural = field[:-2] + "Ids" if field.endswith("Id") else field + "s"
        if lowered.endswith(plural.lower()):
            return type_name
    if "snapshot" in lowered:
        return "SnapshotId"
    if "unit" in lowered:
        return "UnitId"
    return "OpaqueId"


def property_type(
    entity: dict[str, Any],
    property_name: str,
    definition: dict[str, Any],
    required: bool,
    by_entity: dict[str, str],
    by_field: dict[str, str],
) -> str:
    kind = definition.get("type")
    target = definition.get("target")
    if kind == "id":
        base = inferred_id_type(property_name, entity["name"], entity["identity"], by_field)
    elif kind == "id-array":
        base = f"Vec<{inferred_id_type(property_name, entity['name'], entity['identity'], by_field)}>"
    elif kind == "ref":
        if target not in by_entity:
            raise ValueError(f"unknown reference target {target!r} on {entity['name']}.{property_name}")
        base = by_entity[target]
    elif kind == "ref-array":
        if target not in by_entity:
            raise ValueError(f"unknown reference target {target!r} on {entity['name']}.{property_name}")
        base = f"Vec<{by_entity[target]}>"
    elif kind == "entity-array":
        if target not in by_entity:
            raise ValueError(f"unknown entity target {target!r} on {entity['name']}.{property_name}")
        base = f"Vec<{target}>"
    elif kind == "enum":
        enumeration = definition.get("enum")
        if not isinstance(enumeration, str):
            raise ValueError(f"missing enumeration on {entity['name']}.{property_name}")
        base = enumeration
    elif kind in {"string", "const"}:
        base = "String"
    elif kind == "integer":
        base = "u64" if definition.get("minimum") == 0 else "i64"
    elif kind == "number":
        base = "crate::FiniteNumber"
    elif kind == "number-array":
        base = "Vec<crate::FiniteNumber>"
    elif kind == "digest":
        base = "crate::Digest"
    elif kind == "object":
        base = "crate::ObjectValue"
    elif kind == "any":
        base = "crate::CanonicalValue"
    else:
        raise ValueError(f"unsupported property type {kind!r} on {entity['name']}.{property_name}")
    if required or kind == "const":
        return base
    return f"Option<{base}>"


def property_kind_variant(kind: str) -> str:
    return {
        "id": "Id",
        "id-array": "IdArray",
        "ref": "Reference",
        "ref-array": "ReferenceArray",
        "entity-array": "EntityArray",
        "enum": "Enumeration",
        "string": "String",
        "const": "Constant",
        "integer": "Integer",
        "number": "Number",
        "number-array": "NumberArray",
        "digest": "Digest",
        "object": "Object",
        "any": "Any",
    }[kind]


def option_string(value: Any) -> str:
    return "None" if value is None else f"Some({rust_string(str(value))})"


def option_float(value: Any) -> str:
    if value is None:
        return "None"
    return f"Some({float(value)!r})"


def option_usize(value: Any) -> str:
    if value is None:
        return "None"
    return f"Some({int(value)})"


def generated_contract(root: Path) -> tuple[str, dict[str, int]]:
    model_path = root / "machine/logical-model.yaml"
    vector_path = root / "fixtures/canonical/vector.json"
    model = load_json(model_path)
    vector_bytes = vector_path.read_bytes()
    entities, enums = validate_model(model)
    by_entity, by_field = identity_maps(entities)

    model_id = model.get("modelId")
    model_version = model.get("version")
    root_entity = model.get("rootEntity")
    if not all(isinstance(value, str) and value for value in (model_id, model_version, root_entity)):
        raise ValueError("logical model metadata is incomplete")

    id_types = sorted({*by_entity.values(), "OpaqueId"})
    entity_names = [entity["name"] for entity in entities]
    enum_names = sorted(enums)

    lines = [
        "// @generated by tools/generate_rust_contract.py; do not edit.",
        "// The language-neutral machine model and canonical vector remain authoritative.",
        "",
        "#[rustfmt::skip]",
        "mod generated_contract {",
        "    use std::fmt::{self, Display, Formatter};",
        "    use std::str::FromStr;",
        "",
        "    /// Identifier of the frozen language-neutral logical model.",
        f"    pub const MODEL_ID: &str = {rust_string(model_id)};",
        "    /// Version of the frozen language-neutral logical model.",
        f"    pub const MODEL_VERSION: &str = {rust_string(model_version)};",
        "    /// Root entity declared by the logical model.",
        f"    pub const ROOT_ENTITY: &str = {rust_string(root_entity)};",
        "    /// SHA-256 of the canonical-vector source bytes.",
        f'    pub const CANONICAL_VECTOR_SHA256: &str = "{hashlib.sha256(vector_bytes).hexdigest()}";',
        "    /// Byte length of the canonical-vector source.",
        f"    pub const CANONICAL_VECTOR_LENGTH: usize = {len(vector_bytes)};",
        "    /// Canonical vector compiled directly from the repository authority.",
        "    pub const CANONICAL_VECTOR_JSON: &str = include_str!(concat!(",
        '        env!("CARGO_MANIFEST_DIR"),',
        '        "/../../fixtures/canonical/vector.json"',
        "    ));",
        "",
        "    /// Entity names in authority order.",
        *rust_slice("ENTITY_NAMES", entity_names),
        "",
        "    /// Enumeration names in deterministic lexical order.",
        *rust_slice("ENUM_NAMES", enum_names),
        "",
        "    macro_rules! define_strong_id {",
        "        ($name:ident) => {",
        "            #[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]",
        "            pub struct $name(crate::Identifier);",
        "",
        "            impl $name {",
        "                /// Construct a non-empty, entity-specific identifier.",
        "                pub fn new(value: impl Into<String>) -> Result<Self, crate::ValueError> {",
        "                    crate::Identifier::new(value).map(Self)",
        "                }",
        "",
        "                /// Borrow the exact identifier text.",
        "                pub fn as_str(&self) -> &str {",
        "                    self.0.as_str()",
        "                }",
        "",
        "                /// Consume the identifier and return its text.",
        "                pub fn into_string(self) -> String {",
        "                    self.0.into_string()",
        "                }",
        "            }",
        "",
        "            impl AsRef<str> for $name {",
        "                fn as_ref(&self) -> &str {",
        "                    self.as_str()",
        "                }",
        "            }",
        "",
        "            impl Display for $name {",
        "                fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {",
        "                    formatter.write_str(self.as_str())",
        "                }",
        "            }",
        "",
        "            impl FromStr for $name {",
        "                type Err = crate::ValueError;",
        "",
        "                fn from_str(value: &str) -> Result<Self, Self::Err> {",
        "                    Self::new(value)",
        "                }",
        "            }",
        "        };",
        "    }",
        "",
    ]
    for type_name in id_types:
        lines.append(f"    define_strong_id!({type_name});")
    lines.append("")

    for enum_name in enum_names:
        values = enums[enum_name]
        lines.extend(
            [
                "    #[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]",
                f"    pub enum {enum_name} {{",
                *[f"        {enum_variant(value)}," for value in values],
                "    }",
                "",
                f"    impl {enum_name} {{",
                f"        pub const ALL: [Self; {len(values)}] = [",
                *[f"            Self::{enum_variant(value)}," for value in values],
                "        ];",
                "",
                "        /// Stable machine-readable value from the logical model.",
                "        pub const fn as_str(self) -> &'static str {",
                "            match self {",
                *[
                    f"                Self::{enum_variant(value)} => {rust_string(value)},"
                    for value in values
                ],
                "            }",
                "        }",
                "",
                "        /// Parse a value without accepting an unknown variant as success.",
                "        pub fn parse(value: &str) -> Result<Self, crate::UnknownEnumValue> {",
                "            match value {",
                *[
                    f"                {rust_string(value)} => Ok(Self::{enum_variant(value)}),"
                    for value in values
                ],
                f"                _ => Err(crate::UnknownEnumValue::new({rust_string(enum_name)}, value)),",
                "            }",
                "        }",
                "    }",
                "",
                f"    impl Display for {enum_name} {{",
                "        fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {",
                "            formatter.write_str(self.as_str())",
                "        }",
                "    }",
                "",
                f"    impl TryFrom<&str> for {enum_name} {{",
                "        type Error = crate::UnknownEnumValue;",
                "",
                "        fn try_from(value: &str) -> Result<Self, crate::UnknownEnumValue> {",
                "            Self::parse(value)",
                "        }",
                "    }",
                "",
            ]
        )

    for entity in entities:
        name = entity["name"]
        properties: dict[str, dict[str, Any]] = entity["properties"]
        required_names = set(entity["required"])
        ordered_properties = sorted(properties)
        lines.extend(
            [
                "    #[derive(Clone, Debug, PartialEq)]",
                f"    pub struct {name} {{",
            ]
        )
        for property_name in ordered_properties:
            definition = properties[property_name]
            field_type = property_type(
                entity,
                property_name,
                definition,
                property_name in required_names,
                by_entity,
                by_field,
            )
            lines.extend(
                [
                    f"        /// JSON property `{property_name}`.",
                    f"        pub {snake_case(property_name)}: {field_type},",
                ]
            )
        lines.extend(
            [
                "        /// Unknown extension members retained without granting them core authority.",
                "        pub extensions: crate::ExtensionMap,",
                "    }",
                "",
                f"    impl {name} {{",
                "        /// Construct the entity with every machine-required property present.",
                "        #[allow(clippy::too_many_arguments)]",
            ]
        )
        constructor_props = [
            property_name
            for property_name in entity["required"]
            if properties[property_name].get("type") != "const"
        ]
        if constructor_props:
            lines.append("        pub fn new(")
            for property_name in constructor_props:
                definition = properties[property_name]
                field_type = property_type(
                    entity,
                    property_name,
                    definition,
                    True,
                    by_entity,
                    by_field,
                )
                lines.append(f"            {snake_case(property_name)}: {field_type},")
            lines.append("        ) -> Self {")
        else:
            lines.append("        pub fn new() -> Self {")
        lines.append("            Self {")
        for property_name in ordered_properties:
            definition = properties[property_name]
            rust_name = snake_case(property_name)
            kind = definition.get("type")
            if kind == "const":
                constant = definition.get("value")
                if constant == model_version:
                    value = "MODEL_VERSION.to_owned()"
                else:
                    value = f"{rust_string(str(constant))}.to_owned()"
            elif property_name in required_names:
                value = rust_name
            else:
                value = "None"
            if value == rust_name:
                lines.append(f"                {rust_name},")
            else:
                lines.append(f"                {rust_name}: {value},")
        lines.extend(
            [
                "                extensions: crate::ExtensionMap::new(),",
                "            }",
                "        }",
                "    }",
                "",
            ]
        )

    for entity in entities:
        name = entity["name"]
        const_name = f"{snake_case(name).upper()}_PROPERTIES"
        properties: dict[str, dict[str, Any]] = entity["properties"]
        required_names = set(entity["required"])
        lines.append(f"    const {const_name}: &[crate::PropertySpec] = &[")
        for property_name in sorted(properties):
            definition = properties[property_name]
            lines.extend(
                [
                    "        crate::PropertySpec {",
                    f"            json_name: {rust_string(property_name)},",
                    f"            rust_name: {rust_string(snake_case(property_name))},",
                    f"            kind: crate::PropertyKind::{property_kind_variant(definition['type'])},",
                    f"            required: {str(property_name in required_names).lower()},",
                    f"            target: {option_string(definition.get('target'))},",
                    f"            enumeration: {option_string(definition.get('enum'))},",
                    f"            constant: {option_string(definition.get('value'))},",
                    f"            minimum: {option_float(definition.get('minimum'))},",
                    f"            maximum: {option_float(definition.get('maximum'))},",
                    f"            min_items: {option_usize(definition.get('minItems'))},",
                    f"            max_items: {option_usize(definition.get('maxItems'))},",
                    "        },",
                ]
            )
        lines.extend(["    ];", ""])

    lines.append("    /// Machine-derived entity descriptors used by independent validation.")
    lines.append("    pub const ENTITY_SPECS: &[crate::EntitySpec] = &[")
    for entity in entities:
        const_name = f"{snake_case(entity['name']).upper()}_PROPERTIES"
        domain = {
            "evidence": "Evidence",
            "recorded-information": "RecordedInformation",
            "shared": "Shared",
            "root": "Root",
        }[entity["domain"]]
        lines.extend(
            [
                "        crate::EntitySpec {",
                f"            name: {rust_string(entity['name'])},",
                f"            domain: crate::EntityDomain::{domain},",
                f"            identity: {rust_string(entity['identity'])},",
                f"            properties: {const_name},",
                "        },",
            ]
        )
    lines.extend(["    ];", ""])

    lines.append("    /// Machine-derived enumeration descriptors used by independent validation.")
    lines.append("    pub const ENUM_SPECS: &[crate::EnumSpec] = &[")
    for enum_name in enum_names:
        values_name = f"{snake_case(enum_name).upper()}_VALUES"
        lines.append(f"        crate::EnumSpec {{ name: {rust_string(enum_name)}, values: {values_name} }},")
    lines.extend(["    ];", ""])
    for enum_name in enum_names:
        values_name = f"{snake_case(enum_name).upper()}_VALUES"
        lines.append(f"    const {values_name}: &[&str] = &[")
        lines.extend(f"        {rust_string(value)}," for value in enums[enum_name])
        lines.extend(["    ];", ""])

    lines.extend(
        [
            "}",
            "",
            "pub use generated_contract::*;",
            "",
        ]
    )
    stats = {
        "entityCount": len(entities),
        "enumCount": len(enums),
        "strongIdCount": len(id_types),
    }
    return "\n".join(lines), stats


def outputs(root: Path) -> dict[Path, str]:
    generated, stats = generated_contract(root)
    manifest = {
        "canonicalVector": "fixtures/canonical/vector.json",
        "entityCount": stats["entityCount"],
        "enumCount": stats["enumCount"],
        "generator": "tools/generate_rust_contract.py",
        "logicalModel": "machine/logical-model.yaml",
        "output": GENERATED_PATH.as_posix(),
        "outputSha256": hashlib.sha256(generated.encode("utf-8")).hexdigest(),
        "schema": MANIFEST_SCHEMA,
        "strongIdCount": stats["strongIdCount"],
        "typedProjection": True,
    }
    return {
        GENERATED_PATH: generated,
        MANIFEST_PATH: json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    }


def run(root: Path, check: bool) -> int:
    failures: list[str] = []
    for relative, content in outputs(root).items():
        path = root / relative
        if check:
            if not path.is_file() or path.read_text(encoding="utf-8") != content:
                failures.append(relative.as_posix())
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    if failures:
        print("generated Rust contract mismatch:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("generated Rust contract: ok" if check else "generated Rust contract: written")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("root", nargs="?", default=".")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return run(Path(args.root).resolve(), args.check)


if __name__ == "__main__":
    raise SystemExit(main())
