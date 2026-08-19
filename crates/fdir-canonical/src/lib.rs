#![forbid(unsafe_code)]
//! FDIR 2.1 canonical JSON, content digests, and acyclic identity construction.
//!
//! Canonical JSON remains the current identity authority. Content digests are plain SHA-256 over
//! canonical bytes, while entity identities add an explicit versioned domain separator and a
//! canonical identity envelope. Rebuildable projections, indexes, transport envelopes, worker
//! receipts, and operational timing are excluded from identity material.

use fdir_core::CapabilityStatus;

mod canonical;
mod identity;
mod raw;
mod sha256;

pub use canonical::{
    CANONICAL_JSON_VERSION, CanonicalError, IDENTITY_VERSION, canonical_bytes, canonical_string,
    canonicalize_json, content_digest, domain_separated_digest, is_canonical_json,
};
pub use identity::{
    IDENTITY_MATERIAL_SCHEMA, IdentityDag, IdentityDigest, IdentityError, IdentityKind,
    IdentityNode, IdentityReference, IdentityResult,
};
pub use raw::raw_content_digest;

/// Implemented canonical/identity boundary without a production qualification claim.
pub const CAPABILITY: CapabilityStatus =
    CapabilityStatus::implemented_unqualified("canonical-identity", 9);

#[cfg(test)]
mod tests {
    use super::CAPABILITY;

    #[test]
    fn capability_is_implemented_but_not_production_qualified() {
        const {
            assert!(CAPABILITY.available);
            assert!(!CAPABILITY.production_ready);
            assert!(CAPABILITY.owning_issue == 9);
        }
    }
}
