#![forbid(unsafe_code)]
//! Generated, language-neutral FDIR contract metadata.

include!("generated.rs");

#[cfg(test)]
mod tests {
    use super::{ENTITY_NAMES, ENUM_NAMES, MODEL_VERSION, ROOT_ENTITY};

    #[test]
    fn generated_registries_are_non_empty() {
        assert_eq!(MODEL_VERSION, "2.1.0");
        assert_eq!(ROOT_ENTITY, "Snapshot");
        assert!(!ENTITY_NAMES.is_empty());
        assert!(!ENUM_NAMES.is_empty());
    }
}
