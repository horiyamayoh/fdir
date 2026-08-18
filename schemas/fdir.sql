-- Generated from machine/logical-model.yaml. Rebuildable projection only.
PRAGMA foreign_keys = ON;
CREATE TABLE snapshot_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE artifacts (artifactId TEXT PRIMARY KEY, json TEXT NOT NULL CHECK(json_valid(json)));
CREATE TABLE carriers (carrierId TEXT PRIMARY KEY, json TEXT NOT NULL CHECK(json_valid(json)));
CREATE TABLE occurrences (occurrenceId TEXT PRIMARY KEY, json TEXT NOT NULL CHECK(json_valid(json)));
CREATE TABLE units (unitId TEXT PRIMARY KEY, json TEXT NOT NULL CHECK(json_valid(json)));
CREATE TABLE assertions (assertionId TEXT PRIMARY KEY, json TEXT NOT NULL CHECK(json_valid(json)));
CREATE TABLE relations (relationId TEXT PRIMARY KEY, json TEXT NOT NULL CHECK(json_valid(json)));
CREATE TABLE inventory_domains (inventoryDomainId TEXT PRIMARY KEY, json TEXT NOT NULL CHECK(json_valid(json)));
CREATE TABLE accounting_items (accountingItemId TEXT PRIMARY KEY, json TEXT NOT NULL CHECK(json_valid(json)));
CREATE TABLE guarantee_statuses (guaranteeStatusId TEXT PRIMARY KEY, json TEXT NOT NULL CHECK(json_valid(json)));
CREATE TABLE diagnostics (diagnosticId TEXT PRIMARY KEY, json TEXT NOT NULL CHECK(json_valid(json)));
CREATE INDEX assertions_by_unit ON assertions(json_extract(json, '$.unitId'));
CREATE INDEX occurrences_by_carrier ON occurrences(json_extract(json, '$.carrierId'));
CREATE INDEX accounting_by_domain ON accounting_items(json_extract(json, '$.inventoryDomainId'));
CREATE INDEX diagnostics_by_code ON diagnostics(json_extract(json, '$.code'));
