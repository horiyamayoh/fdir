# Rebuildable SQLite index

The SQLite database implemented by `fdir-storage` is a disposable materialized projection. Canonical snapshot JSON and content-addressed evidence remain the only authorities. Deleting, invalidating, or replacing the database cannot delete or alter release-critical information.

## Authority boundary

Every trusted open is bound to one canonical snapshot digest. The index stores the exact canonical root JSON, a complete RFC 6901 path traversal, generated entity tables, snapshot object and reference rows, explicit status transitions, provenance, capability/profile declarations, and supported-query records. These rows are derived only from the supplied `SnapshotManifest`; no SQLite-only field participates in canonical identity.

The materializer verifies all of the following before returning an index handle:

- the SQLite application identifier and generated schema version;
- SQLite integrity and foreign-key checks;
- the generated DDL digest and materializer version;
- the expected canonical snapshot digest and exact root bytes;
- every generated table against a fresh canonical traversal;
- the canonicalized projection digest used by rebuild-parity receipts.

A missing, corrupt, stale, wrong-version, wrong-snapshot, or logically divergent database fails closed. Older schemas require a full rebuild. Future schemas are rejected by older readers.

## Build and invalidation modes

`Clean` creates a new database and refuses to overwrite an existing path. `Full` builds a complete sibling database, verifies it, and then publishes it with a recoverable backup of the previous index. `Incremental` replaces all derived rows in one transaction while retaining the database file and incrementing operational generation metadata. Explicit invalidation removes only the disposable index.

Clean, full, incremental, and delete-then-rebuild operations produce the same canonicalized projection dump and digest for the same snapshot. Build mode and generation are operational metadata and are intentionally excluded from that digest.

## Supported consistency queries

The consistency API compares SQLite results with direct canonical traversal for units, assertions, evidence links and objects, relations, status vectors and transitions, capabilities, profiles, diagnostics, provenance, and explicit non-complete outcomes such as `partial`, `unsupported`, `unresolved`, and `resource-limited`.

The final user-facing query and CLI surface remains outside this boundary. Callers must retain the expected snapshot digest and must not treat a successful standalone SQL query as authority without the validated `SqliteIndex::open` path.

## Dependency and qualification state

The exact `rusqlite` dependency and bundled SQLite build are recorded in `machine/dependency-catalog.yaml`. They are admitted only for the `storage-codec` lane, receive generated canonical projection records rather than untrusted document bytes, use no network at runtime, and remain development-unqualified under Issue #11.
