#![forbid(unsafe_code)]
//! Empty first-party adapter registry for the product foundation.

use fdir_core::CapabilityStatus;

/// Adapter execution is unavailable; format implementations begin in later issues.
pub const CAPABILITY: CapabilityStatus = CapabilityStatus::unavailable("first-party-adapters", 14);

/// The foundation registers no adapters and therefore cannot accept documents.
#[must_use]
pub const fn registered_adapters() -> &'static [&'static str] {
    &[]
}

#[cfg(test)]
mod tests {
    use super::registered_adapters;

    #[test]
    fn foundation_has_no_placeholder_adapter() {
        assert!(registered_adapters().is_empty());
    }
}
