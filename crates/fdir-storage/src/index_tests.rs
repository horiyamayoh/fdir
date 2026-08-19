#![forbid(unsafe_code)]

use std::error::Error;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use fdir_core::{CanonicalValue, Digest, ObjectValue, ResultState};

use super::{
    INDEX_CAPABILITY, IndexBuildMode, IndexQuery, ObjectDescriptor, ObjectReference,
    ReferenceSource, SnapshotManifest, SqliteIndex, StatusTransition, StorageError,
};
use crate::index::{overwrite_assertion_json, overwrite_user_version};

static TEST_COUNTER: AtomicU64 = AtomicU64::new(0);

struct TestDirectory {
    path: PathBuf,
}

impl TestDirectory {
    fn new(label: &str) -> Result<Self, std::io::Error> {
        let counter = TEST_COUNTER.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!(
            "fdir-sqlite-index-{label}-{}-{counter}",
            std::process::id()
        ));
        fs::create_dir_all(&path)?;
        Ok(Self { path })
    }

    fn join(&self, name: &str) -> PathBuf {
        self.path.join(name)
    }
}

impl Drop for TestDirectory {
    fn drop(&mut self) {
        let _ignored = fs::remove_dir_all(&self.path);
    }
}

fn rich_manifest() -> Result<SnapshotManifest, Box<dyn Error>> {
    let mut payload = CanonicalValue::parse_json(include_str!(
        "../../../fixtures/positive/accounted-document.json"
    ))?;
    let object = match &mut payload {
        CanonicalValue::Object(value) => value,
        _ => {
            return Err(Box::new(StorageError::new(
                "FDIR-INDEX-TEST-FIXTURE",
                "$/payload",
                "accounted-document fixture must be an object",
            )));
        }
    };
    object.insert(
        "relations".to_owned(),
        CanonicalValue::parse_json(
            r#"[
                {
                    "relationId":"relation-1",
                    "relationType":"follows",
                    "sourceUnitId":"unit-1",
                    "targetUnitId":"unit-1"
                }
            ]"#,
        )?,
    );

    let provenance = CanonicalValue::parse_json(
        r#"{
            "adapter":{"id":"test-adapter","version":"1"},
            "capabilities":[
                {"capabilityId":"recorded-information","state":"implemented-unqualified"}
            ],
            "profiles":[
                {"profileId":"recorded-information-core","state":"development-unqualified"}
            ]
        }"#,
    )?;
    let digest = Digest::new(
        "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )?;
    let descriptor = ObjectDescriptor::new(
        digest.clone(),
        4,
        "application/octet-stream",
        "source-evidence",
    )?;
    let reference = ObjectReference::new(
        ReferenceSource::Snapshot,
        digest,
        "source-evidence",
    )?;
    let transition = StatusTransition::new(ResultState::Incomplete, ResultState::Unsupported)?;
    Ok(SnapshotManifest::new(
        payload,
        provenance,
        vec![descriptor],
        vec![reference],
        vec![transition],
        ObjectValue::new(),
    ))
}

fn minimal_manifest() -> Result<SnapshotManifest, Box<dyn Error>> {
    Ok(SnapshotManifest::new(
        CanonicalValue::parse_json(r#"{"units":[],"assertions":[]}"#)?,
        CanonicalValue::parse_json(r#"{"source":"minimal-test"}"#)?,
        Vec::new(),
        Vec::new(),
        Vec::new(),
        ObjectValue::new(),
    ))
}

fn require_error<T>(result: Result<T, StorageError>) -> Result<StorageError, Box<dyn Error>> {
    match result {
        Ok(_) => Err(Box::new(StorageError::new(
            "FDIR-INDEX-TEST-EXPECTED-ERROR",
            "$",
            "operation unexpectedly succeeded",
        ))),
        Err(error) => Ok(error),
    }
}

fn copy_index(source: &Path, target: &Path) -> Result<(), std::io::Error> {
    fs::copy(source, target).map(|_| ())
}

#[test]
fn capability_is_implemented_but_not_production_qualified() {
    const {
        assert!(INDEX_CAPABILITY.available);
        assert!(!INDEX_CAPABILITY.production_ready);
        assert!(INDEX_CAPABILITY.owning_issue == 11);
    }
}

#[test]
fn clean_full_incremental_and_deleted_rebuilds_are_equivalent() -> Result<(), Box<dyn Error>> {
    let directory = TestDirectory::new("rebuild-parity")?;
    let manifest = rich_manifest()?;
    let clean_path = directory.join("clean.sqlite");
    let full_path = directory.join("full.sqlite");
    let incremental_path = directory.join("incremental.sqlite");

    let clean = SqliteIndex::build(&clean_path, &manifest, IndexBuildMode::Clean)?;
    let full = SqliteIndex::build(&full_path, &manifest, IndexBuildMode::Full)?;
    SqliteIndex::build(
        &incremental_path,
        &minimal_manifest()?,
        IndexBuildMode::Clean,
    )?;
    let incremental =
        SqliteIndex::build(&incremental_path, &manifest, IndexBuildMode::Incremental)?;

    assert_eq!(clean.snapshot_digest(), full.snapshot_digest());
    assert_eq!(clean.snapshot_digest(), incremental.snapshot_digest());
    assert_eq!(clean.content_digest(), full.content_digest());
    assert_eq!(clean.content_digest(), incremental.content_digest());
    assert_eq!(clean.row_count(), full.row_count());
    assert_eq!(clean.row_count(), incremental.row_count());
    assert_eq!(clean.generation(), 1);
    assert_eq!(full.generation(), 1);
    assert_eq!(incremental.generation(), 2);

    let clean_index = SqliteIndex::open(&clean_path, clean.snapshot_digest())?;
    let full_index = SqliteIndex::open(&full_path, full.snapshot_digest())?;
    let incremental_index =
        SqliteIndex::open(&incremental_path, incremental.snapshot_digest())?;
    assert_eq!(clean_index.canonical_dump()?, full_index.canonical_dump()?);
    assert_eq!(
        clean_index.canonical_dump()?,
        incremental_index.canonical_dump()?
    );
    for query in IndexQuery::ALL {
        assert_eq!(clean_index.query(query)?, full_index.query(query)?);
        assert_eq!(clean_index.query(query)?, incremental_index.query(query)?);
    }

    fs::remove_file(&clean_path)?;
    let rebuilt = SqliteIndex::build(&clean_path, &manifest, IndexBuildMode::Full)?;
    assert_eq!(rebuilt.content_digest(), clean.content_digest());
    assert_eq!(rebuilt.row_count(), clean.row_count());
    Ok(())
}

#[test]
fn stale_corrupt_wrong_version_and_logically_divergent_indexes_are_rejected(
) -> Result<(), Box<dyn Error>> {
    let directory = TestDirectory::new("invalid-indexes")?;
    let manifest = rich_manifest()?;
    let path = directory.join("valid.sqlite");
    let receipt = SqliteIndex::build(&path, &manifest, IndexBuildMode::Clean)?;
    let valid_index = SqliteIndex::open(&path, receipt.snapshot_digest())?;

    let other_path = directory.join("other.sqlite");
    let other = SqliteIndex::build(
        &other_path,
        &minimal_manifest()?,
        IndexBuildMode::Clean,
    )?;
    let stale = require_error(SqliteIndex::open(&path, other.snapshot_digest()))?;
    assert_eq!(stale.code(), "FDIR-INDEX-SNAPSHOT-MISMATCH");

    let corrupt_path = directory.join("corrupt.sqlite");
    fs::write(&corrupt_path, b"not a SQLite database")?;
    let corrupt = require_error(SqliteIndex::open(&corrupt_path, receipt.snapshot_digest()))?;
    assert_eq!(corrupt.code(), "FDIR-INDEX-CORRUPT");

    let wrong_version_path = directory.join("wrong-version.sqlite");
    copy_index(&path, &wrong_version_path)?;
    overwrite_user_version(&wrong_version_path, 99)?;
    let wrong_version = require_error(SqliteIndex::open(
        &wrong_version_path,
        receipt.snapshot_digest(),
    ))?;
    assert_eq!(wrong_version.code(), "FDIR-INDEX-SCHEMA-VERSION");

    overwrite_assertion_json(
        &path,
        "assertion-kind",
        r#"{"assertionId":"assertion-kind","tampered":true}"#,
    )?;
    let report = valid_index.consistency_report(&manifest)?;
    assert!(!report.is_consistent());
    assert!(
        report
            .differences()
            .iter()
            .any(|difference| difference.table() == "assertions")
    );
    let divergent = require_error(SqliteIndex::open(&path, receipt.snapshot_digest()))?;
    assert_eq!(divergent.code(), "FDIR-INDEX-CONSISTENCY");
    Ok(())
}

#[test]
fn supported_queries_match_canonical_traversal_for_all_required_domains(
) -> Result<(), Box<dyn Error>> {
    let directory = TestDirectory::new("query-consistency")?;
    let manifest = rich_manifest()?;
    let path = directory.join("index.sqlite");
    let receipt = SqliteIndex::build(&path, &manifest, IndexBuildMode::Clean)?;
    let index = SqliteIndex::open(&path, receipt.snapshot_digest())?;

    for query in IndexQuery::ALL {
        let indexed = index.query(query)?;
        let canonical = SqliteIndex::query_snapshot(&manifest, query)?;
        assert_eq!(indexed, canonical, "{} query differs", query.as_str());
        assert!(!indexed.is_empty(), "{} query is empty", query.as_str());
    }

    let table_report = index.consistency_report(&manifest)?;
    assert!(table_report.is_consistent());
    assert_eq!(
        table_report.expected_content_digest(),
        table_report.actual_content_digest()
    );
    let query_report = index.query_consistency_report(&manifest)?;
    assert!(query_report.is_consistent());
    assert!(query_report.differences().is_empty());

    let dump = index.canonical_dump()?;
    let dump_text = std::str::from_utf8(&dump)?;
    let dump_value = CanonicalValue::parse_json(dump_text)?;
    assert!(dump_value.as_object().is_some());
    Ok(())
}

#[test]
fn explicit_invalidation_removes_only_the_rebuildable_index() -> Result<(), Box<dyn Error>> {
    let directory = TestDirectory::new("invalidation")?;
    let manifest = rich_manifest()?;
    let path = directory.join("index.sqlite");
    let receipt = SqliteIndex::build(&path, &manifest, IndexBuildMode::Clean)?;
    assert!(SqliteIndex::invalidate(&path)?);
    assert!(!SqliteIndex::invalidate(&path)?);
    let missing = require_error(SqliteIndex::open(&path, receipt.snapshot_digest()))?;
    assert_eq!(missing.code(), "FDIR-INDEX-MISSING");

    let rebuilt = SqliteIndex::build(&path, &manifest, IndexBuildMode::Clean)?;
    assert_eq!(rebuilt.content_digest(), receipt.content_digest());
    Ok(())
}
