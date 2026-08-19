#![forbid(unsafe_code)]
//! Canonical snapshot I/O, content-addressed evidence storage, and rebuildable SQLite queries.
//!
//! Snapshots are exact canonical JSON authorities addressed by their SHA-256 digest. Evidence and
//! auxiliary objects use the same independently verifiable layout. Writers publish synchronized
//! temporary files with an atomic rename, readers verify content identities before parsing, and an
//! exclusive mutation lock serializes snapshot commits with retention-safe garbage collection.
//! Incomplete temporary paths and stale locks are never accepted as complete state and require an
//! explicit recovery call.
//!
//! SQLite is a generated, versioned materialization only. Every open validates the database,
//! generated DDL identity, bound snapshot digest, canonical root copy, and all derived table rows.
//! Deleting the index loses no authority because it is rebuilt solely from canonical snapshots.
//! These implementations remain development-unqualified.

mod diagnostic;
mod index;
mod manifest;
mod store;
mod version;

use fdir_core::CapabilityStatus;

pub use diagnostic::{StorageDiagnostic, StorageError, ValidationReport};
pub use index::{
    INDEX_APPLICATION_ID, INDEX_DDL, INDEX_MATERIALIZER_VERSION, INDEX_SCHEMA_VERSION,
    IndexBuildMode, IndexConsistencyReport, IndexDifference, IndexQuery, IndexQueryRow,
    IndexReceipt, IndexVersionDecision, QueryConsistencyReport, QueryDifference, SqliteIndex,
    negotiate_index_version,
};
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

/// Implemented rebuildable-index boundary without a production qualification claim.
pub const INDEX_CAPABILITY: CapabilityStatus =
    CapabilityStatus::implemented_unqualified("rebuildable-sqlite-index", 11);

#[cfg(test)]
mod index_tests;
#[cfg(test)]
mod tests;
