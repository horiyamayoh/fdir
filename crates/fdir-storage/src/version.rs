#![forbid(unsafe_code)]

use fdir_canonical::CANONICAL_JSON_VERSION;

use crate::StorageError;

/// Frozen schema identifier for the authoritative snapshot container.
pub const SNAPSHOT_SCHEMA: &str = "fdir/snapshot/1";
/// Current supported snapshot format version.
pub const SNAPSHOT_VERSION: u64 = 1;
/// Frozen schema identifier for complete portable snapshot exports.
pub const SNAPSHOT_EXPORT_SCHEMA: &str = "fdir/snapshot-export/1";

/// Result of negotiating a snapshot header against this reader.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum VersionDecision {
    /// The snapshot is supported by the current reader.
    Supported,
    /// The schema is recognized, but this older version requires an explicit migration.
    Deprecated { found: u64, current: u64 },
    /// The schema is recognized, but the snapshot was produced by a newer writer.
    FutureUnknown { found: u64, current: u64 },
    /// The container schema is not compatible with this reader.
    IncompatibleSchema { found: String },
    /// The snapshot names a different canonical JSON contract.
    IncompatibleCanonicalJson {
        found: String,
        supported: &'static str,
    },
}

/// Classify a snapshot version without silently migrating or downgrading it.
#[must_use]
pub fn negotiate_snapshot_version(
    schema: &str,
    version: u64,
    canonical_json: &str,
) -> VersionDecision {
    if schema != SNAPSHOT_SCHEMA {
        return VersionDecision::IncompatibleSchema {
            found: schema.to_owned(),
        };
    }
    if canonical_json != CANONICAL_JSON_VERSION {
        return VersionDecision::IncompatibleCanonicalJson {
            found: canonical_json.to_owned(),
            supported: CANONICAL_JSON_VERSION,
        };
    }
    if version == SNAPSHOT_VERSION {
        VersionDecision::Supported
    } else if version < SNAPSHOT_VERSION {
        VersionDecision::Deprecated {
            found: version,
            current: SNAPSHOT_VERSION,
        }
    } else {
        VersionDecision::FutureUnknown {
            found: version,
            current: SNAPSHOT_VERSION,
        }
    }
}

pub(crate) fn require_supported(decision: VersionDecision) -> Result<(), StorageError> {
    match decision {
        VersionDecision::Supported => Ok(()),
        VersionDecision::Deprecated { found, current } => Err(StorageError::new(
            "FDIR-SNAPSHOT-VERSION-DEPRECATED",
            "$/version",
            format!("snapshot version {found} requires an explicit migration to version {current}"),
        )),
        VersionDecision::FutureUnknown { found, current } => Err(StorageError::new(
            "FDIR-SNAPSHOT-VERSION-FUTURE",
            "$/version",
            format!("snapshot version {found} is newer than supported version {current}"),
        )),
        VersionDecision::IncompatibleSchema { found } => Err(StorageError::new(
            "FDIR-SNAPSHOT-SCHEMA-INCOMPATIBLE",
            "$/schema",
            format!("snapshot schema {found:?} is incompatible with {SNAPSHOT_SCHEMA}"),
        )),
        VersionDecision::IncompatibleCanonicalJson { found, supported } => Err(StorageError::new(
            "FDIR-SNAPSHOT-CANONICAL-INCOMPATIBLE",
            "$/canonicalJson",
            format!("canonical JSON contract {found:?} is incompatible with {supported}"),
        )),
    }
}
