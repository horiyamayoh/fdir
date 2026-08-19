#![forbid(unsafe_code)]

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File};
use std::path::{Path, PathBuf};
use std::str;
use std::sync::atomic::{AtomicU64, Ordering};

use fdir_canonical::{canonical_bytes, raw_content_digest};
use fdir_core::{CanonicalValue, Digest, ObjectValue};
use rusqlite::{Connection, OpenFlags, Transaction, TransactionBehavior, params, params_from_iter};

use crate::diagnostic::StorageError;
use crate::manifest::SnapshotManifest;

/// SQLite application identifier (`FDIR`) written into every materialized index.
pub const INDEX_APPLICATION_ID: i64 = 1_178_880_338;
/// Current generated SQLite schema version.
pub const INDEX_SCHEMA_VERSION: i64 = 1;
/// Current deterministic materializer implementation version.
pub const INDEX_MATERIALIZER_VERSION: &str = "fdir/sqlite-index-materializer/1";
/// Generated SQLite DDL. The generator parity gate owns this file.
pub const INDEX_DDL: &str = include_str!("../../../schemas/fdir.sql");

const INDEX_DUMP_SCHEMA: &str = "fdir/sqlite-index-dump/1";
const TEMPORARY_PREFIX: &str = ".fdir-index-tmp-";
const BACKUP_PREFIX: &str = ".fdir-index-backup-";
const META_SINGLETON: i64 = 1;

static TEMPORARY_COUNTER: AtomicU64 = AtomicU64::new(0);

const NON_COMPLETE_OUTCOMES: [&str; 9] = [
    "cancelled",
    "failed",
    "incomplete",
    "partial",
    "policy-excluded",
    "resource-limited",
    "unreadable",
    "unresolved",
    "unsupported",
];

/// How a canonical snapshot is materialized into SQLite.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum IndexBuildMode {
    /// Create a new index and fail when the target already exists.
    Clean,
    /// Build a complete replacement beside the target and then publish it.
    Full,
    /// Replace all authoritative projection rows in one transaction while retaining the file.
    Incremental,
}

impl IndexBuildMode {
    const fn as_str(self) -> &'static str {
        match self {
            Self::Clean => "clean",
            Self::Full => "full",
            Self::Incremental => "incremental",
        }
    }
}

/// Version-negotiation result for an existing SQLite index.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum IndexVersionDecision {
    /// The index uses the current generated schema.
    Current,
    /// The index is older or unversioned and requires a complete rebuild.
    RebuildRequired { found: i64, supported: i64 },
    /// The index was created by a newer materializer and cannot be trusted by this build.
    UnsupportedFuture { found: i64, supported: i64 },
}

/// Decide whether an index schema version can be read by this build.
#[must_use]
pub const fn negotiate_index_version(found: i64) -> IndexVersionDecision {
    if found == INDEX_SCHEMA_VERSION {
        IndexVersionDecision::Current
    } else if found < INDEX_SCHEMA_VERSION {
        IndexVersionDecision::RebuildRequired {
            found,
            supported: INDEX_SCHEMA_VERSION,
        }
    } else {
        IndexVersionDecision::UnsupportedFuture {
            found,
            supported: INDEX_SCHEMA_VERSION,
        }
    }
}

/// Stable supported query groups whose results are compared with canonical traversal.
#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub enum IndexQuery {
    /// Information units.
    Units,
    /// Record assertions.
    Assertions,
    /// Snapshot objects, object-reference edges, and assertion-to-occurrence evidence links.
    Evidence,
    /// Information relations.
    Relations,
    /// Guarantee-status vectors and explicit snapshot status transitions.
    StatusVectors,
    /// Capability declarations retained in the canonical snapshot.
    Capabilities,
    /// Explicit or referenced profile declarations.
    Profiles,
    /// Diagnostics.
    Diagnostics,
    /// Snapshot provenance.
    Provenance,
    /// Explicit incomplete, partial, unsupported, and other non-complete outcomes.
    NonCompleteOutcomes,
}

impl IndexQuery {
    /// Every supported query group in stable order.
    pub const ALL: [Self; 10] = [
        Self::Units,
        Self::Assertions,
        Self::Evidence,
        Self::Relations,
        Self::StatusVectors,
        Self::Capabilities,
        Self::Profiles,
        Self::Diagnostics,
        Self::Provenance,
        Self::NonCompleteOutcomes,
    ];

    /// Stable machine-readable query name.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Units => "units",
            Self::Assertions => "assertions",
            Self::Evidence => "evidence",
            Self::Relations => "relations",
            Self::StatusVectors => "status-vectors",
            Self::Capabilities => "capabilities",
            Self::Profiles => "profiles",
            Self::Diagnostics => "diagnostics",
            Self::Provenance => "provenance",
            Self::NonCompleteOutcomes => "non-complete-outcomes",
        }
    }

    fn accepts_category(self, category: &str) -> bool {
        match self {
            Self::Units => category == "unit",
            Self::Assertions => category == "assertion",
            Self::Evidence => matches!(
                category,
                "assertion-evidence" | "evidence-object" | "evidence-reference"
            ),
            Self::Relations => category == "relation",
            Self::StatusVectors => matches!(category, "status" | "status-transition"),
            Self::Capabilities => category == "capability",
            Self::Profiles => category == "profile",
            Self::Diagnostics => category == "diagnostic",
            Self::Provenance => category == "provenance",
            Self::NonCompleteOutcomes => category == "outcome",
        }
    }
}

/// One deterministic supported-query row.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct IndexQueryRow {
    key: String,
    source_path: String,
    json: String,
}

impl IndexQueryRow {
    /// Stable row key within the query group.
    #[must_use]
    pub fn key(&self) -> &str {
        &self.key
    }

    /// RFC 6901 path to the canonical source represented by this row.
    #[must_use]
    pub fn source_path(&self) -> &str {
        &self.source_path
    }

    /// Exact deterministic JSON retained or derived from the canonical source path.
    #[must_use]
    pub fn json(&self) -> &str {
        &self.json
    }
}

/// One table-level consistency difference.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct IndexDifference {
    table: &'static str,
    expected_rows: usize,
    actual_rows: usize,
}

impl IndexDifference {
    /// Generated table containing a difference.
    #[must_use]
    pub const fn table(&self) -> &'static str {
        self.table
    }

    /// Number of rows derived from the canonical snapshot.
    #[must_use]
    pub const fn expected_rows(&self) -> usize {
        self.expected_rows
    }

    /// Number of rows read from SQLite.
    #[must_use]
    pub const fn actual_rows(&self) -> usize {
        self.actual_rows
    }
}

/// Canonical-vs-index comparison evidence.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct IndexConsistencyReport {
    snapshot_digest: Digest,
    expected_content_digest: Digest,
    actual_content_digest: Digest,
    differences: Vec<IndexDifference>,
}

impl IndexConsistencyReport {
    /// Whether every generated projection table matches canonical traversal.
    #[must_use]
    pub fn is_consistent(&self) -> bool {
        self.differences.is_empty()
            && self.expected_content_digest == self.actual_content_digest
    }

    /// Snapshot content identity bound to the comparison.
    #[must_use]
    pub const fn snapshot_digest(&self) -> &Digest {
        &self.snapshot_digest
    }

    /// Digest of the deterministic projection derived from canonical JSON.
    #[must_use]
    pub const fn expected_content_digest(&self) -> &Digest {
        &self.expected_content_digest
    }

    /// Digest of the deterministic projection read from SQLite.
    #[must_use]
    pub const fn actual_content_digest(&self) -> &Digest {
        &self.actual_content_digest
    }

    /// Table-level differences in generated table order.
    #[must_use]
    pub fn differences(&self) -> &[IndexDifference] {
        &self.differences
    }
}

/// One supported-query consistency difference.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct QueryDifference {
    query: IndexQuery,
    expected_rows: usize,
    actual_rows: usize,
}

impl QueryDifference {
    /// Query group that differs.
    #[must_use]
    pub const fn query(&self) -> IndexQuery {
        self.query
    }

    /// Canonically traversed row count.
    #[must_use]
    pub const fn expected_rows(&self) -> usize {
        self.expected_rows
    }

    /// SQLite row count.
    #[must_use]
    pub const fn actual_rows(&self) -> usize {
        self.actual_rows
    }
}

/// Query-by-query canonical consistency evidence.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct QueryConsistencyReport {
    snapshot_digest: Digest,
    differences: Vec<QueryDifference>,
}

impl QueryConsistencyReport {
    /// Whether all supported queries match canonical traversal.
    #[must_use]
    pub fn is_consistent(&self) -> bool {
        self.differences.is_empty()
    }

    /// Snapshot content identity bound to the report.
    #[must_use]
    pub const fn snapshot_digest(&self) -> &Digest {
        &self.snapshot_digest
    }

    /// Query groups whose rows differ.
    #[must_use]
    pub fn differences(&self) -> &[QueryDifference] {
        &self.differences
    }
}

/// Deterministic evidence returned after a successful index build or update.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct IndexReceipt {
    snapshot_digest: Digest,
    content_digest: Digest,
    mode: IndexBuildMode,
    generation: u64,
    row_count: usize,
}

impl IndexReceipt {
    /// Canonical snapshot identity bound to the completed index.
    #[must_use]
    pub const fn snapshot_digest(&self) -> &Digest {
        &self.snapshot_digest
    }

    /// Digest of the canonicalized projection dump, excluding operational metadata.
    #[must_use]
    pub const fn content_digest(&self) -> &Digest {
        &self.content_digest
    }

    /// Build path used for this receipt.
    #[must_use]
    pub const fn mode(&self) -> IndexBuildMode {
        self.mode
    }

    /// Monotonic file-local generation. Clean and full builds begin at one.
    #[must_use]
    pub const fn generation(&self) -> u64 {
        self.generation
    }

    /// Number of rebuildable projection rows across generated tables.
    #[must_use]
    pub const fn row_count(&self) -> usize {
        self.row_count
    }
}

/// A validated rebuildable SQLite materialized index.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SqliteIndex {
    path: PathBuf,
    snapshot_digest: Digest,
}

impl SqliteIndex {
    /// Build or update an index from one canonical snapshot.
    ///
    /// Clean and full builds create a complete temporary database before publication. Incremental
    /// updates replace every authoritative projection row in one transaction. Build mode and
    /// generation are operational metadata and are excluded from the content digest.
    pub fn build(
        path: impl AsRef<Path>,
        manifest: &SnapshotManifest,
        mode: IndexBuildMode,
    ) -> Result<IndexReceipt, StorageError> {
        let path = path.as_ref().to_path_buf();
        let source = ProjectionSource::from_manifest(manifest)?;
        match mode {
            IndexBuildMode::Clean => {
                if path.exists() {
                    return Err(StorageError::new(
                        "FDIR-INDEX-EXISTS",
                        display_path(&path),
                        "clean index build requires an absent target",
                    ));
                }
                build_replacement(&path, &source, mode)?;
            }
            IndexBuildMode::Full => build_replacement(&path, &source, mode)?,
            IndexBuildMode::Incremental => update_incrementally(&path, &source)?,
        }

        let index = Self::open(&path, &source.snapshot_digest)?;
        let connection = index.validated_connection()?;
        let metadata = read_metadata(&connection, &path)?;
        let actual = read_projection(&connection, &path)?;
        let content_digest = actual.content_digest()?;
        Ok(IndexReceipt {
            snapshot_digest: source.snapshot_digest,
            content_digest,
            mode,
            generation: metadata.generation,
            row_count: actual.row_count(),
        })
    }

    /// Open an index only after integrity, version, DDL, snapshot, and projection validation.
    pub fn open(
        path: impl AsRef<Path>,
        expected_snapshot: &Digest,
    ) -> Result<Self, StorageError> {
        let index = Self {
            path: path.as_ref().to_path_buf(),
            snapshot_digest: expected_snapshot.clone(),
        };
        let connection = open_read_only(&index.path)?;
        let validated = validate_root(&connection, &index.path, Some(expected_snapshot))?;
        let actual = read_projection(&connection, &index.path)?;
        let differences = projection_differences(&validated.projection, &actual);
        if !differences.is_empty() {
            return Err(consistency_error(&index.path, &differences));
        }
        Ok(index)
    }

    /// Borrow the SQLite file path.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Borrow the expected canonical snapshot digest.
    #[must_use]
    pub const fn snapshot_digest(&self) -> &Digest {
        &self.snapshot_digest
    }

    /// Query a validated materialization in deterministic key/path order.
    pub fn query(&self, query: IndexQuery) -> Result<Vec<IndexQueryRow>, StorageError> {
        let connection = self.validated_connection()?;
        let actual = read_projection(&connection, &self.path)?;
        Ok(actual.query(query))
    }

    /// Traverse a canonical snapshot using the same supported-query semantics without SQLite.
    pub fn query_snapshot(
        manifest: &SnapshotManifest,
        query: IndexQuery,
    ) -> Result<Vec<IndexQueryRow>, StorageError> {
        Ok(ProjectionSource::from_manifest(manifest)?.projection.query(query))
    }

    /// Compare all generated table rows with a supplied canonical snapshot.
    pub fn consistency_report(
        &self,
        manifest: &SnapshotManifest,
    ) -> Result<IndexConsistencyReport, StorageError> {
        let source = ProjectionSource::from_manifest(manifest)?;
        if source.snapshot_digest != self.snapshot_digest {
            return Err(StorageError::new(
                "FDIR-INDEX-SNAPSHOT-MISMATCH",
                display_path(&self.path),
                format!(
                    "index handle expects {}, but supplied snapshot is {}",
                    self.snapshot_digest, source.snapshot_digest
                ),
            ));
        }
        let connection = open_read_only(&self.path)?;
        validate_root(&connection, &self.path, Some(&self.snapshot_digest))?;
        let actual = read_projection(&connection, &self.path)?;
        let expected_content_digest = source.projection.content_digest()?;
        let actual_content_digest = actual.content_digest()?;
        let differences = projection_differences(&source.projection, &actual);
        Ok(IndexConsistencyReport {
            snapshot_digest: source.snapshot_digest,
            expected_content_digest,
            actual_content_digest,
            differences,
        })
    }

    /// Compare every supported query with direct canonical traversal.
    pub fn query_consistency_report(
        &self,
        manifest: &SnapshotManifest,
    ) -> Result<QueryConsistencyReport, StorageError> {
        let source = ProjectionSource::from_manifest(manifest)?;
        if source.snapshot_digest != self.snapshot_digest {
            return Err(StorageError::new(
                "FDIR-INDEX-SNAPSHOT-MISMATCH",
                display_path(&self.path),
                format!(
                    "index handle expects {}, but supplied snapshot is {}",
                    self.snapshot_digest, source.snapshot_digest
                ),
            ));
        }
        let connection = open_read_only(&self.path)?;
        validate_root(&connection, &self.path, Some(&self.snapshot_digest))?;
        let actual = read_projection(&connection, &self.path)?;
        let mut differences = Vec::new();
        for query in IndexQuery::ALL {
            let expected_rows = source.projection.query(query);
            let actual_rows = actual.query(query);
            if expected_rows != actual_rows {
                differences.push(QueryDifference {
                    query,
                    expected_rows: expected_rows.len(),
                    actual_rows: actual_rows.len(),
                });
            }
        }
        Ok(QueryConsistencyReport {
            snapshot_digest: source.snapshot_digest,
            differences,
        })
    }

    /// Return the canonicalized projection dump, excluding build mode and generation.
    pub fn canonical_dump(&self) -> Result<Vec<u8>, StorageError> {
        let connection = self.validated_connection()?;
        read_projection(&connection, &self.path)?.canonical_dump()
    }

    /// Return the digest of the canonicalized projection dump.
    pub fn content_digest(&self) -> Result<Digest, StorageError> {
        let connection = self.validated_connection()?;
        read_projection(&connection, &self.path)?.content_digest()
    }

    /// Return the file-local update generation after complete validation.
    pub fn generation(&self) -> Result<u64, StorageError> {
        let connection = self.validated_connection()?;
        Ok(read_metadata(&connection, &self.path)?.generation)
    }

    /// Explicitly invalidate an index by deleting it. Canonical snapshots remain untouched.
    pub fn invalidate(path: impl AsRef<Path>) -> Result<bool, StorageError> {
        let path = path.as_ref();
        match fs::remove_file(path) {
            Ok(()) => Ok(true),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
            Err(error) => Err(io_error(
                "FDIR-INDEX-INVALIDATE",
                path,
                "index could not be invalidated",
                error,
            )),
        }
    }

    fn validated_connection(&self) -> Result<Connection, StorageError> {
        let connection = open_read_only(&self.path)?;
        let validated = validate_root(&connection, &self.path, Some(&self.snapshot_digest))?;
        let actual = read_projection(&connection, &self.path)?;
        let differences = projection_differences(&validated.projection, &actual);
        if !differences.is_empty() {
            return Err(consistency_error(&self.path, &differences));
        }
        Ok(connection)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct ProjectionSource {
    projection: Projection,
    snapshot_digest: Digest,
    snapshot_byte_length: u64,
}

impl ProjectionSource {
    fn from_manifest(manifest: &SnapshotManifest) -> Result<Self, StorageError> {
        let bytes = manifest.canonical_bytes()?;
        let snapshot_digest = raw_digest(&bytes, "$/snapshot")?;
        let snapshot_byte_length = u64::try_from(bytes.len()).map_err(|error| {
            StorageError::new(
                "FDIR-INDEX-SNAPSHOT-LENGTH",
                "$/snapshot",
                format!("canonical snapshot length cannot be represented as u64: {error}"),
            )
        })?;
        let input = str::from_utf8(&bytes).map_err(|error| {
            StorageError::new(
                "FDIR-INDEX-SNAPSHOT-UTF8",
                "$/snapshot",
                format!("canonical snapshot is not UTF-8: {error}"),
            )
        })?;
        let root = CanonicalValue::parse_json(input).map_err(|error| {
            StorageError::new(
                "FDIR-INDEX-SNAPSHOT-JSON",
                "$/snapshot",
                format!("canonical snapshot could not be parsed: {error}"),
            )
        })?;
        let projection = Projection::from_root(&root)?;
        Ok(Self {
            projection,
            snapshot_digest,
            snapshot_byte_length,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct ValidatedRoot {
    projection: Projection,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct IndexMetadata {
    snapshot_digest: Digest,
    snapshot_byte_length: u64,
    generation: u64,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
struct Projection {
    tables: BTreeMap<&'static str, Vec<Vec<String>>>,
}

impl Projection {
    fn from_root(root: &CanonicalValue) -> Result<Self, StorageError> {
        let root_object = root.as_object().ok_or_else(|| {
            StorageError::new(
                "FDIR-INDEX-SNAPSHOT-TYPE",
                "",
                "canonical snapshot root must be an object",
            )
        })?;
        let mut projection = Self::default();
        walk_canonical_nodes(root, "", &mut projection);

        let payload = required_object(root_object, "payload", "/payload")?;
        for entity in ENTITY_SPECS {
            project_entity_array(payload, entity, &mut projection)?;
        }
        project_snapshot_objects(root_object, &mut projection)?;
        project_object_references(root_object, &mut projection)?;
        project_status_transitions(root_object, &mut projection)?;
        project_provenance(root_object, &mut projection)?;
        project_capabilities_and_profiles(root, payload, &mut projection)?;
        project_outcomes(root, "", &mut projection);
        projection.normalize_and_validate()?;
        Ok(projection)
    }

    fn push(&mut self, table: &'static str, row: Vec<String>) {
        self.tables.entry(table).or_default().push(row);
    }

    fn rows(&self, table: &'static str) -> &[Vec<String>] {
        self.tables.get(table).map_or(&[], Vec::as_slice)
    }

    fn normalize_and_validate(&mut self) -> Result<(), StorageError> {
        for spec in TABLE_SPECS {
            let rows = self.tables.entry(spec.name).or_default();
            rows.sort();
            for row in rows.iter() {
                if row.len() != spec.columns.len() {
                    return Err(StorageError::new(
                        "FDIR-INDEX-INTERNAL-ROW",
                        spec.name,
                        format!(
                            "generated row has {} columns; expected {}",
                            row.len(),
                            spec.columns.len()
                        ),
                    ));
                }
            }
            for pair in rows.windows(2) {
                if pair[0][..spec.key_columns] == pair[1][..spec.key_columns] {
                    return Err(StorageError::new(
                        "FDIR-INDEX-DUPLICATE-KEY",
                        spec.name,
                        format!(
                            "canonical snapshot produces duplicate key {:?}",
                            &pair[0][..spec.key_columns]
                        ),
                    ));
                }
            }
        }
        Ok(())
    }

    fn row_count(&self) -> usize {
        TABLE_SPECS
            .iter()
            .map(|spec| self.rows(spec.name).len())
            .sum()
    }

    fn canonical_dump(&self) -> Result<Vec<u8>, StorageError> {
        let mut tables = ObjectValue::new();
        for spec in TABLE_SPECS {
            let mut values = Vec::new();
            for row in self.rows(spec.name) {
                let mut value = ObjectValue::new();
                for (column, cell) in spec.columns.iter().zip(row) {
                    value.insert(
                        (*column).to_owned(),
                        CanonicalValue::String(cell.clone()),
                    );
                }
                values.push(CanonicalValue::Object(value));
            }
            tables.insert(spec.name.to_owned(), CanonicalValue::Array(values));
        }
        let mut dump = ObjectValue::new();
        dump.insert(
            "schema".to_owned(),
            CanonicalValue::String(INDEX_DUMP_SCHEMA.to_owned()),
        );
        dump.insert("tables".to_owned(), CanonicalValue::Object(tables));
        canonical_bytes(&CanonicalValue::Object(dump)).map_err(|error| {
            StorageError::new(
                "FDIR-INDEX-DUMP",
                "$",
                format!("projection dump could not be canonicalized: {error}"),
            )
        })
    }

    fn content_digest(&self) -> Result<Digest, StorageError> {
        raw_digest(&self.canonical_dump()?, "$/indexDump")
    }

    fn query(&self, query: IndexQuery) -> Vec<IndexQueryRow> {
        let mut rows = self
            .rows("materialized_records")
            .iter()
            .filter(|row| query.accepts_category(&row[0]))
            .map(|row| IndexQueryRow {
                key: row[2].clone(),
                source_path: row[1].clone(),
                json: row[3].clone(),
            })
            .collect::<Vec<_>>();
        rows.sort_by(|left, right| {
            (&left.key, &left.source_path, &left.json).cmp(&(
                &right.key,
                &right.source_path,
                &right.json,
            ))
        });
        rows
    }
}

#[derive(Clone, Copy)]
struct EntitySpec {
    payload_field: &'static str,
    table: &'static str,
    identity_field: &'static str,
    query_category: Option<&'static str>,
}

const ENTITY_SPECS: [EntitySpec; 10] = [
    EntitySpec {
        payload_field: "artifacts",
        table: "artifacts",
        identity_field: "artifactId",
        query_category: None,
    },
    EntitySpec {
        payload_field: "carriers",
        table: "carriers",
        identity_field: "carrierId",
        query_category: None,
    },
    EntitySpec {
        payload_field: "occurrences",
        table: "occurrences",
        identity_field: "occurrenceId",
        query_category: None,
    },
    EntitySpec {
        payload_field: "units",
        table: "units",
        identity_field: "unitId",
        query_category: Some("unit"),
    },
    EntitySpec {
        payload_field: "assertions",
        table: "assertions",
        identity_field: "assertionId",
        query_category: Some("assertion"),
    },
    EntitySpec {
        payload_field: "relations",
        table: "relations",
        identity_field: "relationId",
        query_category: Some("relation"),
    },
    EntitySpec {
        payload_field: "inventoryDomains",
        table: "inventory_domains",
        identity_field: "inventoryDomainId",
        query_category: None,
    },
    EntitySpec {
        payload_field: "accountingItems",
        table: "accounting_items",
        identity_field: "accountingItemId",
        query_category: None,
    },
    EntitySpec {
        payload_field: "guaranteeStatuses",
        table: "guarantee_statuses",
        identity_field: "guaranteeStatusId",
        query_category: Some("status"),
    },
    EntitySpec {
        payload_field: "diagnostics",
        table: "diagnostics",
        identity_field: "diagnosticId",
        query_category: Some("diagnostic"),
    },
];

#[derive(Clone, Copy)]
struct TableSpec {
    name: &'static str,
    columns: &'static [&'static str],
    key_columns: usize,
    insert_sql: &'static str,
    select_sql: &'static str,
}

const TABLE_SPECS: [TableSpec; 20] = [
    TableSpec {
        name: "canonical_nodes",
        columns: &["path", "kind", "json"],
        key_columns: 1,
        insert_sql: "INSERT INTO canonical_nodes(path, kind, json) VALUES (?1, ?2, ?3)",
        select_sql: "SELECT path, kind, json FROM canonical_nodes ORDER BY path",
    },
    TableSpec {
        name: "materialized_records",
        columns: &["category", "sourcePath", "recordKey", "json"],
        key_columns: 2,
        insert_sql: "INSERT INTO materialized_records(category, sourcePath, recordKey, json) VALUES (?1, ?2, ?3, ?4)",
        select_sql: "SELECT category, sourcePath, recordKey, json FROM materialized_records ORDER BY category, sourcePath",
    },
    TableSpec {
        name: "artifacts",
        columns: &["artifactId", "sourcePath", "json"],
        key_columns: 1,
        insert_sql: "INSERT INTO artifacts(artifactId, sourcePath, json) VALUES (?1, ?2, ?3)",
        select_sql: "SELECT artifactId, sourcePath, json FROM artifacts ORDER BY artifactId",
    },
    TableSpec {
        name: "carriers",
        columns: &["carrierId", "sourcePath", "json"],
        key_columns: 1,
        insert_sql: "INSERT INTO carriers(carrierId, sourcePath, json) VALUES (?1, ?2, ?3)",
        select_sql: "SELECT carrierId, sourcePath, json FROM carriers ORDER BY carrierId",
    },
    TableSpec {
        name: "occurrences",
        columns: &["occurrenceId", "sourcePath", "json"],
        key_columns: 1,
        insert_sql: "INSERT INTO occurrences(occurrenceId, sourcePath, json) VALUES (?1, ?2, ?3)",
        select_sql: "SELECT occurrenceId, sourcePath, json FROM occurrences ORDER BY occurrenceId",
    },
    TableSpec {
        name: "units",
        columns: &["unitId", "sourcePath", "json"],
        key_columns: 1,
        insert_sql: "INSERT INTO units(unitId, sourcePath, json) VALUES (?1, ?2, ?3)",
        select_sql: "SELECT unitId, sourcePath, json FROM units ORDER BY unitId",
    },
    TableSpec {
        name: "assertions",
        columns: &["assertionId", "sourcePath", "json"],
        key_columns: 1,
        insert_sql: "INSERT INTO assertions(assertionId, sourcePath, json) VALUES (?1, ?2, ?3)",
        select_sql: "SELECT assertionId, sourcePath, json FROM assertions ORDER BY assertionId",
    },
    TableSpec {
        name: "relations",
        columns: &["relationId", "sourcePath", "json"],
        key_columns: 1,
        insert_sql: "INSERT INTO relations(relationId, sourcePath, json) VALUES (?1, ?2, ?3)",
        select_sql: "SELECT relationId, sourcePath, json FROM relations ORDER BY relationId",
    },
    TableSpec {
        name: "inventory_domains",
        columns: &["inventoryDomainId", "sourcePath", "json"],
        key_columns: 1,
        insert_sql: "INSERT INTO inventory_domains(inventoryDomainId, sourcePath, json) VALUES (?1, ?2, ?3)",
        select_sql: "SELECT inventoryDomainId, sourcePath, json FROM inventory_domains ORDER BY inventoryDomainId",
    },
    TableSpec {
        name: "accounting_items",
        columns: &["accountingItemId", "sourcePath", "json"],
        key_columns: 1,
        insert_sql: "INSERT INTO accounting_items(accountingItemId, sourcePath, json) VALUES (?1, ?2, ?3)",
        select_sql: "SELECT accountingItemId, sourcePath, json FROM accounting_items ORDER BY accountingItemId",
    },
    TableSpec {
        name: "guarantee_statuses",
        columns: &["guaranteeStatusId", "sourcePath", "json"],
        key_columns: 1,
        insert_sql: "INSERT INTO guarantee_statuses(guaranteeStatusId, sourcePath, json) VALUES (?1, ?2, ?3)",
        select_sql: "SELECT guaranteeStatusId, sourcePath, json FROM guarantee_statuses ORDER BY guaranteeStatusId",
    },
    TableSpec {
        name: "diagnostics",
        columns: &["diagnosticId", "sourcePath", "json"],
        key_columns: 1,
        insert_sql: "INSERT INTO diagnostics(diagnosticId, sourcePath, json) VALUES (?1, ?2, ?3)",
        select_sql: "SELECT diagnosticId, sourcePath, json FROM diagnostics ORDER BY diagnosticId",
    },
    TableSpec {
        name: "snapshot_objects",
        columns: &["digest", "byteLength", "mediaType", "role", "sourcePath"],
        key_columns: 1,
        insert_sql: "INSERT INTO snapshot_objects(digest, byteLength, mediaType, role, sourcePath) VALUES (?1, ?2, ?3, ?4, ?5)",
        select_sql: "SELECT digest, CAST(byteLength AS TEXT), mediaType, role, sourcePath FROM snapshot_objects ORDER BY digest",
    },
    TableSpec {
        name: "object_references",
        columns: &["source", "target", "relation", "sourcePath"],
        key_columns: 3,
        insert_sql: "INSERT INTO object_references(source, target, relation, sourcePath) VALUES (?1, ?2, ?3, ?4)",
        select_sql: "SELECT source, target, relation, sourcePath FROM object_references ORDER BY source, target, relation",
    },
    TableSpec {
        name: "status_transitions",
        columns: &["fromState", "toState", "sourcePath"],
        key_columns: 2,
        insert_sql: "INSERT INTO status_transitions(fromState, toState, sourcePath) VALUES (?1, ?2, ?3)",
        select_sql: "SELECT fromState, toState, sourcePath FROM status_transitions ORDER BY fromState, toState",
    },
    TableSpec {
        name: "assertion_evidence",
        columns: &["assertionId", "occurrenceId", "sourcePath", "json"],
        key_columns: 2,
        insert_sql: "INSERT INTO assertion_evidence(assertionId, occurrenceId, sourcePath, json) VALUES (?1, ?2, ?3, ?4)",
        select_sql: "SELECT assertionId, occurrenceId, sourcePath, json FROM assertion_evidence ORDER BY assertionId, occurrenceId",
    },
    TableSpec {
        name: "provenance_records",
        columns: &["recordKey", "sourcePath", "json"],
        key_columns: 1,
        insert_sql: "INSERT INTO provenance_records(recordKey, sourcePath, json) VALUES (?1, ?2, ?3)",
        select_sql: "SELECT recordKey, sourcePath, json FROM provenance_records ORDER BY recordKey",
    },
    TableSpec {
        name: "capabilities",
        columns: &["capabilityId", "sourcePath", "json"],
        key_columns: 1,
        insert_sql: "INSERT INTO capabilities(capabilityId, sourcePath, json) VALUES (?1, ?2, ?3)",
        select_sql: "SELECT capabilityId, sourcePath, json FROM capabilities ORDER BY capabilityId",
    },
    TableSpec {
        name: "profiles",
        columns: &["profileId", "sourcePath", "json"],
        key_columns: 1,
        insert_sql: "INSERT INTO profiles(profileId, sourcePath, json) VALUES (?1, ?2, ?3)",
        select_sql: "SELECT profileId, sourcePath, json FROM profiles ORDER BY profileId",
    },
    TableSpec {
        name: "outcomes",
        columns: &["sourcePath", "outcomeKey", "state", "json"],
        key_columns: 1,
        insert_sql: "INSERT INTO outcomes(sourcePath, outcomeKey, state, json) VALUES (?1, ?2, ?3, ?4)",
        select_sql: "SELECT sourcePath, outcomeKey, state, json FROM outcomes ORDER BY sourcePath",
    },
];

fn project_entity_array(
    payload: &ObjectValue,
    spec: EntitySpec,
    projection: &mut Projection,
) -> Result<(), StorageError> {
    let Some(value) = payload.get(spec.payload_field) else {
        return Ok(());
    };
    let array = value.as_array().ok_or_else(|| {
        StorageError::new(
            "FDIR-INDEX-ENTITY-ARRAY",
            format!("/payload/{}", pointer_escape(spec.payload_field)),
            format!("{} must be an array", spec.payload_field),
        )
    })?;
    for (index, item) in array.iter().enumerate() {
        let source_path = format!(
            "/payload/{}/{}",
            pointer_escape(spec.payload_field),
            index
        );
        let object = item.as_object().ok_or_else(|| {
            StorageError::new(
                "FDIR-INDEX-ENTITY-TYPE",
                &source_path,
                format!("{} item must be an object", spec.payload_field),
            )
        })?;
        let identity = required_string(object, spec.identity_field, &source_path)?;
        let json = item.to_json();
        projection.push(
            spec.table,
            vec![identity.to_owned(), source_path.clone(), json.clone()],
        );
        if let Some(category) = spec.query_category {
            add_materialized(
                projection,
                category,
                identity,
                &source_path,
                &json,
            );
        }
        if spec.payload_field == "assertions" {
            project_assertion_evidence(object, identity, &source_path, projection)?;
        }
    }
    Ok(())
}

fn project_assertion_evidence(
    assertion: &ObjectValue,
    assertion_id: &str,
    assertion_path: &str,
    projection: &mut Projection,
) -> Result<(), StorageError> {
    let Some(value) = assertion.get("occurrenceIds") else {
        return Ok(());
    };
    let occurrences = value.as_array().ok_or_else(|| {
        StorageError::new(
            "FDIR-INDEX-EVIDENCE-ARRAY",
            format!("{assertion_path}/occurrenceIds"),
            "assertion occurrenceIds must be an array",
        )
    })?;
    for (index, occurrence) in occurrences.iter().enumerate() {
        let occurrence_id = occurrence.as_str().ok_or_else(|| {
            StorageError::new(
                "FDIR-INDEX-EVIDENCE-ID",
                format!("{assertion_path}/occurrenceIds/{index}"),
                "assertion evidence identifier must be a string",
            )
        })?;
        let source_path = format!("{assertion_path}/occurrenceIds/{index}");
        let json = string_object_json(&[
            ("assertionId", assertion_id),
            ("occurrenceId", occurrence_id),
        ]);
        projection.push(
            "assertion_evidence",
            vec![
                assertion_id.to_owned(),
                occurrence_id.to_owned(),
                source_path.clone(),
                json.clone(),
            ],
        );
        add_materialized(
            projection,
            "assertion-evidence",
            &format!("{assertion_id}:{occurrence_id}"),
            &source_path,
            &json,
        );
    }
    Ok(())
}

fn project_snapshot_objects(
    root: &ObjectValue,
    projection: &mut Projection,
) -> Result<(), StorageError> {
    let objects = required_array(root, "objects", "/objects")?;
    for (index, item) in objects.iter().enumerate() {
        let source_path = format!("/objects/{index}");
        let object = item.as_object().ok_or_else(|| {
            StorageError::new(
                "FDIR-INDEX-OBJECT-TYPE",
                &source_path,
                "snapshot object descriptor must be an object",
            )
        })?;
        let digest = required_string(object, "digest", &source_path)?;
        let byte_length = required_number_text(object, "byteLength", &source_path)?;
        let media_type = required_string(object, "mediaType", &source_path)?;
        let role = required_string(object, "role", &source_path)?;
        let json = item.to_json();
        projection.push(
            "snapshot_objects",
            vec![
                digest.to_owned(),
                byte_length.to_owned(),
                media_type.to_owned(),
                role.to_owned(),
                source_path.clone(),
            ],
        );
        add_materialized(
            projection,
            "evidence-object",
            digest,
            &source_path,
            &json,
        );
    }
    Ok(())
}

fn project_object_references(
    root: &ObjectValue,
    projection: &mut Projection,
) -> Result<(), StorageError> {
    let references = required_array(root, "references", "/references")?;
    for (index, item) in references.iter().enumerate() {
        let source_path = format!("/references/{index}");
        let object = item.as_object().ok_or_else(|| {
            StorageError::new(
                "FDIR-INDEX-REFERENCE-TYPE",
                &source_path,
                "snapshot object reference must be an object",
            )
        })?;
        let source = required_string(object, "source", &source_path)?;
        let target = required_string(object, "target", &source_path)?;
        let relation = required_string(object, "relation", &source_path)?;
        let json = item.to_json();
        projection.push(
            "object_references",
            vec![
                source.to_owned(),
                target.to_owned(),
                relation.to_owned(),
                source_path.clone(),
            ],
        );
        add_materialized(
            projection,
            "evidence-reference",
            &format!("{source}:{relation}:{target}"),
            &source_path,
            &json,
        );
    }
    Ok(())
}

fn project_status_transitions(
    root: &ObjectValue,
    projection: &mut Projection,
) -> Result<(), StorageError> {
    let transitions = required_array(root, "statusTransitions", "/statusTransitions")?;
    for (index, item) in transitions.iter().enumerate() {
        let source_path = format!("/statusTransitions/{index}");
        let object = item.as_object().ok_or_else(|| {
            StorageError::new(
                "FDIR-INDEX-STATUS-TYPE",
                &source_path,
                "status transition must be an object",
            )
        })?;
        let from = required_string(object, "from", &source_path)?;
        let to = required_string(object, "to", &source_path)?;
        let json = item.to_json();
        projection.push(
            "status_transitions",
            vec![from.to_owned(), to.to_owned(), source_path.clone()],
        );
        add_materialized(
            projection,
            "status-transition",
            &format!("{from}:{to}"),
            &source_path,
            &json,
        );
    }
    Ok(())
}

fn project_provenance(
    root: &ObjectValue,
    projection: &mut Projection,
) -> Result<(), StorageError> {
    let provenance = root.get("provenance").ok_or_else(|| {
        StorageError::new(
            "FDIR-INDEX-PROVENANCE-MISSING",
            "/provenance",
            "snapshot provenance is missing",
        )
    })?;
    let json = provenance.to_json();
    projection.push(
        "provenance_records",
        vec!["snapshot".to_owned(), "/provenance".to_owned(), json.clone()],
    );
    add_materialized(
        projection,
        "provenance",
        "snapshot",
        "/provenance",
        &json,
    );
    Ok(())
}

fn project_capabilities_and_profiles(
    root: &CanonicalValue,
    payload: &ObjectValue,
    projection: &mut Projection,
) -> Result<(), StorageError> {
    let mut capabilities = BTreeMap::new();
    let mut profiles = BTreeMap::new();
    collect_named_entities(
        root,
        "",
        "capabilities",
        &["capabilityId", "id"],
        &mut capabilities,
    )?;
    collect_named_entities(
        root,
        "",
        "profiles",
        &["profileId", "id"],
        &mut profiles,
    )?;

    if let Some(statuses) = payload.get("guaranteeStatuses").and_then(CanonicalValue::as_array) {
        for (index, status) in statuses.iter().enumerate() {
            let Some(object) = status.as_object() else {
                continue;
            };
            let Some(profile_id) = object.get("profileId").and_then(CanonicalValue::as_str) else {
                continue;
            };
            profiles.entry(profile_id.to_owned()).or_insert_with(|| {
                let source_path = format!("/payload/guaranteeStatuses/{index}/profileId");
                let json = string_object_json(&[("profileId", profile_id)]);
                (source_path, json)
            });
        }
    }

    for (identifier, (source_path, json)) in capabilities {
        projection.push(
            "capabilities",
            vec![identifier.clone(), source_path.clone(), json.clone()],
        );
        add_materialized(
            projection,
            "capability",
            &identifier,
            &source_path,
            &json,
        );
    }
    for (identifier, (source_path, json)) in profiles {
        projection.push(
            "profiles",
            vec![identifier.clone(), source_path.clone(), json.clone()],
        );
        add_materialized(
            projection,
            "profile",
            &identifier,
            &source_path,
            &json,
        );
    }
    Ok(())
}

fn collect_named_entities(
    value: &CanonicalValue,
    path: &str,
    field_name: &str,
    identity_fields: &[&str],
    output: &mut BTreeMap<String, (String, String)>,
) -> Result<(), StorageError> {
    match value {
        CanonicalValue::Object(object) => {
            for (key, child) in object {
                let child_path = pointer_child(path, key);
                if key == field_name {
                    let array = child.as_array().ok_or_else(|| {
                        StorageError::new(
                            "FDIR-INDEX-NAMED-ARRAY",
                            &child_path,
                            format!("{field_name} must be an array when present"),
                        )
                    })?;
                    for (index, item) in array.iter().enumerate() {
                        let item_path = format!("{child_path}/{index}");
                        let item_object = item.as_object().ok_or_else(|| {
                            StorageError::new(
                                "FDIR-INDEX-NAMED-ENTITY",
                                &item_path,
                                format!("{field_name} item must be an object"),
                            )
                        })?;
                        let identifier = identity_fields
                            .iter()
                            .find_map(|field| {
                                item_object.get(*field).and_then(CanonicalValue::as_str)
                            })
                            .ok_or_else(|| {
                                StorageError::new(
                                    "FDIR-INDEX-NAMED-IDENTITY",
                                    &item_path,
                                    format!(
                                        "{field_name} item requires one of {}",
                                        identity_fields.join(", ")
                                    ),
                                )
                            })?;
                        let json = item.to_json();
                        match output.get(identifier) {
                            Some((_, existing)) if existing != &json => {
                                return Err(StorageError::new(
                                    "FDIR-INDEX-NAMED-DUPLICATE",
                                    &item_path,
                                    format!(
                                        "{field_name} identifier {identifier} has conflicting definitions"
                                    ),
                                ));
                            }
                            Some(_) => {}
                            None => {
                                output.insert(identifier.to_owned(), (item_path, json));
                            }
                        }
                    }
                }
                collect_named_entities(
                    child,
                    &child_path,
                    field_name,
                    identity_fields,
                    output,
                )?;
            }
        }
        CanonicalValue::Array(values) => {
            for (index, child) in values.iter().enumerate() {
                collect_named_entities(
                    child,
                    &format!("{path}/{index}"),
                    field_name,
                    identity_fields,
                    output,
                )?;
            }
        }
        CanonicalValue::Null
        | CanonicalValue::Boolean(_)
        | CanonicalValue::Number(_)
        | CanonicalValue::String(_) => {}
    }
    Ok(())
}

fn project_outcomes(value: &CanonicalValue, path: &str, projection: &mut Projection) {
    match value {
        CanonicalValue::Object(object) => {
            for field in ["disposition", "state", "status"] {
                if let Some(state) = object.get(field).and_then(CanonicalValue::as_str)
                    && NON_COMPLETE_OUTCOMES.contains(&state)
                {
                    let source_path = pointer_child(path, field);
                    let json = value.to_json();
                    let outcome_key = format!("{field}:{state}");
                    projection.push(
                        "outcomes",
                        vec![
                            source_path.clone(),
                            outcome_key.clone(),
                            state.to_owned(),
                            json.clone(),
                        ],
                    );
                    add_materialized(
                        projection,
                        "outcome",
                        &outcome_key,
                        &source_path,
                        &json,
                    );
                }
            }
            for (key, child) in object {
                project_outcomes(child, &pointer_child(path, key), projection);
            }
        }
        CanonicalValue::Array(values) => {
            for (index, child) in values.iter().enumerate() {
                project_outcomes(child, &format!("{path}/{index}"), projection);
            }
        }
        CanonicalValue::Null
        | CanonicalValue::Boolean(_)
        | CanonicalValue::Number(_)
        | CanonicalValue::String(_) => {}
    }
}

fn walk_canonical_nodes(value: &CanonicalValue, path: &str, projection: &mut Projection) {
    projection.push(
        "canonical_nodes",
        vec![path.to_owned(), value_kind(value).to_owned(), value.to_json()],
    );
    match value {
        CanonicalValue::Object(object) => {
            for (key, child) in object {
                walk_canonical_nodes(child, &pointer_child(path, key), projection);
            }
        }
        CanonicalValue::Array(values) => {
            for (index, child) in values.iter().enumerate() {
                walk_canonical_nodes(child, &format!("{path}/{index}"), projection);
            }
        }
        CanonicalValue::Null
        | CanonicalValue::Boolean(_)
        | CanonicalValue::Number(_)
        | CanonicalValue::String(_) => {}
    }
}

const fn value_kind(value: &CanonicalValue) -> &'static str {
    match value {
        CanonicalValue::Null => "null",
        CanonicalValue::Boolean(_) => "boolean",
        CanonicalValue::Number(_) => "number",
        CanonicalValue::String(_) => "string",
        CanonicalValue::Array(_) => "array",
        CanonicalValue::Object(_) => "object",
    }
}

fn add_materialized(
    projection: &mut Projection,
    category: &str,
    key: &str,
    source_path: &str,
    json: &str,
) {
    projection.push(
        "materialized_records",
        vec![
            category.to_owned(),
            source_path.to_owned(),
            key.to_owned(),
            json.to_owned(),
        ],
    );
}

fn build_replacement(
    path: &Path,
    source: &ProjectionSource,
    mode: IndexBuildMode,
) -> Result<(), StorageError> {
    ensure_parent(path)?;
    let temporary = sibling_temporary_path(path, TEMPORARY_PREFIX);
    let result = (|| {
        let mut connection = open_writable_create(&temporary)?;
        connection.execute_batch(INDEX_DDL).map_err(|error| {
            sqlite_error(
                "FDIR-INDEX-DDL",
                &temporary,
                "generated DDL could not be applied",
                error,
            )
        })?;
        let transaction = connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| {
                sqlite_error(
                    "FDIR-INDEX-TRANSACTION",
                    &temporary,
                    "index build transaction could not begin",
                    error,
                )
            })?;
        write_new_metadata(&transaction, source, mode)?;
        insert_projection(&transaction, &source.projection, &temporary)?;
        transaction
            .execute(
                "UPDATE index_meta SET state = 'complete' WHERE singleton = ?1",
                [META_SINGLETON],
            )
            .map_err(|error| {
                sqlite_error(
                    "FDIR-INDEX-METADATA",
                    &temporary,
                    "index completion marker could not be written",
                    error,
                )
            })?;
        transaction.commit().map_err(|error| {
            sqlite_error(
                "FDIR-INDEX-COMMIT",
                &temporary,
                "index build transaction could not commit",
                error,
            )
        })?;
        drop(connection);
        sync_file(&temporary)?;
        replace_file(&temporary, path)
    })();
    if result.is_err() {
        let _ignored = fs::remove_file(&temporary);
    }
    result
}

fn update_incrementally(path: &Path, source: &ProjectionSource) -> Result<(), StorageError> {
    if !path.is_file() {
        return Err(StorageError::new(
            "FDIR-INDEX-MISSING",
            display_path(path),
            "incremental update requires an existing complete index",
        ));
    }
    let mut connection = open_read_write(path)?;
    validate_root(&connection, path, None)?;
    let actual = read_projection(&connection, path)?;
    let current_root = projection_from_index_root(&connection, path)?;
    let differences = projection_differences(&current_root, &actual);
    if !differences.is_empty() {
        return Err(consistency_error(path, &differences));
    }
    let metadata = read_metadata(&connection, path)?;
    let next_generation = metadata.generation.checked_add(1).ok_or_else(|| {
        StorageError::new(
            "FDIR-INDEX-GENERATION",
            display_path(path),
            "index generation overflowed",
        )
    })?;
    let transaction = connection
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| {
            sqlite_error(
                "FDIR-INDEX-TRANSACTION",
                path,
                "incremental index transaction could not begin",
                error,
            )
        })?;
    transaction
        .execute(
            "UPDATE index_meta SET state = 'building' WHERE singleton = ?1",
            [META_SINGLETON],
        )
        .map_err(|error| {
            sqlite_error(
                "FDIR-INDEX-METADATA",
                path,
                "incremental update marker could not be written",
                error,
            )
        })?;
    clear_projection(&transaction, path)?;
    insert_projection(&transaction, &source.projection, path)?;
    let ddl_digest = ddl_digest()?;
    let byte_length = i64::try_from(source.snapshot_byte_length).map_err(|error| {
        StorageError::new(
            "FDIR-INDEX-SNAPSHOT-LENGTH",
            display_path(path),
            format!("snapshot byte length is outside SQLite range: {error}"),
        )
    })?;
    let generation = i64::try_from(next_generation).map_err(|error| {
        StorageError::new(
            "FDIR-INDEX-GENERATION",
            display_path(path),
            format!("index generation is outside SQLite range: {error}"),
        )
    })?;
    transaction
        .execute(
            "UPDATE index_meta
             SET schemaVersion = ?1,
                 materializerVersion = ?2,
                 ddlDigest = ?3,
                 snapshotDigest = ?4,
                 snapshotByteLength = ?5,
                 buildMode = ?6,
                 generation = ?7,
                 state = 'complete'
             WHERE singleton = ?8",
            params![
                INDEX_SCHEMA_VERSION,
                INDEX_MATERIALIZER_VERSION,
                ddl_digest.as_str(),
                source.snapshot_digest.as_str(),
                byte_length,
                IndexBuildMode::Incremental.as_str(),
                generation,
                META_SINGLETON,
            ],
        )
        .map_err(|error| {
            sqlite_error(
                "FDIR-INDEX-METADATA",
                path,
                "incremental metadata could not be updated",
                error,
            )
        })?;
    transaction.commit().map_err(|error| {
        sqlite_error(
            "FDIR-INDEX-COMMIT",
            path,
            "incremental index transaction could not commit",
            error,
        )
    })?;
    drop(connection);
    sync_file(path)
}

fn write_new_metadata(
    transaction: &Transaction<'_>,
    source: &ProjectionSource,
    mode: IndexBuildMode,
) -> Result<(), StorageError> {
    let ddl_digest = ddl_digest()?;
    let byte_length = i64::try_from(source.snapshot_byte_length).map_err(|error| {
        StorageError::new(
            "FDIR-INDEX-SNAPSHOT-LENGTH",
            "$/indexMeta/snapshotByteLength",
            format!("snapshot byte length is outside SQLite range: {error}"),
        )
    })?;
    transaction
        .execute(
            "INSERT INTO index_meta(
                singleton,
                schemaVersion,
                materializerVersion,
                ddlDigest,
                snapshotDigest,
                snapshotByteLength,
                buildMode,
                generation,
                state
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 1, 'building')",
            params![
                META_SINGLETON,
                INDEX_SCHEMA_VERSION,
                INDEX_MATERIALIZER_VERSION,
                ddl_digest.as_str(),
                source.snapshot_digest.as_str(),
                byte_length,
                mode.as_str(),
            ],
        )
        .map_err(|error| {
            StorageError::new(
                "FDIR-INDEX-METADATA",
                "$/indexMeta",
                format!("index metadata could not be written: {error}"),
            )
        })?;
    Ok(())
}

fn insert_projection(
    transaction: &Transaction<'_>,
    projection: &Projection,
    path: &Path,
) -> Result<(), StorageError> {
    for spec in TABLE_SPECS {
        let mut statement = transaction.prepare(spec.insert_sql).map_err(|error| {
            sqlite_error(
                "FDIR-INDEX-PREPARE",
                path,
                &format!("{} insert could not be prepared", spec.name),
                error,
            )
        })?;
        for row in projection.rows(spec.name) {
            statement
                .execute(params_from_iter(row.iter().map(String::as_str)))
                .map_err(|error| {
                    sqlite_error(
                        "FDIR-INDEX-POPULATE",
                        path,
                        &format!("{} row could not be inserted", spec.name),
                        error,
                    )
                })?;
        }
    }
    Ok(())
}

fn clear_projection(transaction: &Transaction<'_>, path: &Path) -> Result<(), StorageError> {
    for spec in TABLE_SPECS.iter().rev() {
        transaction
            .execute(&format!("DELETE FROM {}", spec.name), [])
            .map_err(|error| {
                sqlite_error(
                    "FDIR-INDEX-CLEAR",
                    path,
                    &format!("{} could not be cleared", spec.name),
                    error,
                )
            })?;
    }
    Ok(())
}

fn validate_root(
    connection: &Connection,
    path: &Path,
    expected_snapshot: Option<&Digest>,
) -> Result<ValidatedRoot, StorageError> {
    validate_integrity(connection, path)?;
    let application_id = scalar_i64(connection, "SELECT application_id FROM pragma_application_id", path)?;
    if application_id != INDEX_APPLICATION_ID {
        return Err(StorageError::new(
            "FDIR-INDEX-APPLICATION-ID",
            display_path(path),
            format!(
                "SQLite application id {application_id} does not identify an FDIR index"
            ),
        ));
    }
    let user_version = scalar_i64(connection, "SELECT user_version FROM pragma_user_version", path)?;
    match negotiate_index_version(user_version) {
        IndexVersionDecision::Current => {}
        IndexVersionDecision::RebuildRequired { found, supported } => {
            return Err(StorageError::new(
                "FDIR-INDEX-SCHEMA-VERSION",
                display_path(path),
                format!(
                    "index schema {found} requires a full rebuild for supported schema {supported}"
                ),
            ));
        }
        IndexVersionDecision::UnsupportedFuture { found, supported } => {
            return Err(StorageError::new(
                "FDIR-INDEX-SCHEMA-VERSION",
                display_path(path),
                format!(
                    "index schema {found} is newer than supported schema {supported}"
                ),
            ));
        }
    }

    let metadata = read_metadata(connection, path)?;
    if let Some(expected) = expected_snapshot
        && &metadata.snapshot_digest != expected
    {
        return Err(StorageError::new(
            "FDIR-INDEX-SNAPSHOT-MISMATCH",
            display_path(path),
            format!(
                "index is bound to {}, but caller requires {expected}",
                metadata.snapshot_digest
            ),
        ));
    }
    let root_json = connection
        .query_row(
            "SELECT json FROM canonical_nodes WHERE path = ''",
            [],
            |row| row.get::<_, String>(0),
        )
        .map_err(|error| {
            sqlite_error(
                "FDIR-INDEX-ROOT",
                path,
                "canonical root row is missing or unreadable",
                error,
            )
        })?;
    let root_digest = raw_digest(root_json.as_bytes(), "$/indexRoot")?;
    if root_digest != metadata.snapshot_digest {
        return Err(StorageError::new(
            "FDIR-INDEX-SNAPSHOT-CONTENT",
            display_path(path),
            format!(
                "canonical root digest {root_digest} does not match metadata {}",
                metadata.snapshot_digest
            ),
        ));
    }
    let root_length = u64::try_from(root_json.len()).map_err(|error| {
        StorageError::new(
            "FDIR-INDEX-SNAPSHOT-LENGTH",
            display_path(path),
            format!("canonical root length cannot be represented as u64: {error}"),
        )
    })?;
    if root_length != metadata.snapshot_byte_length {
        return Err(StorageError::new(
            "FDIR-INDEX-SNAPSHOT-LENGTH",
            display_path(path),
            format!(
                "canonical root length {root_length} does not match metadata {}",
                metadata.snapshot_byte_length
            ),
        ));
    }
    let root = CanonicalValue::parse_json(&root_json).map_err(|error| {
        StorageError::new(
            "FDIR-INDEX-ROOT-JSON",
            display_path(path),
            format!("canonical root row is invalid JSON: {error}"),
        )
    })?;
    let recanonicalized = canonical_bytes(&root).map_err(|error| {
        StorageError::new(
            "FDIR-INDEX-ROOT-CANONICAL",
            display_path(path),
            format!("canonical root row could not be canonicalized: {error}"),
        )
    })?;
    if recanonicalized != root_json.as_bytes() {
        return Err(StorageError::new(
            "FDIR-INDEX-ROOT-CANONICAL",
            display_path(path),
            "canonical root row is not byte-canonical JSON",
        ));
    }
    let projection = Projection::from_root(&root)?;
    Ok(ValidatedRoot { projection })
}

fn validate_integrity(connection: &Connection, path: &Path) -> Result<(), StorageError> {
    let integrity = connection
        .query_row("PRAGMA integrity_check", [], |row| row.get::<_, String>(0))
        .map_err(|error| {
            sqlite_error(
                "FDIR-INDEX-CORRUPT",
                path,
                "SQLite integrity check could not run",
                error,
            )
        })?;
    if integrity != "ok" {
        return Err(StorageError::new(
            "FDIR-INDEX-CORRUPT",
            display_path(path),
            format!("SQLite integrity check failed: {integrity}"),
        ));
    }
    let mut statement = connection
        .prepare("PRAGMA foreign_key_check")
        .map_err(|error| {
            sqlite_error(
                "FDIR-INDEX-CORRUPT",
                path,
                "SQLite foreign-key check could not be prepared",
                error,
            )
        })?;
    let mut rows = statement.query([]).map_err(|error| {
        sqlite_error(
            "FDIR-INDEX-CORRUPT",
            path,
            "SQLite foreign-key check could not run",
            error,
        )
    })?;
    if rows.next().map_err(|error| {
        sqlite_error(
            "FDIR-INDEX-CORRUPT",
            path,
            "SQLite foreign-key check could not be read",
            error,
        )
    })?.is_some()
    {
        return Err(StorageError::new(
            "FDIR-INDEX-CORRUPT",
            display_path(path),
            "SQLite foreign-key check reported a violation",
        ));
    }
    Ok(())
}

fn read_metadata(connection: &Connection, path: &Path) -> Result<IndexMetadata, StorageError> {
    let row = connection
        .query_row(
            "SELECT
                schemaVersion,
                materializerVersion,
                ddlDigest,
                snapshotDigest,
                snapshotByteLength,
                generation,
                state
             FROM index_meta
             WHERE singleton = ?1",
            [META_SINGLETON],
            |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, i64>(4)?,
                    row.get::<_, i64>(5)?,
                    row.get::<_, String>(6)?,
                ))
            },
        )
        .map_err(|error| {
            sqlite_error(
                "FDIR-INDEX-METADATA",
                path,
                "index metadata is missing or unreadable",
                error,
            )
        })?;
    let (schema_version, materializer, stored_ddl, snapshot, byte_length, generation, state) = row;
    if schema_version != INDEX_SCHEMA_VERSION {
        return Err(StorageError::new(
            "FDIR-INDEX-SCHEMA-VERSION",
            display_path(path),
            format!(
                "metadata schema {schema_version} differs from supported schema {INDEX_SCHEMA_VERSION}"
            ),
        ));
    }
    if materializer != INDEX_MATERIALIZER_VERSION {
        return Err(StorageError::new(
            "FDIR-INDEX-MATERIALIZER-VERSION",
            display_path(path),
            format!(
                "materializer {materializer} differs from {INDEX_MATERIALIZER_VERSION}"
            ),
        ));
    }
    let expected_ddl = ddl_digest()?;
    if stored_ddl != expected_ddl.as_str() {
        return Err(StorageError::new(
            "FDIR-INDEX-DDL-MISMATCH",
            display_path(path),
            format!(
                "stored DDL digest {stored_ddl} differs from generated digest {expected_ddl}"
            ),
        ));
    }
    if state != "complete" {
        return Err(StorageError::new(
            "FDIR-INDEX-INCOMPLETE",
            display_path(path),
            format!("index state is {state}, not complete"),
        ));
    }
    let snapshot_digest = Digest::new(snapshot).map_err(|error| {
        StorageError::new(
            "FDIR-INDEX-SNAPSHOT-DIGEST",
            display_path(path),
            format!("stored snapshot digest is invalid: {error}"),
        )
    })?;
    let snapshot_byte_length = u64::try_from(byte_length).map_err(|error| {
        StorageError::new(
            "FDIR-INDEX-SNAPSHOT-LENGTH",
            display_path(path),
            format!("stored snapshot byte length is invalid: {error}"),
        )
    })?;
    let generation = u64::try_from(generation).map_err(|error| {
        StorageError::new(
            "FDIR-INDEX-GENERATION",
            display_path(path),
            format!("stored index generation is invalid: {error}"),
        )
    })?;
    if generation == 0 {
        return Err(StorageError::new(
            "FDIR-INDEX-GENERATION",
            display_path(path),
            "stored index generation must be positive",
        ));
    }
    Ok(IndexMetadata {
        snapshot_digest,
        snapshot_byte_length,
        generation,
    })
}

fn read_projection(connection: &Connection, path: &Path) -> Result<Projection, StorageError> {
    let mut projection = Projection::default();
    for spec in TABLE_SPECS {
        let mut statement = connection.prepare(spec.select_sql).map_err(|error| {
            sqlite_error(
                "FDIR-INDEX-READ",
                path,
                &format!("{} query could not be prepared", spec.name),
                error,
            )
        })?;
        let rows = statement
            .query_map([], |row| {
                let mut cells = Vec::with_capacity(spec.columns.len());
                for index in 0..spec.columns.len() {
                    cells.push(row.get::<_, String>(index)?);
                }
                Ok(cells)
            })
            .map_err(|error| {
                sqlite_error(
                    "FDIR-INDEX-READ",
                    path,
                    &format!("{} query could not run", spec.name),
                    error,
                )
            })?;
        for row in rows {
            projection.push(
                spec.name,
                row.map_err(|error| {
                    sqlite_error(
                        "FDIR-INDEX-READ",
                        path,
                        &format!("{} row could not be read", spec.name),
                        error,
                    )
                })?,
            );
        }
    }
    projection.normalize_and_validate()?;
    Ok(projection)
}

fn projection_from_index_root(
    connection: &Connection,
    path: &Path,
) -> Result<Projection, StorageError> {
    let root_json = connection
        .query_row(
            "SELECT json FROM canonical_nodes WHERE path = ''",
            [],
            |row| row.get::<_, String>(0),
        )
        .map_err(|error| {
            sqlite_error(
                "FDIR-INDEX-ROOT",
                path,
                "canonical root row is missing or unreadable",
                error,
            )
        })?;
    let root = CanonicalValue::parse_json(&root_json).map_err(|error| {
        StorageError::new(
            "FDIR-INDEX-ROOT-JSON",
            display_path(path),
            format!("canonical root row is invalid JSON: {error}"),
        )
    })?;
    Projection::from_root(&root)
}

fn projection_differences(
    expected: &Projection,
    actual: &Projection,
) -> Vec<IndexDifference> {
    let mut differences = Vec::new();
    for spec in TABLE_SPECS {
        let expected_rows = expected.rows(spec.name);
        let actual_rows = actual.rows(spec.name);
        if expected_rows != actual_rows {
            differences.push(IndexDifference {
                table: spec.name,
                expected_rows: expected_rows.len(),
                actual_rows: actual_rows.len(),
            });
        }
    }
    differences
}

fn consistency_error(path: &Path, differences: &[IndexDifference]) -> StorageError {
    let tables = differences
        .iter()
        .map(IndexDifference::table)
        .collect::<Vec<_>>()
        .join(", ");
    StorageError::new(
        "FDIR-INDEX-CONSISTENCY",
        display_path(path),
        format!("SQLite projection differs from canonical traversal in: {tables}"),
    )
}

fn required_object<'a>(
    object: &'a ObjectValue,
    field: &str,
    path: &str,
) -> Result<&'a ObjectValue, StorageError> {
    object
        .get(field)
        .and_then(CanonicalValue::as_object)
        .ok_or_else(|| {
            StorageError::new(
                "FDIR-INDEX-OBJECT",
                path,
                format!("{field} must be an object"),
            )
        })
}

fn required_array<'a>(
    object: &'a ObjectValue,
    field: &str,
    path: &str,
) -> Result<&'a [CanonicalValue], StorageError> {
    object
        .get(field)
        .and_then(CanonicalValue::as_array)
        .ok_or_else(|| {
            StorageError::new(
                "FDIR-INDEX-ARRAY",
                path,
                format!("{field} must be an array"),
            )
        })
}

fn required_string<'a>(
    object: &'a ObjectValue,
    field: &str,
    base_path: &str,
) -> Result<&'a str, StorageError> {
    object
        .get(field)
        .and_then(CanonicalValue::as_str)
        .ok_or_else(|| {
            StorageError::new(
                "FDIR-INDEX-STRING",
                format!("{base_path}/{}", pointer_escape(field)),
                format!("{field} must be a string"),
            )
        })
}

fn required_number_text<'a>(
    object: &'a ObjectValue,
    field: &str,
    base_path: &str,
) -> Result<&'a str, StorageError> {
    object
        .get(field)
        .and_then(CanonicalValue::as_number)
        .map(fdir_core::JsonNumber::as_str)
        .ok_or_else(|| {
            StorageError::new(
                "FDIR-INDEX-NUMBER",
                format!("{base_path}/{}", pointer_escape(field)),
                format!("{field} must be a number"),
            )
        })
}

fn string_object_json(fields: &[(&str, &str)]) -> String {
    let mut object = ObjectValue::new();
    for (key, value) in fields {
        object.insert(
            (*key).to_owned(),
            CanonicalValue::String((*value).to_owned()),
        );
    }
    CanonicalValue::Object(object).to_json()
}

fn pointer_child(path: &str, key: &str) -> String {
    format!("{path}/{}", pointer_escape(key))
}

fn pointer_escape(value: &str) -> String {
    value.replace('~', "~0").replace('/', "~1")
}

fn ddl_digest() -> Result<Digest, StorageError> {
    raw_digest(INDEX_DDL.as_bytes(), "$/generatedDdl")
}

fn raw_digest(bytes: &[u8], path: &str) -> Result<Digest, StorageError> {
    raw_content_digest(bytes).map_err(|error| {
        StorageError::new(
            "FDIR-INDEX-DIGEST",
            path,
            format!("content digest could not be computed: {error}"),
        )
    })
}

fn scalar_i64(connection: &Connection, sql: &str, path: &Path) -> Result<i64, StorageError> {
    connection
        .query_row(sql, [], |row| row.get::<_, i64>(0))
        .map_err(|error| {
            sqlite_error(
                "FDIR-INDEX-CORRUPT",
                path,
                "SQLite metadata pragma could not be read",
                error,
            )
        })
}

fn open_read_only(path: &Path) -> Result<Connection, StorageError> {
    if !path.is_file() {
        return Err(StorageError::new(
            "FDIR-INDEX-MISSING",
            display_path(path),
            "SQLite materialized index is missing",
        ));
    }
    Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|error| {
        sqlite_error(
            "FDIR-INDEX-CORRUPT",
            path,
            "SQLite materialized index could not be opened read-only",
            error,
        )
    })
}

fn open_read_write(path: &Path) -> Result<Connection, StorageError> {
    Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|error| {
        sqlite_error(
            "FDIR-INDEX-CORRUPT",
            path,
            "SQLite materialized index could not be opened for update",
            error,
        )
    })
}

fn open_writable_create(path: &Path) -> Result<Connection, StorageError> {
    Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_WRITE
            | OpenFlags::SQLITE_OPEN_CREATE
            | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|error| {
        sqlite_error(
            "FDIR-INDEX-CREATE",
            path,
            "temporary SQLite index could not be created",
            error,
        )
    })
}

fn ensure_parent(path: &Path) -> Result<(), StorageError> {
    let Some(parent) = path.parent() else {
        return Ok(());
    };
    if parent.as_os_str().is_empty() {
        return Ok(());
    }
    fs::create_dir_all(parent).map_err(|error| {
        io_error(
            "FDIR-INDEX-DIRECTORY",
            parent,
            "index parent directory could not be created",
            error,
        )
    })
}

fn sibling_temporary_path(path: &Path, prefix: &str) -> PathBuf {
    let counter = TEMPORARY_COUNTER.fetch_add(1, Ordering::Relaxed);
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    let file_name = path
        .file_name()
        .map_or_else(|| "index.sqlite".into(), |value| value.to_string_lossy());
    parent.join(format!(
        "{prefix}{}-{}-{counter}",
        std::process::id(),
        file_name
    ))
}

fn replace_file(temporary: &Path, target: &Path) -> Result<(), StorageError> {
    if !target.exists() {
        return fs::rename(temporary, target).map_err(|error| {
            io_error(
                "FDIR-INDEX-PUBLISH",
                target,
                "completed index could not be published",
                error,
            )
        });
    }
    let backup = sibling_temporary_path(target, BACKUP_PREFIX);
    fs::rename(target, &backup).map_err(|error| {
        io_error(
            "FDIR-INDEX-PUBLISH",
            target,
            "existing index could not be moved aside",
            error,
        )
    })?;
    if let Err(publish_error) = fs::rename(temporary, target) {
        if let Err(restore_error) = fs::rename(&backup, target) {
            return Err(StorageError::new(
                "FDIR-INDEX-PUBLISH-RECOVERY",
                display_path(target),
                format!(
                    "new index publication failed ({publish_error}); previous index restoration also failed ({restore_error})"
                ),
            ));
        }
        return Err(io_error(
            "FDIR-INDEX-PUBLISH",
            target,
            "completed index could not replace the previous index",
            publish_error,
        ));
    }
    fs::remove_file(&backup).map_err(|error| {
        io_error(
            "FDIR-INDEX-PUBLISH-CLEANUP",
            &backup,
            "published index backup could not be removed",
            error,
        )
    })
}

fn sync_file(path: &Path) -> Result<(), StorageError> {
    File::open(path)
        .and_then(|file| file.sync_all())
        .map_err(|error| {
            io_error(
                "FDIR-INDEX-SYNC",
                path,
                "completed index could not be synchronized",
                error,
            )
        })
}

fn sqlite_error(
    code: &'static str,
    path: &Path,
    context: &str,
    error: rusqlite::Error,
) -> StorageError {
    StorageError::new(
        code,
        display_path(path),
        format!("{context}: {error}"),
    )
}

fn io_error(
    code: &'static str,
    path: &Path,
    context: &str,
    error: std::io::Error,
) -> StorageError {
    StorageError::new(
        code,
        display_path(path),
        format!("{context}: {error}"),
    )
}

fn display_path(path: &Path) -> String {
    path.to_string_lossy().into_owned()
}

#[cfg(test)]
pub(crate) fn overwrite_user_version(path: &Path, version: i64) -> Result<(), StorageError> {
    let connection = open_read_write(path)?;
    connection
        .pragma_update(None, "user_version", version)
        .map_err(|error| {
            sqlite_error(
                "FDIR-INDEX-TEST-MUTATION",
                path,
                "test user_version mutation failed",
                error,
            )
        })
}

#[cfg(test)]
pub(crate) fn overwrite_assertion_json(
    path: &Path,
    assertion_id: &str,
    json: &str,
) -> Result<(), StorageError> {
    let connection = open_read_write(path)?;
    connection
        .execute(
            "UPDATE assertions SET json = ?1 WHERE assertionId = ?2",
            params![json, assertion_id],
        )
        .map_err(|error| {
            sqlite_error(
                "FDIR-INDEX-TEST-MUTATION",
                path,
                "test assertion mutation failed",
                error,
            )
        })?;
    Ok(())
}
