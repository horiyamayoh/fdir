#![forbid(unsafe_code)]

use std::collections::BTreeSet;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use fdir_canonical::{canonical_bytes, raw_content_digest};
use fdir_core::{CanonicalValue, Digest, ObjectValue};

use crate::diagnostic::{StorageDiagnostic, StorageError, ValidationReport};
use crate::manifest::{ObjectDescriptor, SnapshotManifest, parse_snapshot_bytes};
use crate::version::SNAPSHOT_EXPORT_SCHEMA;

const OBJECTS_DIRECTORY: &str = "objects";
const SNAPSHOTS_DIRECTORY: &str = "snapshots";
const HASH_ALGORITHM_DIRECTORY: &str = "sha256";
const MUTATION_LOCK_DIRECTORY: &str = ".fdir-mutation-lock";
const TEMPORARY_PREFIX: &str = ".fdir-tmp-";
const EXPORT_SNAPSHOT_FILE: &str = "snapshot.json";
const EXPORT_COMPLETE_FILE: &str = "complete.json";
const TEMPORARY_ATTEMPTS: u64 = 128;

static TEMPORARY_COUNTER: AtomicU64 = AtomicU64::new(0);

/// Whether a content-addressed write created new durable bytes or reused identical bytes.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum WriteDisposition {
    Created,
    Deduplicated,
}

/// Result of storing one evidence or auxiliary object.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StoredObject {
    descriptor: ObjectDescriptor,
    disposition: WriteDisposition,
}

impl StoredObject {
    /// Borrow the descriptor that belongs in the snapshot authority.
    #[must_use]
    pub const fn descriptor(&self) -> &ObjectDescriptor {
        &self.descriptor
    }

    /// Whether bytes were newly published or deduplicated.
    #[must_use]
    pub const fn disposition(&self) -> WriteDisposition {
        self.disposition
    }

    /// Consume the receipt and return the snapshot descriptor.
    #[must_use]
    pub fn into_descriptor(self) -> ObjectDescriptor {
        self.descriptor
    }
}

/// Result of atomically publishing one canonical snapshot.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SnapshotReceipt {
    digest: Digest,
    disposition: WriteDisposition,
}

impl SnapshotReceipt {
    /// Borrow the content identity used to read or export the snapshot.
    #[must_use]
    pub const fn digest(&self) -> &Digest {
        &self.digest
    }

    /// Whether bytes were newly published or deduplicated.
    #[must_use]
    pub const fn disposition(&self) -> WriteDisposition {
        self.disposition
    }
}

/// Garbage-collection operation selected by the caller.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum GarbageCollectionMode {
    /// Report unreachable objects without deleting bytes.
    ReportOnly,
    /// Delete objects that are unreachable from every retained snapshot.
    DeleteUnreachable,
}

/// Deterministic retention and deletion evidence for one garbage-collection pass.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GarbageCollectionReport {
    pub mode: GarbageCollectionMode,
    pub snapshots_scanned: usize,
    pub reachable: Vec<Digest>,
    pub unreachable: Vec<Digest>,
    pub deleted: Vec<Digest>,
}

/// Explicit recovery evidence for abandoned lock and temporary state.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RecoveryReport {
    pub stale_lock_removed: bool,
    pub temporary_paths_removed: usize,
}

/// Content-addressed authoritative snapshot and evidence store.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SnapshotStore {
    root: PathBuf,
}

impl SnapshotStore {
    /// Open or initialize a store rooted at the supplied directory.
    pub fn open(root: impl AsRef<Path>) -> Result<Self, StorageError> {
        let root = root.as_ref().to_path_buf();
        create_directory(&root)?;
        create_directory(&root.join(OBJECTS_DIRECTORY))?;
        create_directory(&root.join(SNAPSHOTS_DIRECTORY))?;
        Ok(Self { root })
    }

    /// Borrow the store root.
    #[must_use]
    pub fn root(&self) -> &Path {
        &self.root
    }

    /// Begin an exclusive mutation transaction shared by writes, imports, and garbage collection.
    pub fn begin_write(&self) -> Result<WriteTransaction<'_>, StorageError> {
        Ok(WriteTransaction {
            store: self,
            _lock: MutationLock::acquire(&self.root)?,
        })
    }

    /// Store one object in a short exclusive transaction.
    pub fn put_object(
        &self,
        bytes: &[u8],
        media_type: impl Into<String>,
        role: impl Into<String>,
    ) -> Result<StoredObject, StorageError> {
        self.begin_write()?.put_object(bytes, media_type, role)
    }

    /// Read and independently verify one object against its descriptor.
    pub fn read_object(&self, descriptor: &ObjectDescriptor) -> Result<Vec<u8>, StorageError> {
        let bytes = read_content(
            &self.object_path(descriptor.digest())?,
            descriptor.digest(),
            "FDIR-OBJECT-MISSING",
            "FDIR-OBJECT-DIGEST-MISMATCH",
        )?;
        let length = u64::try_from(bytes.len()).map_err(|error| {
            StorageError::new(
                "FDIR-OBJECT-LENGTH-RANGE",
                descriptor.digest().as_str(),
                format!("object length cannot be represented as u64: {error}"),
            )
        })?;
        if length != descriptor.byte_length() {
            return Err(StorageError::new(
                "FDIR-OBJECT-LENGTH-MISMATCH",
                descriptor.digest().as_str(),
                format!(
                    "declared object length {} differs from stored length {length}",
                    descriptor.byte_length()
                ),
            ));
        }
        Ok(bytes)
    }

    /// Read and independently verify bytes by digest when descriptor metadata is unavailable.
    pub fn read_object_by_digest(&self, digest: &Digest) -> Result<Vec<u8>, StorageError> {
        read_content(
            &self.object_path(digest)?,
            digest,
            "FDIR-OBJECT-MISSING",
            "FDIR-OBJECT-DIGEST-MISMATCH",
        )
    }

    /// Atomically publish one canonical snapshot in a short exclusive transaction.
    pub fn write_snapshot(
        &self,
        manifest: &SnapshotManifest,
    ) -> Result<SnapshotReceipt, StorageError> {
        self.begin_write()?.write_snapshot(manifest)
    }

    /// Read a snapshot by content identity and verify its canonical bytes and every required object.
    pub fn read_snapshot(&self, digest: &Digest) -> Result<SnapshotManifest, StorageError> {
        let bytes = read_content(
            &self.snapshot_path(digest)?,
            digest,
            "FDIR-SNAPSHOT-MISSING",
            "FDIR-SNAPSHOT-DIGEST-MISMATCH",
        )?;
        let manifest = parse_snapshot_bytes(&bytes)?;
        self.validate_manifest(&manifest).into_result()?;
        Ok(manifest)
    }

    /// Validate structure, reference completeness, object length, and object digest integrity.
    #[must_use]
    pub fn validate_manifest(&self, manifest: &SnapshotManifest) -> ValidationReport {
        let mut report = manifest.validation_report();
        for (index, descriptor) in manifest.objects().iter().enumerate() {
            let path = format!("$/objects/{index}");
            let object_path = match self.object_path(descriptor.digest()) {
                Ok(value) => value,
                Err(error) => {
                    report.push(error.diagnostic().clone());
                    continue;
                }
            };
            let bytes = match fs::read(&object_path) {
                Ok(value) => value,
                Err(error) if error.kind() == io::ErrorKind::NotFound => {
                    report.push(StorageDiagnostic::new(
                        "FDIR-OBJECT-MISSING",
                        format!("{path}/digest"),
                        format!("required object {} is missing", descriptor.digest()),
                    ));
                    continue;
                }
                Err(error) => {
                    report.push(StorageDiagnostic::new(
                        "FDIR-OBJECT-READ",
                        format!("{path}/digest"),
                        format!("required object could not be read: {error}"),
                    ));
                    continue;
                }
            };
            match u64::try_from(bytes.len()) {
                Ok(length) if length != descriptor.byte_length() => {
                    report.push(StorageDiagnostic::new(
                        "FDIR-OBJECT-LENGTH-MISMATCH",
                        format!("{path}/byteLength"),
                        format!(
                            "declared length {} differs from stored length {length}",
                            descriptor.byte_length()
                        ),
                    ));
                }
                Ok(_) => {}
                Err(error) => report.push(StorageDiagnostic::new(
                    "FDIR-OBJECT-LENGTH-RANGE",
                    format!("{path}/byteLength"),
                    format!("stored object length cannot be represented as u64: {error}"),
                )),
            }
            match raw_content_digest(&bytes) {
                Ok(actual) if actual != *descriptor.digest() => {
                    report.push(StorageDiagnostic::new(
                        "FDIR-OBJECT-DIGEST-MISMATCH",
                        format!("{path}/digest"),
                        format!(
                            "declared digest {} differs from stored digest {actual}",
                            descriptor.digest()
                        ),
                    ));
                }
                Ok(_) => {}
                Err(error) => report.push(StorageDiagnostic::new(
                    error.code(),
                    format!("{path}/digest"),
                    error.message(),
                )),
            }
        }
        report
    }

    /// Return the deterministic storage path for one object digest.
    pub fn object_path(&self, digest: &Digest) -> Result<PathBuf, StorageError> {
        content_path(&self.root.join(OBJECTS_DIRECTORY), digest)
    }

    /// Return the deterministic storage path for one snapshot digest.
    pub fn snapshot_path(&self, digest: &Digest) -> Result<PathBuf, StorageError> {
        content_path(&self.root.join(SNAPSHOTS_DIRECTORY), digest)
    }

    /// List retained snapshots in digest order. Temporary and incomplete writes are excluded.
    pub fn list_snapshots(&self) -> Result<Vec<Digest>, StorageError> {
        list_content_digests(&self.root.join(SNAPSHOTS_DIRECTORY))
    }

    /// Explicitly remove one retained snapshot. Referenced objects remain until garbage collection.
    pub fn delete_snapshot(&self, digest: &Digest) -> Result<bool, StorageError> {
        self.begin_write()?.delete_snapshot(digest)
    }

    /// Export a complete snapshot and its referenced objects to a new portable directory.
    pub fn export_snapshot(
        &self,
        digest: &Digest,
        destination: impl AsRef<Path>,
    ) -> Result<(), StorageError> {
        let destination = destination.as_ref();
        if path_exists(destination)? {
            return Err(StorageError::new(
                "FDIR-EXPORT-DESTINATION-EXISTS",
                destination.display().to_string(),
                "export destination must not already exist",
            ));
        }
        let manifest = self.read_snapshot(digest)?;
        let snapshot_bytes = read_content(
            &self.snapshot_path(digest)?,
            digest,
            "FDIR-SNAPSHOT-MISSING",
            "FDIR-SNAPSHOT-DIGEST-MISMATCH",
        )?;
        let parent = usable_parent(destination);
        create_directory(parent)?;
        let temporary = create_temporary_directory(parent, "export")?;
        let result = (|| {
            write_synced_file(&temporary.join(EXPORT_SNAPSHOT_FILE), &snapshot_bytes)?;
            for descriptor in manifest.objects() {
                let bytes = self.read_object(descriptor)?;
                let path = content_path(&temporary.join(OBJECTS_DIRECTORY), descriptor.digest())?;
                write_synced_file(&path, &bytes)?;
            }
            let marker = export_marker_bytes(digest)?;
            write_synced_file(&temporary.join(EXPORT_COMPLETE_FILE), &marker)?;
            sync_directory(&temporary)?;
            fs::rename(&temporary, destination).map_err(|error| {
                io_storage_error(
                    "FDIR-EXPORT-PUBLISH",
                    destination,
                    "could not atomically publish export directory",
                    error,
                )
            })?;
            sync_directory(parent)
        })();
        if result.is_err() {
            let _ = fs::remove_dir_all(&temporary);
        }
        result
    }

    /// Import a complete portable export, deduplicating objects before publishing the snapshot.
    pub fn import_snapshot(
        &self,
        source: impl AsRef<Path>,
    ) -> Result<SnapshotReceipt, StorageError> {
        let source = source.as_ref();
        let expected_digest = read_export_marker(source)?;
        let snapshot_bytes = fs::read(source.join(EXPORT_SNAPSHOT_FILE)).map_err(|error| {
            io_storage_error(
                "FDIR-IMPORT-SNAPSHOT-MISSING",
                &source.join(EXPORT_SNAPSHOT_FILE),
                "could not read exported snapshot",
                error,
            )
        })?;
        let actual_digest = raw_content_digest(&snapshot_bytes).map_err(|error| {
            StorageError::new(error.code(), "$/snapshotDigest", error.message())
        })?;
        if actual_digest != expected_digest {
            return Err(StorageError::new(
                "FDIR-IMPORT-SNAPSHOT-DIGEST",
                source.display().to_string(),
                format!(
                    "export marker digest {expected_digest} differs from snapshot digest {actual_digest}"
                ),
            ));
        }
        let manifest = parse_snapshot_bytes(&snapshot_bytes)?;
        let transaction = self.begin_write()?;
        for descriptor in manifest.objects() {
            let path = content_path(&source.join(OBJECTS_DIRECTORY), descriptor.digest())?;
            let bytes = read_content(
                &path,
                descriptor.digest(),
                "FDIR-IMPORT-OBJECT-MISSING",
                "FDIR-IMPORT-OBJECT-DIGEST",
            )?;
            let length = u64::try_from(bytes.len()).map_err(|error| {
                StorageError::new(
                    "FDIR-IMPORT-OBJECT-LENGTH-RANGE",
                    path.display().to_string(),
                    format!("exported object length cannot be represented as u64: {error}"),
                )
            })?;
            if length != descriptor.byte_length() {
                return Err(StorageError::new(
                    "FDIR-IMPORT-OBJECT-LENGTH",
                    path.display().to_string(),
                    format!(
                        "exported object length {length} differs from declared length {}",
                        descriptor.byte_length()
                    ),
                ));
            }
            let published = transaction.put_object(
                &bytes,
                descriptor.media_type().to_owned(),
                descriptor.role().to_owned(),
            )?;
            if published.descriptor() != descriptor {
                return Err(StorageError::new(
                    "FDIR-IMPORT-OBJECT-DESCRIPTOR",
                    path.display().to_string(),
                    "imported object descriptor differs from snapshot authority",
                ));
            }
        }
        let receipt = transaction.write_snapshot(&manifest)?;
        if receipt.digest != expected_digest {
            return Err(StorageError::new(
                "FDIR-IMPORT-SNAPSHOT-IDENTITY",
                source.display().to_string(),
                format!(
                    "imported snapshot digest {} differs from export digest {expected_digest}",
                    receipt.digest
                ),
            ));
        }
        Ok(receipt)
    }

    /// Plan or execute retention-safe garbage collection under the exclusive mutation lock.
    pub fn garbage_collect(
        &self,
        mode: GarbageCollectionMode,
    ) -> Result<GarbageCollectionReport, StorageError> {
        self.begin_write()?.garbage_collect(mode)
    }

    /// Explicitly remove abandoned temporary state and a stale mutation lock.
    ///
    /// The caller must ensure no live writer is using this store. Recovery never edits a final
    /// content-addressed object or snapshot path.
    pub fn recover_interrupted_state(&self) -> Result<RecoveryReport, StorageError> {
        let mut temporary_paths = Vec::new();
        collect_temporary_paths(&self.root, &mut temporary_paths)?;
        temporary_paths.sort_by(|left, right| {
            right
                .components()
                .count()
                .cmp(&left.components().count())
                .then_with(|| right.cmp(left))
        });
        let mut removed = 0_usize;
        for path in temporary_paths {
            let metadata = fs::symlink_metadata(&path).map_err(|error| {
                io_storage_error(
                    "FDIR-RECOVERY-METADATA",
                    &path,
                    "could not inspect temporary path",
                    error,
                )
            })?;
            if metadata.is_dir() {
                fs::remove_dir_all(&path).map_err(|error| {
                    io_storage_error(
                        "FDIR-RECOVERY-REMOVE",
                        &path,
                        "could not remove temporary directory",
                        error,
                    )
                })?;
            } else {
                fs::remove_file(&path).map_err(|error| {
                    io_storage_error(
                        "FDIR-RECOVERY-REMOVE",
                        &path,
                        "could not remove temporary file",
                        error,
                    )
                })?;
            }
            removed = removed.saturating_add(1);
        }
        let lock_path = self.root.join(MUTATION_LOCK_DIRECTORY);
        let stale_lock_removed = if path_exists(&lock_path)? {
            fs::remove_dir_all(&lock_path).map_err(|error| {
                io_storage_error(
                    "FDIR-RECOVERY-LOCK",
                    &lock_path,
                    "could not remove stale mutation lock",
                    error,
                )
            })?;
            true
        } else {
            false
        };
        Ok(RecoveryReport {
            stale_lock_removed,
            temporary_paths_removed: removed,
        })
    }

    fn put_object_unlocked(
        &self,
        bytes: &[u8],
        media_type: String,
        role: String,
    ) -> Result<StoredObject, StorageError> {
        let digest = raw_content_digest(bytes)
            .map_err(|error| StorageError::new(error.code(), "$", error.message()))?;
        let byte_length = u64::try_from(bytes.len()).map_err(|error| {
            StorageError::new(
                "FDIR-OBJECT-LENGTH-RANGE",
                digest.as_str(),
                format!("object length cannot be represented as u64: {error}"),
            )
        })?;
        let descriptor = ObjectDescriptor::new(digest, byte_length, media_type, role)?;
        let path = self.object_path(descriptor.digest())?;
        let disposition = atomic_publish(&path, bytes, descriptor.digest(), "object")?;
        Ok(StoredObject {
            descriptor,
            disposition,
        })
    }

    fn write_snapshot_unlocked(
        &self,
        manifest: &SnapshotManifest,
    ) -> Result<SnapshotReceipt, StorageError> {
        self.validate_manifest(manifest).into_result()?;
        let bytes = manifest.canonical_bytes()?;
        let digest = raw_content_digest(&bytes)
            .map_err(|error| StorageError::new(error.code(), "$", error.message()))?;
        let path = self.snapshot_path(&digest)?;
        let disposition = atomic_publish(&path, &bytes, &digest, "snapshot")?;
        Ok(SnapshotReceipt {
            digest,
            disposition,
        })
    }

    fn delete_snapshot_unlocked(&self, digest: &Digest) -> Result<bool, StorageError> {
        let path = self.snapshot_path(digest)?;
        if !path_exists(&path)? {
            return Ok(false);
        }
        read_content(
            &path,
            digest,
            "FDIR-SNAPSHOT-MISSING",
            "FDIR-SNAPSHOT-DIGEST-MISMATCH",
        )?;
        fs::remove_file(&path).map_err(|error| {
            io_storage_error(
                "FDIR-SNAPSHOT-DELETE",
                &path,
                "could not delete retained snapshot",
                error,
            )
        })?;
        if let Some(parent) = path.parent() {
            sync_directory(parent)?;
        }
        Ok(true)
    }

    fn garbage_collect_unlocked(
        &self,
        mode: GarbageCollectionMode,
    ) -> Result<GarbageCollectionReport, StorageError> {
        let snapshots = self.list_snapshots()?;
        let mut reachable = BTreeSet::new();
        for digest in &snapshots {
            let manifest = self.read_snapshot(digest)?;
            reachable.extend(
                manifest
                    .objects()
                    .iter()
                    .map(|descriptor| descriptor.digest().clone()),
            );
        }
        let stored: BTreeSet<Digest> = list_content_digests(&self.root.join(OBJECTS_DIRECTORY))?
            .into_iter()
            .collect();
        let unreachable: Vec<Digest> = stored.difference(&reachable).cloned().collect();
        let mut deleted = Vec::new();
        if mode == GarbageCollectionMode::DeleteUnreachable {
            for digest in &unreachable {
                let path = self.object_path(digest)?;
                fs::remove_file(&path).map_err(|error| {
                    io_storage_error(
                        "FDIR-GC-DELETE",
                        &path,
                        "could not delete unreachable object",
                        error,
                    )
                })?;
                if let Some(parent) = path.parent() {
                    sync_directory(parent)?;
                }
                deleted.push(digest.clone());
            }
        }
        Ok(GarbageCollectionReport {
            mode,
            snapshots_scanned: snapshots.len(),
            reachable: reachable.into_iter().collect(),
            unreachable,
            deleted,
        })
    }
}

/// Exclusive mutation transaction. Dropping it releases the store lock.
pub struct WriteTransaction<'a> {
    store: &'a SnapshotStore,
    _lock: MutationLock,
}

impl WriteTransaction<'_> {
    /// Store one object while retaining the same exclusive transaction.
    pub fn put_object(
        &self,
        bytes: &[u8],
        media_type: impl Into<String>,
        role: impl Into<String>,
    ) -> Result<StoredObject, StorageError> {
        self.store
            .put_object_unlocked(bytes, media_type.into(), role.into())
    }

    /// Publish a snapshot after verifying every object while retaining the same transaction.
    pub fn write_snapshot(
        &self,
        manifest: &SnapshotManifest,
    ) -> Result<SnapshotReceipt, StorageError> {
        self.store.write_snapshot_unlocked(manifest)
    }

    /// Explicitly delete one retained snapshot while retaining the same transaction.
    pub fn delete_snapshot(&self, digest: &Digest) -> Result<bool, StorageError> {
        self.store.delete_snapshot_unlocked(digest)
    }

    /// Plan or execute garbage collection while retaining the same transaction.
    pub fn garbage_collect(
        &self,
        mode: GarbageCollectionMode,
    ) -> Result<GarbageCollectionReport, StorageError> {
        self.store.garbage_collect_unlocked(mode)
    }
}

struct MutationLock {
    path: PathBuf,
}

impl MutationLock {
    fn acquire(root: &Path) -> Result<Self, StorageError> {
        let path = root.join(MUTATION_LOCK_DIRECTORY);
        match fs::create_dir(&path) {
            Ok(()) => {}
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                return Err(StorageError::new(
                    "FDIR-STORAGE-LOCKED",
                    path.display().to_string(),
                    "another mutation transaction or unrecovered interrupted write owns the store",
                ));
            }
            Err(error) => {
                return Err(io_storage_error(
                    "FDIR-STORAGE-LOCK",
                    &path,
                    "could not create mutation lock",
                    error,
                ));
            }
        }
        let owner = format!("process={}\n", std::process::id());
        if let Err(error) = write_synced_file(&path.join("owner"), owner.as_bytes()) {
            let _ = fs::remove_dir_all(&path);
            return Err(error);
        }
        sync_directory(root)?;
        Ok(Self { path })
    }
}

impl Drop for MutationLock {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
        if let Some(parent) = self.path.parent() {
            let _ = sync_directory(parent);
        }
    }
}

fn atomic_publish(
    path: &Path,
    bytes: &[u8],
    expected_digest: &Digest,
    label: &str,
) -> Result<WriteDisposition, StorageError> {
    if path_exists(path)? {
        return verify_existing_content(path, bytes, expected_digest);
    }
    let parent = path.parent().ok_or_else(|| {
        StorageError::new(
            "FDIR-STORAGE-PATH",
            path.display().to_string(),
            "content path has no parent directory",
        )
    })?;
    create_directory(parent)?;
    let (mut file, temporary) = create_temporary_file(parent, label)?;
    let publish_result = (|| {
        file.write_all(bytes).map_err(|error| {
            io_storage_error(
                "FDIR-STORAGE-TEMP-WRITE",
                &temporary,
                "could not write temporary content",
                error,
            )
        })?;
        file.sync_all().map_err(|error| {
            io_storage_error(
                "FDIR-STORAGE-TEMP-SYNC",
                &temporary,
                "could not synchronize temporary content",
                error,
            )
        })?;
        drop(file);
        fs::rename(&temporary, path).map_err(|error| {
            io_storage_error(
                "FDIR-STORAGE-PUBLISH",
                path,
                "could not atomically publish content",
                error,
            )
        })?;
        sync_directory(parent)?;
        Ok(WriteDisposition::Created)
    })();
    if publish_result.is_err() {
        let _ = fs::remove_file(&temporary);
        if path_exists(path)? {
            return verify_existing_content(path, bytes, expected_digest);
        }
    }
    publish_result
}

fn verify_existing_content(
    path: &Path,
    expected_bytes: &[u8],
    expected_digest: &Digest,
) -> Result<WriteDisposition, StorageError> {
    let existing = fs::read(path).map_err(|error| {
        io_storage_error(
            "FDIR-STORAGE-EXISTING-READ",
            path,
            "could not read existing content-addressed file",
            error,
        )
    })?;
    if existing == expected_bytes {
        return Ok(WriteDisposition::Deduplicated);
    }
    let actual = raw_content_digest(&existing).map_err(|error| {
        StorageError::new(error.code(), path.display().to_string(), error.message())
    })?;
    if actual != *expected_digest {
        return Err(StorageError::new(
            "FDIR-STORAGE-EXISTING-CORRUPT",
            path.display().to_string(),
            format!("existing path for {expected_digest} contains bytes with digest {actual}"),
        ));
    }
    Err(StorageError::new(
        "FDIR-STORAGE-DIGEST-COLLISION",
        path.display().to_string(),
        format!("existing bytes differ despite sharing content digest {expected_digest}"),
    ))
}

fn read_content(
    path: &Path,
    expected_digest: &Digest,
    missing_code: &'static str,
    mismatch_code: &'static str,
) -> Result<Vec<u8>, StorageError> {
    let bytes = fs::read(path).map_err(|error| {
        let code = if error.kind() == io::ErrorKind::NotFound {
            missing_code
        } else {
            "FDIR-STORAGE-READ"
        };
        io_storage_error(code, path, "could not read content-addressed file", error)
    })?;
    let actual = raw_content_digest(&bytes).map_err(|error| {
        StorageError::new(error.code(), path.display().to_string(), error.message())
    })?;
    if actual != *expected_digest {
        return Err(StorageError::new(
            mismatch_code,
            path.display().to_string(),
            format!("expected digest {expected_digest}, found {actual}"),
        ));
    }
    Ok(bytes)
}

fn content_path(base: &Path, digest: &Digest) -> Result<PathBuf, StorageError> {
    let hex = digest.as_str().strip_prefix("sha256:").ok_or_else(|| {
        StorageError::new(
            "FDIR-STORAGE-DIGEST",
            digest.as_str(),
            "content digest does not use the sha256 algorithm",
        )
    })?;
    let (prefix, remainder) = hex.split_at(2);
    Ok(base
        .join(HASH_ALGORITHM_DIRECTORY)
        .join(prefix)
        .join(remainder))
}

fn list_content_digests(base: &Path) -> Result<Vec<Digest>, StorageError> {
    if !path_exists(base)? {
        return Ok(Vec::new());
    }
    let mut digests = Vec::new();
    for algorithm_entry in read_directory(base)? {
        let algorithm_path = algorithm_entry.path();
        let algorithm_name = utf8_file_name(&algorithm_path)?;
        if algorithm_name.starts_with(TEMPORARY_PREFIX) {
            continue;
        }
        if algorithm_name != HASH_ALGORITHM_DIRECTORY || !algorithm_path.is_dir() {
            return Err(StorageError::new(
                "FDIR-STORAGE-LAYOUT",
                algorithm_path.display().to_string(),
                "unexpected entry in content-addressed store",
            ));
        }
        for prefix_entry in read_directory(&algorithm_path)? {
            let prefix_path = prefix_entry.path();
            let prefix = utf8_file_name(&prefix_path)?;
            if prefix.starts_with(TEMPORARY_PREFIX) {
                continue;
            }
            if prefix.len() != 2 || !is_lower_hex(&prefix) || !prefix_path.is_dir() {
                return Err(StorageError::new(
                    "FDIR-STORAGE-LAYOUT",
                    prefix_path.display().to_string(),
                    "invalid SHA-256 prefix directory",
                ));
            }
            for object_entry in read_directory(&prefix_path)? {
                let object_path = object_entry.path();
                let remainder = utf8_file_name(&object_path)?;
                if remainder.starts_with(TEMPORARY_PREFIX) {
                    continue;
                }
                if remainder.len() != 62 || !is_lower_hex(&remainder) || !object_path.is_file() {
                    return Err(StorageError::new(
                        "FDIR-STORAGE-LAYOUT",
                        object_path.display().to_string(),
                        "invalid content-addressed file name",
                    ));
                }
                let digest =
                    Digest::new(format!("sha256:{prefix}{remainder}")).map_err(|error| {
                        StorageError::new(
                            "FDIR-STORAGE-DIGEST",
                            object_path.display().to_string(),
                            error.to_string(),
                        )
                    })?;
                digests.push(digest);
            }
        }
    }
    digests.sort();
    digests.dedup();
    Ok(digests)
}

fn export_marker_bytes(snapshot_digest: &Digest) -> Result<Vec<u8>, StorageError> {
    let mut value = ObjectValue::new();
    value.insert(
        "schema".to_owned(),
        CanonicalValue::String(SNAPSHOT_EXPORT_SCHEMA.to_owned()),
    );
    value.insert(
        "snapshotDigest".to_owned(),
        CanonicalValue::String(snapshot_digest.as_str().to_owned()),
    );
    canonical_bytes(&CanonicalValue::Object(value))
        .map_err(|error| StorageError::new(error.code(), error.path(), error.message()))
}

fn read_export_marker(source: &Path) -> Result<Digest, StorageError> {
    let path = source.join(EXPORT_COMPLETE_FILE);
    let bytes = fs::read(&path).map_err(|error| {
        io_storage_error(
            "FDIR-IMPORT-INCOMPLETE",
            &path,
            "portable export has no complete marker",
            error,
        )
    })?;
    let input = std::str::from_utf8(&bytes).map_err(|error| {
        StorageError::new(
            "FDIR-IMPORT-MARKER-UTF8",
            path.display().to_string(),
            format!("export marker is not UTF-8: {error}"),
        )
    })?;
    let value = CanonicalValue::parse_json(input).map_err(|error| {
        StorageError::new(
            "FDIR-IMPORT-MARKER-JSON",
            path.display().to_string(),
            error.to_string(),
        )
    })?;
    let canonical = canonical_bytes(&value)
        .map_err(|error| StorageError::new(error.code(), error.path(), error.message()))?;
    if canonical != bytes {
        return Err(StorageError::new(
            "FDIR-IMPORT-MARKER-NONCANONICAL",
            path.display().to_string(),
            "export complete marker is not canonical JSON",
        ));
    }
    let object = value.as_object().ok_or_else(|| {
        StorageError::new(
            "FDIR-IMPORT-MARKER-TYPE",
            path.display().to_string(),
            "export complete marker must be an object",
        )
    })?;
    if object.len() != 2 || !object.contains_key("schema") || !object.contains_key("snapshotDigest")
    {
        return Err(StorageError::new(
            "FDIR-IMPORT-MARKER-FIELDS",
            path.display().to_string(),
            "export complete marker has unexpected fields",
        ));
    }
    let schema = object
        .get("schema")
        .and_then(CanonicalValue::as_str)
        .ok_or_else(|| {
            StorageError::new(
                "FDIR-IMPORT-MARKER-SCHEMA",
                path.display().to_string(),
                "export marker schema must be a string",
            )
        })?;
    if schema != SNAPSHOT_EXPORT_SCHEMA {
        return Err(StorageError::new(
            "FDIR-IMPORT-MARKER-SCHEMA",
            path.display().to_string(),
            format!("unsupported export marker schema {schema:?}"),
        ));
    }
    let digest = object
        .get("snapshotDigest")
        .and_then(CanonicalValue::as_str)
        .ok_or_else(|| {
            StorageError::new(
                "FDIR-IMPORT-MARKER-DIGEST",
                path.display().to_string(),
                "export marker snapshotDigest must be a string",
            )
        })?;
    Digest::new(digest.to_owned()).map_err(|error| {
        StorageError::new(
            "FDIR-IMPORT-MARKER-DIGEST",
            path.display().to_string(),
            error.to_string(),
        )
    })
}

fn create_temporary_file(parent: &Path, label: &str) -> Result<(File, PathBuf), StorageError> {
    for _ in 0..TEMPORARY_ATTEMPTS {
        let counter = TEMPORARY_COUNTER.fetch_add(1, Ordering::Relaxed);
        let path = parent.join(format!(
            "{TEMPORARY_PREFIX}{label}-{}-{counter}",
            std::process::id()
        ));
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(file) => return Ok((file, path)),
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
            Err(error) => {
                return Err(io_storage_error(
                    "FDIR-STORAGE-TEMP-CREATE",
                    &path,
                    "could not create temporary file",
                    error,
                ));
            }
        }
    }
    Err(StorageError::new(
        "FDIR-STORAGE-TEMP-EXHAUSTED",
        parent.display().to_string(),
        "could not allocate a unique temporary file",
    ))
}

fn create_temporary_directory(parent: &Path, label: &str) -> Result<PathBuf, StorageError> {
    for _ in 0..TEMPORARY_ATTEMPTS {
        let counter = TEMPORARY_COUNTER.fetch_add(1, Ordering::Relaxed);
        let path = parent.join(format!(
            "{TEMPORARY_PREFIX}{label}-{}-{counter}",
            std::process::id()
        ));
        match fs::create_dir(&path) {
            Ok(()) => return Ok(path),
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
            Err(error) => {
                return Err(io_storage_error(
                    "FDIR-STORAGE-TEMP-CREATE",
                    &path,
                    "could not create temporary directory",
                    error,
                ));
            }
        }
    }
    Err(StorageError::new(
        "FDIR-STORAGE-TEMP-EXHAUSTED",
        parent.display().to_string(),
        "could not allocate a unique temporary directory",
    ))
}

fn write_synced_file(path: &Path, bytes: &[u8]) -> Result<(), StorageError> {
    let parent = path.parent().ok_or_else(|| {
        StorageError::new(
            "FDIR-STORAGE-PATH",
            path.display().to_string(),
            "file path has no parent directory",
        )
    })?;
    create_directory(parent)?;
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|error| {
            io_storage_error(
                "FDIR-STORAGE-FILE-CREATE",
                path,
                "could not create file",
                error,
            )
        })?;
    file.write_all(bytes).map_err(|error| {
        io_storage_error(
            "FDIR-STORAGE-FILE-WRITE",
            path,
            "could not write file",
            error,
        )
    })?;
    file.sync_all().map_err(|error| {
        io_storage_error(
            "FDIR-STORAGE-FILE-SYNC",
            path,
            "could not synchronize file",
            error,
        )
    })?;
    sync_directory(parent)
}

fn create_directory(path: &Path) -> Result<(), StorageError> {
    fs::create_dir_all(path).map_err(|error| {
        io_storage_error(
            "FDIR-STORAGE-DIRECTORY",
            path,
            "could not create storage directory",
            error,
        )
    })
}

fn sync_directory(path: &Path) -> Result<(), StorageError> {
    File::open(path)
        .and_then(|directory| directory.sync_all())
        .map_err(|error| {
            io_storage_error(
                "FDIR-STORAGE-DIRECTORY-SYNC",
                path,
                "could not synchronize directory metadata",
                error,
            )
        })
}

fn read_directory(path: &Path) -> Result<Vec<fs::DirEntry>, StorageError> {
    let mut entries = fs::read_dir(path)
        .map_err(|error| {
            io_storage_error(
                "FDIR-STORAGE-DIRECTORY-READ",
                path,
                "could not read storage directory",
                error,
            )
        })?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| {
            io_storage_error(
                "FDIR-STORAGE-DIRECTORY-ENTRY",
                path,
                "could not read storage directory entry",
                error,
            )
        })?;
    entries.sort_by_key(fs::DirEntry::file_name);
    Ok(entries)
}

fn collect_temporary_paths(root: &Path, output: &mut Vec<PathBuf>) -> Result<(), StorageError> {
    for entry in read_directory(root)? {
        let path = entry.path();
        let name = utf8_file_name(&path)?;
        if name == MUTATION_LOCK_DIRECTORY {
            continue;
        }
        if name.starts_with(TEMPORARY_PREFIX) {
            output.push(path);
            continue;
        }
        if entry
            .file_type()
            .map_err(|error| {
                io_storage_error(
                    "FDIR-RECOVERY-METADATA",
                    &path,
                    "could not inspect storage entry",
                    error,
                )
            })?
            .is_dir()
        {
            collect_temporary_paths(&path, output)?;
        }
    }
    Ok(())
}

fn path_exists(path: &Path) -> Result<bool, StorageError> {
    path.try_exists().map_err(|error| {
        io_storage_error(
            "FDIR-STORAGE-METADATA",
            path,
            "could not inspect path",
            error,
        )
    })
}

fn utf8_file_name(path: &Path) -> Result<String, StorageError> {
    path.file_name()
        .and_then(|value| value.to_str())
        .map(str::to_owned)
        .ok_or_else(|| {
            StorageError::new(
                "FDIR-STORAGE-PATH-UTF8",
                path.display().to_string(),
                "storage entry name is not valid UTF-8",
            )
        })
}

fn is_lower_hex(value: &str) -> bool {
    value
        .bytes()
        .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
}

fn usable_parent(path: &Path) -> &Path {
    path.parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or(Path::new("."))
}

fn io_storage_error(
    code: &'static str,
    path: &Path,
    action: &str,
    error: io::Error,
) -> StorageError {
    StorageError::new(
        code,
        path.display().to_string(),
        format!("{action}: {error}"),
    )
}
