#![forbid(unsafe_code)]

use std::error::Error;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use fdir_canonical::{CANONICAL_JSON_VERSION, raw_content_digest};
use fdir_core::{CanonicalValue, ObjectValue, ResultState};

use super::{
    GarbageCollectionMode, ObjectDescriptor, ObjectReference, ReferenceSource, SNAPSHOT_SCHEMA,
    SNAPSHOT_VERSION, SnapshotManifest, SnapshotStore, StatusTransition, StorageError,
    WriteDisposition, negotiate_snapshot_version, parse_snapshot_bytes,
};

static TEST_COUNTER: AtomicU64 = AtomicU64::new(0);

struct TestDirectory {
    path: PathBuf,
}

impl TestDirectory {
    fn new(label: &str) -> Result<Self, io::Error> {
        let counter = TEST_COUNTER.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!(
            "fdir-storage-{label}-{}-{counter}",
            std::process::id()
        ));
        match fs::remove_dir_all(&path) {
            Ok(()) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
        fs::create_dir_all(&path)?;
        Ok(Self { path })
    }

    fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for TestDirectory {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}

fn parsed_json(input: &str) -> Result<CanonicalValue, Box<dyn Error>> {
    Ok(CanonicalValue::parse_json(input)?)
}

fn example_manifest(
    objects: Vec<ObjectDescriptor>,
    references: Vec<ObjectReference>,
) -> Result<SnapshotManifest, Box<dyn Error>> {
    let payload = parsed_json(
        r#"{"documents":[{"id":"document-1","state":"partial"}],"operation":{"state":"failed"}}"#,
    )?;
    let provenance = parsed_json(
        r#"{"producer":"fdir-storage-tests","source":"fixture","version":"1"}"#,
    )?;
    let transitions = vec![
        StatusTransition::new(ResultState::Incomplete, ResultState::Partial)?,
        StatusTransition::new(ResultState::Partial, ResultState::Complete)?,
    ];
    Ok(SnapshotManifest::new(
        payload,
        provenance,
        objects,
        references,
        transitions,
        ObjectValue::new(),
    ))
}

fn assert_error_code<T>(result: Result<T, StorageError>, expected: &str) {
    match result {
        Err(error) => assert_eq!(error.code(), expected),
        Ok(_) => panic!("expected storage failure {expected}"),
    }
}

#[test]
fn canonical_snapshot_round_trip_is_byte_identical_and_deduplicated() -> Result<(), Box<dyn Error>> {
    let directory = TestDirectory::new("round-trip")?;
    let store = SnapshotStore::open(directory.path().join("store"))?;
    let transaction = store.begin_write()?;
    let stored = transaction.put_object(
        b"native evidence bytes",
        "application/octet-stream",
        "native-evidence",
    )?;
    assert_eq!(stored.disposition(), WriteDisposition::Created);
    let descriptor = stored.into_descriptor();
    let manifest = example_manifest(
        vec![descriptor.clone()],
        vec![ObjectReference::new(
            ReferenceSource::Snapshot,
            descriptor.digest().clone(),
            "evidence",
        )?],
    )?;
    let expected_bytes = manifest.canonical_bytes()?;
    let receipt = transaction.write_snapshot(&manifest)?;
    drop(transaction);

    let loaded = store.read_snapshot(receipt.digest())?;
    assert_eq!(loaded, manifest);
    assert_eq!(loaded.canonical_bytes()?, expected_bytes);
    assert_eq!(store.read_object(&descriptor)?, b"native evidence bytes");
    let duplicate = store.write_snapshot(&loaded)?;
    assert_eq!(duplicate.digest(), receipt.digest());
    assert_eq!(duplicate.disposition(), WriteDisposition::Deduplicated);
    assert!(loaded.payload().to_json().contains("\"partial\""));
    assert!(loaded.payload().to_json().contains("\"failed\""));
    Ok(())
}

#[test]
fn missing_and_corrupt_objects_fail_with_stable_diagnostics() -> Result<(), Box<dyn Error>> {
    let directory = TestDirectory::new("corruption")?;
    let store = SnapshotStore::open(directory.path().join("store"))?;
    let stored = store.put_object(b"evidence", "application/octet-stream", "evidence")?;
    let descriptor = stored.into_descriptor();
    let manifest = example_manifest(
        vec![descriptor.clone()],
        vec![ObjectReference::new(
            ReferenceSource::Snapshot,
            descriptor.digest().clone(),
            "evidence",
        )?],
    )?;
    let object_path = store.object_path(descriptor.digest())?;
    fs::remove_file(&object_path)?;
    assert_error_code(store.write_snapshot(&manifest), "FDIR-OBJECT-MISSING");

    let replacement = store.put_object(b"evidence", "application/octet-stream", "evidence")?;
    assert_eq!(replacement.descriptor(), &descriptor);
    fs::write(&object_path, b"corrupt")?;
    assert_error_code(
        store.read_object(&descriptor),
        "FDIR-OBJECT-DIGEST-MISMATCH",
    );
    assert_error_code(
        store.put_object(b"evidence", "application/octet-stream", "evidence"),
        "FDIR-STORAGE-EXISTING-CORRUPT",
    );
    Ok(())
}

#[test]
fn cycles_unreachable_objects_and_invalid_status_transitions_are_rejected(
) -> Result<(), Box<dyn Error>> {
    let directory = TestDirectory::new("semantic-errors")?;
    let store = SnapshotStore::open(directory.path().join("store"))?;
    let first = store
        .put_object(b"first", "application/octet-stream", "evidence")?
        .into_descriptor();
    let second = store
        .put_object(b"second", "application/octet-stream", "evidence")?
        .into_descriptor();
    let cyclic = example_manifest(
        vec![first.clone(), second.clone()],
        vec![
            ObjectReference::new(
                ReferenceSource::Snapshot,
                first.digest().clone(),
                "root",
            )?,
            ObjectReference::new(
                ReferenceSource::Object(first.digest().clone()),
                second.digest().clone(),
                "depends-on",
            )?,
            ObjectReference::new(
                ReferenceSource::Object(second.digest().clone()),
                first.digest().clone(),
                "depends-on",
            )?,
        ],
    )?;
    assert!(cyclic
        .validation_report()
        .diagnostics()
        .iter()
        .any(|diagnostic| diagnostic.code() == "FDIR-SNAPSHOT-REFERENCE-CYCLE"));
    assert_error_code(
        store.write_snapshot(&cyclic),
        "FDIR-SNAPSHOT-REFERENCE-CYCLE",
    );

    let unreachable = example_manifest(vec![first], Vec::new())?;
    assert_error_code(
        store.write_snapshot(&unreachable),
        "FDIR-SNAPSHOT-OBJECT-UNREFERENCED",
    );
    assert_error_code(
        StatusTransition::new(ResultState::Complete, ResultState::Partial),
        "FDIR-SNAPSHOT-STATUS-TRANSITION",
    );
    Ok(())
}

#[test]
fn reader_rejects_deprecated_future_and_incompatible_versions() -> Result<(), Box<dyn Error>> {
    let manifest = example_manifest(Vec::new(), Vec::new())?;
    let current = String::from_utf8(manifest.canonical_bytes()?)?;
    let deprecated = current.replace("\"version\":1", "\"version\":0");
    let future = current.replace("\"version\":1", "\"version\":2");
    let incompatible_schema = current.replace(SNAPSHOT_SCHEMA, "fdir/snapshot/2");
    let incompatible_canonical = current.replace(CANONICAL_JSON_VERSION, "fdir-canonical-json/2");

    assert_error_code(
        parse_snapshot_bytes(deprecated.as_bytes()),
        "FDIR-SNAPSHOT-VERSION-DEPRECATED",
    );
    assert_error_code(
        parse_snapshot_bytes(future.as_bytes()),
        "FDIR-SNAPSHOT-VERSION-FUTURE",
    );
    assert_error_code(
        parse_snapshot_bytes(incompatible_schema.as_bytes()),
        "FDIR-SNAPSHOT-SCHEMA-INCOMPATIBLE",
    );
    assert_error_code(
        parse_snapshot_bytes(incompatible_canonical.as_bytes()),
        "FDIR-SNAPSHOT-CANONICAL-INCOMPATIBLE",
    );
    assert_eq!(
        negotiate_snapshot_version(SNAPSHOT_SCHEMA, SNAPSHOT_VERSION, CANONICAL_JSON_VERSION),
        super::VersionDecision::Supported
    );
    Ok(())
}

#[test]
fn interrupted_temporary_state_is_never_accepted_and_requires_explicit_recovery(
) -> Result<(), Box<dyn Error>> {
    let directory = TestDirectory::new("recovery")?;
    let store = SnapshotStore::open(directory.path().join("store"))?;
    let manifest = example_manifest(Vec::new(), Vec::new())?;
    let bytes = manifest.canonical_bytes()?;
    let digest = raw_content_digest(&bytes)?;
    let snapshot_path = store.snapshot_path(&digest)?;
    let parent = snapshot_path
        .parent()
        .ok_or_else(|| io::Error::other("snapshot path has no parent"))?;
    fs::create_dir_all(parent)?;
    let temporary = parent.join(".fdir-tmp-interrupted-snapshot");
    fs::write(&temporary, &bytes)?;
    fs::create_dir(store.root().join(".fdir-mutation-lock"))?;

    assert_error_code(store.read_snapshot(&digest), "FDIR-SNAPSHOT-MISSING");
    assert_error_code(
        store.begin_write(),
        "FDIR-STORAGE-LOCKED",
    );
    let recovery = store.recover_interrupted_state()?;
    assert!(recovery.stale_lock_removed);
    assert_eq!(recovery.temporary_paths_removed, 1);
    assert!(!temporary.try_exists()?);
    assert!(store.begin_write().is_ok());
    Ok(())
}

#[test]
fn portable_export_cleanly_reimports_with_the_same_identity() -> Result<(), Box<dyn Error>> {
    let directory = TestDirectory::new("export-import")?;
    let source = SnapshotStore::open(directory.path().join("source"))?;
    let transaction = source.begin_write()?;
    let descriptor = transaction
        .put_object(b"portable evidence", "application/octet-stream", "evidence")?
        .into_descriptor();
    let manifest = example_manifest(
        vec![descriptor.clone()],
        vec![ObjectReference::new(
            ReferenceSource::Snapshot,
            descriptor.digest().clone(),
            "evidence",
        )?],
    )?;
    let receipt = transaction.write_snapshot(&manifest)?;
    drop(transaction);

    let export = directory.path().join("portable-export");
    source.export_snapshot(receipt.digest(), &export)?;
    let destination = SnapshotStore::open(directory.path().join("destination"))?;
    let imported = destination.import_snapshot(&export)?;
    assert_eq!(imported.digest(), receipt.digest());
    assert_eq!(destination.read_snapshot(imported.digest())?, manifest);
    assert_eq!(destination.read_object(&descriptor)?, b"portable evidence");
    Ok(())
}

#[test]
fn garbage_collection_retains_all_snapshot_references_and_deletes_only_unreachable_objects(
) -> Result<(), Box<dyn Error>> {
    let directory = TestDirectory::new("garbage-collection")?;
    let store = SnapshotStore::open(directory.path().join("store"))?;
    let transaction = store.begin_write()?;
    let retained = transaction
        .put_object(b"retained", "application/octet-stream", "evidence")?
        .into_descriptor();
    let orphan = transaction
        .put_object(b"orphan", "application/octet-stream", "scratch")?
        .into_descriptor();
    let manifest = example_manifest(
        vec![retained.clone()],
        vec![ObjectReference::new(
            ReferenceSource::Snapshot,
            retained.digest().clone(),
            "evidence",
        )?],
    )?;
    let snapshot = transaction.write_snapshot(&manifest)?;
    drop(transaction);

    let plan = store.garbage_collect(GarbageCollectionMode::ReportOnly)?;
    assert_eq!(plan.snapshots_scanned, 1);
    assert_eq!(plan.reachable, vec![retained.digest().clone()]);
    assert_eq!(plan.unreachable, vec![orphan.digest().clone()]);
    assert!(plan.deleted.is_empty());

    let deletion = store.garbage_collect(GarbageCollectionMode::DeleteUnreachable)?;
    assert_eq!(deletion.deleted, vec![orphan.digest().clone()]);
    assert_eq!(store.read_object(&retained)?, b"retained");
    assert_error_code(
        store.read_object(&orphan),
        "FDIR-OBJECT-MISSING",
    );

    assert!(store.delete_snapshot(snapshot.digest())?);
    let final_deletion = store.garbage_collect(GarbageCollectionMode::DeleteUnreachable)?;
    assert_eq!(final_deletion.deleted, vec![retained.digest().clone()]);
    assert!(store.list_snapshots()?.is_empty());
    Ok(())
}

#[test]
fn raw_reader_rejects_noncanonical_bytes_and_snapshot_digest_mismatch(
) -> Result<(), Box<dyn Error>> {
    let directory = TestDirectory::new("snapshot-corruption")?;
    let store = SnapshotStore::open(directory.path().join("store"))?;
    let manifest = example_manifest(Vec::new(), Vec::new())?;
    let receipt = store.write_snapshot(&manifest)?;
    let path = store.snapshot_path(receipt.digest())?;
    let canonical = fs::read(&path)?;
    let mut noncanonical = canonical.clone();
    noncanonical.push(b'\n');
    assert_error_code(
        parse_snapshot_bytes(&noncanonical),
        "FDIR-SNAPSHOT-NONCANONICAL",
    );
    fs::write(&path, b"{}")?;
    assert_error_code(
        store.read_snapshot(receipt.digest()),
        "FDIR-SNAPSHOT-DIGEST-MISMATCH",
    );
    Ok(())
}
