#![forbid(unsafe_code)]
//! Authoritative storage boundary.

use fdir_core::{CapabilityStatus, CommandFailure, FailureClass};

/// Storage is intentionally unavailable until Issue #10.
pub const CAPABILITY: CapabilityStatus = CapabilityStatus::unavailable("authoritative-storage", 10);

/// Return an explicit unsupported failure rather than creating placeholder state.
#[must_use]
pub fn unavailable() -> CommandFailure {
    CommandFailure::new(
        FailureClass::Unsupported,
        "FDIR-STORAGE-UNAVAILABLE",
        "authoritative storage is owned by Issue #10",
    )
}
