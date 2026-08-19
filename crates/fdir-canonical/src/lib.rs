#![forbid(unsafe_code)]
//! Canonical serialization and identity boundary.

use fdir_core::{CapabilityStatus, CommandFailure, FailureClass};

/// Canonical JSON and identity are intentionally unavailable until Issue #9.
pub const CAPABILITY: CapabilityStatus = CapabilityStatus::unavailable("canonical-identity", 9);

/// Return an explicit unsupported failure rather than placeholder output.
#[must_use]
pub fn unavailable() -> CommandFailure {
    CommandFailure::new(
        FailureClass::Unsupported,
        "FDIR-CANONICAL-UNAVAILABLE",
        "canonical serialization and identity are owned by Issue #9",
    )
}
