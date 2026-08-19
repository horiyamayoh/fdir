#![forbid(unsafe_code)]
//! Typed, generated, language-neutral FDIR contract projection.
//!
//! `machine/logical-model.yaml` remains authoritative. Generated strong identifiers, closed
//! enumerations, entity projections, and descriptors are checked byte-for-byte by the Python
//! repository quality oracle.

mod schema;
mod value;

pub use schema::{
    EntityDomain, EntitySpec, EnumSpec, PropertyKind, PropertySpec, entity_spec, enum_spec,
};
pub use value::{
    CanonicalValue, Digest, ExtensionMap, FiniteNumber, Identifier, JsonError, JsonNumber,
    ObjectValue, UnknownEnumValue, ValueError,
};

include!("generated.rs");

mod validation;

pub use validation::{ValidationReport, validate_snapshot_json, validate_snapshot_value};

#[cfg(test)]
mod tests {
    use std::any::TypeId;

    use super::{
        ArtifactId, ENTITY_NAMES, ENUM_NAMES, MODEL_VERSION, ROOT_ENTITY, UnitId, entity_spec,
    };

    #[test]
    fn generated_registries_are_complete() {
        assert_eq!(MODEL_VERSION, "2.1.0");
        assert_eq!(ROOT_ENTITY, "Snapshot");
        assert_eq!(ENTITY_NAMES.len(), 20);
        assert_eq!(ENUM_NAMES.len(), 8);
        assert_eq!(
            entity_spec("InformationUnit").map(|item| item.properties.len()),
            Some(1)
        );
    }

    #[test]
    fn generated_identifiers_are_not_interchangeable_types() {
        assert_ne!(TypeId::of::<ArtifactId>(), TypeId::of::<UnitId>());
    }
}
