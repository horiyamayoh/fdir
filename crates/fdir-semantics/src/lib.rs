#![forbid(unsafe_code)]
//! Projection, equivalence, alignment, and lineage boundaries.

use fdir_core::CapabilityStatus;

/// Projection is unavailable until Issue #18.
pub const PROJECTION: CapabilityStatus = CapabilityStatus::unavailable("projection", 18);
/// Equivalence is unavailable until Issue #19.
pub const EQUIVALENCE: CapabilityStatus = CapabilityStatus::unavailable("equivalence", 19);
/// Alignment is unavailable until Issue #19.
pub const ALIGNMENT: CapabilityStatus = CapabilityStatus::unavailable("alignment", 19);
/// Lineage is unavailable until Issue #19.
pub const LINEAGE: CapabilityStatus = CapabilityStatus::unavailable("lineage", 19);
