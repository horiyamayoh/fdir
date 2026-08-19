-- Generated from machine/logical-model.yaml. Rebuildable projection only; never authority.
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = DELETE;
PRAGMA synchronous = FULL;
PRAGMA application_id = 1178880338;
PRAGMA user_version = 1;
CREATE TABLE index_meta (
  singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
  schemaVersion INTEGER NOT NULL CHECK(schemaVersion = 1),
  materializerVersion TEXT NOT NULL,
  ddlDigest TEXT NOT NULL,
  snapshotDigest TEXT NOT NULL,
  snapshotByteLength INTEGER NOT NULL CHECK(snapshotByteLength >= 0),
  buildMode TEXT NOT NULL CHECK(buildMode IN ('clean', 'full', 'incremental')),
  generation INTEGER NOT NULL CHECK(generation >= 1),
  state TEXT NOT NULL CHECK(state IN ('building', 'complete'))
);
CREATE TABLE snapshot_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE canonical_nodes (
  path TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('null', 'boolean', 'number', 'string', 'array', 'object')),
  json TEXT NOT NULL CHECK(json_valid(json))
);
CREATE TABLE materialized_records (
  category TEXT NOT NULL CHECK(category IN (
    'unit',
    'assertion',
    'assertion-evidence',
    'evidence-object',
    'evidence-reference',
    'relation',
    'status',
    'status-transition',
    'capability',
    'profile',
    'diagnostic',
    'provenance',
    'outcome'
  )),
  sourcePath TEXT NOT NULL,
  recordKey TEXT NOT NULL,
  json TEXT NOT NULL CHECK(json_valid(json)),
  PRIMARY KEY(category, sourcePath)
);
CREATE TABLE artifacts (
  artifactId TEXT PRIMARY KEY,
  sourcePath TEXT NOT NULL UNIQUE,
  json TEXT NOT NULL CHECK(json_valid(json))
);
CREATE TABLE carriers (
  carrierId TEXT PRIMARY KEY,
  sourcePath TEXT NOT NULL UNIQUE,
  json TEXT NOT NULL CHECK(json_valid(json))
);
CREATE TABLE occurrences (
  occurrenceId TEXT PRIMARY KEY,
  sourcePath TEXT NOT NULL UNIQUE,
  json TEXT NOT NULL CHECK(json_valid(json))
);
CREATE TABLE units (
  unitId TEXT PRIMARY KEY,
  sourcePath TEXT NOT NULL UNIQUE,
  json TEXT NOT NULL CHECK(json_valid(json))
);
CREATE TABLE assertions (
  assertionId TEXT PRIMARY KEY,
  sourcePath TEXT NOT NULL UNIQUE,
  json TEXT NOT NULL CHECK(json_valid(json))
);
CREATE TABLE relations (
  relationId TEXT PRIMARY KEY,
  sourcePath TEXT NOT NULL UNIQUE,
  json TEXT NOT NULL CHECK(json_valid(json))
);
CREATE TABLE inventory_domains (
  inventoryDomainId TEXT PRIMARY KEY,
  sourcePath TEXT NOT NULL UNIQUE,
  json TEXT NOT NULL CHECK(json_valid(json))
);
CREATE TABLE accounting_items (
  accountingItemId TEXT PRIMARY KEY,
  sourcePath TEXT NOT NULL UNIQUE,
  json TEXT NOT NULL CHECK(json_valid(json))
);
CREATE TABLE guarantee_statuses (
  guaranteeStatusId TEXT PRIMARY KEY,
  sourcePath TEXT NOT NULL UNIQUE,
  json TEXT NOT NULL CHECK(json_valid(json))
);
CREATE TABLE diagnostics (
  diagnosticId TEXT PRIMARY KEY,
  sourcePath TEXT NOT NULL UNIQUE,
  json TEXT NOT NULL CHECK(json_valid(json))
);
CREATE TABLE snapshot_objects (
  digest TEXT PRIMARY KEY,
  byteLength INTEGER NOT NULL CHECK(byteLength >= 0),
  mediaType TEXT NOT NULL,
  role TEXT NOT NULL,
  sourcePath TEXT NOT NULL UNIQUE
);
CREATE TABLE object_references (
  source TEXT NOT NULL,
  target TEXT NOT NULL,
  relation TEXT NOT NULL,
  sourcePath TEXT NOT NULL UNIQUE,
  PRIMARY KEY(source, target, relation)
);
CREATE TABLE status_transitions (
  fromState TEXT NOT NULL,
  toState TEXT NOT NULL,
  sourcePath TEXT NOT NULL UNIQUE,
  PRIMARY KEY(fromState, toState)
);
CREATE TABLE assertion_evidence (
  assertionId TEXT NOT NULL,
  occurrenceId TEXT NOT NULL,
  sourcePath TEXT NOT NULL UNIQUE,
  json TEXT NOT NULL CHECK(json_valid(json)),
  PRIMARY KEY(assertionId, occurrenceId)
);
CREATE TABLE provenance_records (
  recordKey TEXT PRIMARY KEY,
  sourcePath TEXT NOT NULL UNIQUE,
  json TEXT NOT NULL CHECK(json_valid(json))
);
CREATE TABLE capabilities (
  capabilityId TEXT PRIMARY KEY,
  sourcePath TEXT NOT NULL UNIQUE,
  json TEXT NOT NULL CHECK(json_valid(json))
);
CREATE TABLE profiles (
  profileId TEXT PRIMARY KEY,
  sourcePath TEXT NOT NULL UNIQUE,
  json TEXT NOT NULL CHECK(json_valid(json))
);
CREATE TABLE outcomes (
  sourcePath TEXT PRIMARY KEY,
  outcomeKey TEXT NOT NULL,
  state TEXT NOT NULL,
  json TEXT NOT NULL CHECK(json_valid(json))
);
CREATE INDEX materialized_records_by_category_key
  ON materialized_records(category, recordKey, sourcePath);
CREATE INDEX canonical_nodes_by_kind ON canonical_nodes(kind, path);
CREATE INDEX assertions_by_unit ON assertions(json_extract(json, '$.unitId'));
CREATE INDEX occurrences_by_carrier ON occurrences(json_extract(json, '$.carrierId'));
CREATE INDEX accounting_by_domain ON accounting_items(json_extract(json, '$.inventoryDomainId'));
CREATE INDEX diagnostics_by_code ON diagnostics(json_extract(json, '$.code'));
CREATE INDEX guarantee_statuses_by_profile
  ON guarantee_statuses(json_extract(json, '$.profileId'));
CREATE INDEX outcomes_by_state ON outcomes(state, sourcePath);
