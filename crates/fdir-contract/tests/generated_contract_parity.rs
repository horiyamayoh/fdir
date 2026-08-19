#![forbid(unsafe_code)]

use fdir_contract::{
    CANONICAL_VECTOR_JSON, CANONICAL_VECTOR_LENGTH, CANONICAL_VECTOR_SHA256, ENTITY_NAMES,
    ENUM_NAMES, MODEL_ID, MODEL_VERSION, ROOT_ENTITY,
};

const LOGICAL_MODEL: &str = include_str!("../../../machine/logical-model.yaml");
const CANONICAL_VECTOR: &str = include_str!("../../../fixtures/canonical/vector.json");

#[test]
fn generated_metadata_is_bound_to_repository_authorities() {
    assert!(LOGICAL_MODEL.contains(&format!("\"modelId\": \"{MODEL_ID}\"")));
    assert!(LOGICAL_MODEL.contains(&format!("\"version\": \"{MODEL_VERSION}\"")));
    assert!(LOGICAL_MODEL.contains(&format!("\"rootEntity\": \"{ROOT_ENTITY}\"")));
    assert_eq!(CANONICAL_VECTOR_JSON, CANONICAL_VECTOR);
    assert_eq!(CANONICAL_VECTOR_LENGTH, CANONICAL_VECTOR.len());
    assert_eq!(
        CANONICAL_VECTOR_SHA256,
        "1b455af0224e20dc9eb0737b84e13f50a389553ad0e0b54b21fafa1154b3070d"
    );
    assert_eq!(ENTITY_NAMES.first().copied(), Some("Artifact"));
    assert_eq!(ENTITY_NAMES.last().copied(), Some("Snapshot"));
    assert!(ENUM_NAMES.contains(&"CompletenessState"));
}
