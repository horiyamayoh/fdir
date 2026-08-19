#![forbid(unsafe_code)]
//! Exhaustive source-accounting boundary.

use fdir_core::{CapabilityStatus, CommandFailure, FailureClass};

/// Accounting is intentionally unavailable until Issue #17.
pub const CAPABILITY: CapabilityStatus = CapabilityStatus::unavailable("exhaustive-accounting", 17);

/// Return an explicit unsupported failure rather than an incomplete accounting claim.
#[must_use]
pub fn unavailable() -> CommandFailure {
    CommandFailure::new(
        FailureClass::Unsupported,
        "FDIR-ACCOUNTING-UNAVAILABLE",
        "exhaustive accounting is owned by Issue #17",
    )
}
