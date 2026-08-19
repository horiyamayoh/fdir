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
        "7d1897743f643096a37625e50ac6deeb8cda8443ec63890f1f1ba17726f33dc5"
    );
    assert_eq!(ENTITY_NAMES.first().copied(), Some("Artifact"));
    assert_eq!(ENTITY_NAMES.last().copied(), Some("Snapshot"));
    assert!(ENUM_NAMES.contains(&"CompletenessState"));
}
