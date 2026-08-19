#![forbid(unsafe_code)]
//! Canonical snapshot I/O and content-addressed evidence storage.
//!
//! Snapshots are exact canonical JSON authorities addressed by their SHA-256 digest. Evidence and
//! auxiliary objects use the same independently verifiable layout. Writers publish synchronized
//! temporary files with an atomic rename, readers verify content identities before parsing, and an
//! exclusive mutation lock serializes snapshot commits with retention-safe garbage collection.
//! Incomplete temporary paths and stale locks are never accepted as complete state and require an
//! explicit recovery call. This implementation remains development-unqualified.

mod diagnostic;
mod manifest;
mod store;
mod version;

use fdir_core::CapabilityStatus;

pub use diagnostic::{StorageDiagnostic, StorageError, ValidationReport};
pub use manifest::{
    ObjectDescriptor, ObjectReference, ReferenceSource, SnapshotManifest, StatusTransition,
    is_allowed_status_transition, parse_snapshot_bytes,
};
pub use store::{
    GarbageCollectionMode, GarbageCollectionReport, RecoveryReport, SnapshotReceipt, SnapshotStore,
    StoredObject, WriteDisposition, WriteTransaction,
};
pub use version::{
    SNAPSHOT_EXPORT_SCHEMA, SNAPSHOT_SCHEMA, SNAPSHOT_VERSION, VersionDecision,
    negotiate_snapshot_version,
};

/// Implemented authoritative-storage boundary without a production qualification claim.
pub const CAPABILITY: CapabilityStatus =
    CapabilityStatus::implemented_unqualified("authoritative-storage", 10);

#[cfg(test)]
mod tests;
