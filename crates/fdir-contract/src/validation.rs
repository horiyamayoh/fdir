#![forbid(unsafe_code)]

use std::collections::{BTreeMap, BTreeSet};

use crate::{
    CanonicalValue, Digest, EntitySpec, JsonError, ObjectValue, PropertyKind, PropertySpec,
    entity_spec, enum_spec,
};

/// Deterministic set of validation codes returned by the Rust model boundary.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct ValidationReport {
    codes: Vec<String>,
}

impl ValidationReport {
    fn from_codes(codes: BTreeSet<String>) -> Self {
        Self {
            codes: codes.into_iter().collect(),
        }
    }

    /// Whether the document satisfies the generated shape and shared semantic invariants.
    pub fn is_valid(&self) -> bool {
        self.codes.is_empty()
    }

    /// Sorted, duplicate-free machine-readable validation codes.
    pub fn codes(&self) -> &[String] {
        &self.codes
    }

    /// Test for one stable validation code.
    pub fn contains(&self, code: &str) -> bool {
        self.codes.iter().any(|item| item == code)
    }
}

/// Parse and validate a snapshot through the dependency-free Rust model boundary.
pub fn validate_snapshot_json(input: &str) -> Result<ValidationReport, JsonError> {
    CanonicalValue::parse_json(input).map(|value| validate_snapshot_value(&value))
}

/// Validate an already parsed snapshot value.
pub fn validate_snapshot_value(value: &CanonicalValue) -> ValidationReport {
    let mut codes = BTreeSet::new();
    validate_entity(value, "Snapshot", "$", &mut codes);
    let Some(document) = value.as_object() else {
        codes.insert("ROOT_NOT_OBJECT".to_owned());
        return ValidationReport::from_codes(codes);
    };
    validate_shared_semantics(document, &mut codes);
    validate_generated_references(document, &mut codes);
    ValidationReport::from_codes(codes)
}

fn validate_entity(
    value: &CanonicalValue,
    entity_name: &str,
    path: &str,
    codes: &mut BTreeSet<String>,
) {
    let Some(specification) = entity_spec(entity_name) else {
        codes.insert(format!("UNKNOWN_ENTITY_SPEC:{entity_name}"));
        return;
    };
    let Some(object) = value.as_object() else {
        codes.insert(format!("ENTITY_NOT_OBJECT:{path}"));
        return;
    };
    for property in specification.properties {
        match object.get(property.json_name) {
            Some(property_value) => validate_property(property_value, property, path, codes),
            None if property.required => {
                codes.insert(format!("MISSING_PROPERTY:{path}.{}", property.json_name));
            }
            None => {}
        }
    }
}

fn validate_property(
    value: &CanonicalValue,
    property: &PropertySpec,
    parent_path: &str,
    codes: &mut BTreeSet<String>,
) {
    let path = format!("{parent_path}.{}", property.json_name);
    match property.kind {
        PropertyKind::Id | PropertyKind::Reference => {
            validate_non_empty_string(value, &path, codes)
        }
        PropertyKind::IdArray | PropertyKind::ReferenceArray => {
            validate_string_array(value, property, &path, codes);
        }
        PropertyKind::EntityArray => validate_entity_array(value, property, &path, codes),
        PropertyKind::Enumeration => validate_enumeration(value, property, &path, codes),
        PropertyKind::String => {
            if value.as_str().is_none() {
                codes.insert(format!("TYPE_STRING:{path}"));
            }
        }
        PropertyKind::Constant => {
            if value.as_str() != property.constant {
                codes.insert(format!("CONST_MISMATCH:{path}"));
            }
        }
        PropertyKind::Integer => validate_integer(value, property, &path, codes),
        PropertyKind::Number => validate_number(value, property, &path, codes),
        PropertyKind::NumberArray => validate_number_array(value, property, &path, codes),
        PropertyKind::Digest => validate_digest(value, &path, codes),
        PropertyKind::Object => {
            if value.as_object().is_none() {
                codes.insert(format!("TYPE_OBJECT:{path}"));
            }
        }
        PropertyKind::Any => {}
    }
}

fn validate_non_empty_string(value: &CanonicalValue, path: &str, codes: &mut BTreeSet<String>) {
    if !matches!(value.as_str(), Some(text) if !text.is_empty()) {
        codes.insert(format!("TYPE_NON_EMPTY_STRING:{path}"));
    }
}

fn validate_string_array(
    value: &CanonicalValue,
    property: &PropertySpec,
    path: &str,
    codes: &mut BTreeSet<String>,
) {
    let Some(items) = value.as_array() else {
        codes.insert(format!("TYPE_ARRAY:{path}"));
        return;
    };
    validate_array_bounds(items.len(), property, path, codes);
    for (index, item) in items.iter().enumerate() {
        validate_non_empty_string(item, &format!("{path}[{index}]"), codes);
    }
}

fn validate_entity_array(
    value: &CanonicalValue,
    property: &PropertySpec,
    path: &str,
    codes: &mut BTreeSet<String>,
) {
    let Some(items) = value.as_array() else {
        codes.insert(format!("TYPE_ARRAY:{path}"));
        return;
    };
    validate_array_bounds(items.len(), property, path, codes);
    let Some(target) = property.target else {
        codes.insert(format!("ENTITY_ARRAY_TARGET_MISSING:{path}"));
        return;
    };
    for (index, item) in items.iter().enumerate() {
        validate_entity(item, target, &format!("{path}[{index}]"), codes);
    }
}

fn validate_enumeration(
    value: &CanonicalValue,
    property: &PropertySpec,
    path: &str,
    codes: &mut BTreeSet<String>,
) {
    let Some(text) = value.as_str() else {
        codes.insert(format!("TYPE_ENUM_STRING:{path}"));
        return;
    };
    let Some(name) = property.enumeration else {
        codes.insert(format!("ENUM_SPEC_MISSING:{path}"));
        return;
    };
    let Some(specification) = enum_spec(name) else {
        codes.insert(format!("UNKNOWN_ENUM_SPEC:{name}"));
        return;
    };
    if !specification.values.contains(&text) {
        codes.insert(format!("UNKNOWN_ENUM_VALUE:{path}"));
    }
}

fn validate_integer(
    value: &CanonicalValue,
    property: &PropertySpec,
    path: &str,
    codes: &mut BTreeSet<String>,
) {
    let Some(number) = value.as_number() else {
        codes.insert(format!("TYPE_INTEGER:{path}"));
        return;
    };
    let Some(integer) = number.as_u64() else {
        codes.insert(format!("TYPE_INTEGER:{path}"));
        return;
    };
    if property
        .minimum
        .is_some_and(|minimum| (integer as f64) < minimum)
        || property
            .maximum
            .is_some_and(|maximum| (integer as f64) > maximum)
    {
        codes.insert(format!("NUMBER_RANGE:{path}"));
    }
}

fn validate_number(
    value: &CanonicalValue,
    property: &PropertySpec,
    path: &str,
    codes: &mut BTreeSet<String>,
) {
    let Some(number) = value.as_number() else {
        codes.insert(format!("TYPE_NUMBER:{path}"));
        return;
    };
    let Some(number) = number.as_f64() else {
        codes.insert(format!("TYPE_NUMBER:{path}"));
        return;
    };
    if property.minimum.is_some_and(|minimum| number < minimum)
        || property.maximum.is_some_and(|maximum| number > maximum)
    {
        codes.insert(format!("NUMBER_RANGE:{path}"));
    }
}

fn validate_number_array(
    value: &CanonicalValue,
    property: &PropertySpec,
    path: &str,
    codes: &mut BTreeSet<String>,
) {
    let Some(items) = value.as_array() else {
        codes.insert(format!("TYPE_ARRAY:{path}"));
        return;
    };
    validate_array_bounds(items.len(), property, path, codes);
    for (index, item) in items.iter().enumerate() {
        if item.as_number().is_none() {
            codes.insert(format!("TYPE_NUMBER:{path}[{index}]"));
        }
    }
}

fn validate_array_bounds(
    length: usize,
    property: &PropertySpec,
    path: &str,
    codes: &mut BTreeSet<String>,
) {
    if property.min_items.is_some_and(|minimum| length < minimum)
        || property.max_items.is_some_and(|maximum| length > maximum)
    {
        codes.insert(format!("ARRAY_LENGTH:{path}"));
    }
}

fn validate_digest(value: &CanonicalValue, path: &str, codes: &mut BTreeSet<String>) {
    let Some(text) = value.as_str() else {
        codes.insert(format!("TYPE_DIGEST_STRING:{path}"));
        return;
    };
    if Digest::new(text).is_err() {
        codes.insert(format!("DIGEST_INVALID:{path}"));
    }
}

fn validate_shared_semantics(document: &ObjectValue, codes: &mut BTreeSet<String>) {
    if string_value(document, "fdirVersion") != Some(crate::MODEL_VERSION) {
        codes.insert("VERSION_MISMATCH".to_owned());
    }
    for key in [
        "artifacts",
        "carriers",
        "selectors",
        "occurrences",
        "units",
        "assertions",
        "relations",
        "inventoryDomains",
        "accountingItems",
        "guaranteeStatuses",
        "diagnostics",
    ] {
        if array_value(document, key).is_none() {
            codes.insert(format!("MISSING_ARRAY:{key}"));
        }
    }

    let identities = collect_identities(document, codes);
    let artifacts = identities.get("Artifact").cloned().unwrap_or_default();
    let carriers = identities.get("Carrier").cloned().unwrap_or_default();
    let selectors = identities.get("Selector").cloned().unwrap_or_default();
    let occurrences = identities.get("Occurrence").cloned().unwrap_or_default();
    let units = identities
        .get("InformationUnit")
        .cloned()
        .unwrap_or_default();
    let assertions = identities
        .get("RecordAssertion")
        .cloned()
        .unwrap_or_default();
    let contexts = identities
        .get("InterpretationContext")
        .cloned()
        .unwrap_or_default();
    let diagnostics = identities.get("Diagnostic").cloned().unwrap_or_default();
    let inventory_domains = identities
        .get("InventoryDomain")
        .cloned()
        .unwrap_or_default();

    for carrier in object_items(document, "carriers") {
        if !contains_id(&artifacts, carrier, "artifactId") {
            codes.insert("UNKNOWN_ARTIFACT_REF".to_owned());
        }
    }
    for occurrence in object_items(document, "occurrences") {
        if !contains_id(&carriers, occurrence, "carrierId") {
            codes.insert("UNKNOWN_CARRIER_REF".to_owned());
        }
        for selector_id in string_items(occurrence, "selectorIds") {
            if !selectors.contains(selector_id) {
                codes.insert("UNKNOWN_SELECTOR_REF".to_owned());
            }
        }
    }

    let mut all_assertions_by_unit: BTreeMap<String, usize> = BTreeMap::new();
    for assertion in object_items(document, "assertions") {
        let unit_id = string_value(assertion, "unitId");
        match unit_id {
            Some(unit_id) if units.contains(unit_id) => {
                *all_assertions_by_unit
                    .entry(unit_id.to_owned())
                    .or_default() += 1;
            }
            _ => {
                codes.insert("UNKNOWN_UNIT_REF".to_owned());
            }
        }
        let Some(occurrence_ids) = array_value(assertion, "occurrenceIds") else {
            codes.insert("ASSERTION_OCCURRENCES_REQUIRED".to_owned());
            continue;
        };
        if string_value(assertion, "status") == Some("accepted") && occurrence_ids.is_empty() {
            let related = string_items(assertion, "diagnosticIds");
            if !related.iter().any(|item| diagnostics.contains(*item)) {
                codes.insert("ACCEPTED_ASSERTION_WITHOUT_EVIDENCE".to_owned());
            }
        }
        for occurrence_id in occurrence_ids.iter().filter_map(CanonicalValue::as_str) {
            if !occurrences.contains(occurrence_id) {
                codes.insert("UNKNOWN_OCCURRENCE_REF".to_owned());
            }
        }
        if let Some(context_id) = string_value(assertion, "contextId")
            && !contexts.contains(context_id)
        {
            codes.insert("UNKNOWN_CONTEXT_REF".to_owned());
        }
    }
    for unit_id in &units {
        if all_assertions_by_unit
            .get(unit_id)
            .copied()
            .unwrap_or_default()
            == 0
        {
            codes.insert("UNIT_WITHOUT_ASSERTION".to_owned());
        }
    }

    for relation in object_items(document, "relations") {
        let source_known = contains_id(&units, relation, "sourceUnitId");
        let target_known = contains_id(&units, relation, "targetUnitId");
        if !source_known || !target_known {
            codes.insert("UNKNOWN_RELATION_UNIT_REF".to_owned());
        }
    }
    for projection in object_items(document, "acceptedProjections") {
        let unit_id = string_value(projection, "unitId");
        if unit_id.is_none_or(|item| !units.contains(item)) {
            codes.insert("UNKNOWN_PROJECTION_UNIT_REF".to_owned());
        }
        for assertion_id in string_items(projection, "assertionIds") {
            if !assertions.contains(assertion_id) {
                codes.insert("UNKNOWN_PROJECTION_ASSERTION_REF".to_owned());
            }
        }
        let allowed: BTreeSet<&str> = object_items(document, "assertions")
            .filter(|assertion| {
                string_value(assertion, "status") == Some("accepted")
                    && string_value(assertion, "unitId") == unit_id
            })
            .filter_map(|assertion| string_value(assertion, "assertionId"))
            .collect();
        if string_items(projection, "assertionIds")
            .iter()
            .any(|item| !allowed.contains(item))
        {
            codes.insert("PROJECTION_USES_NON_ACCEPTED_ASSERTION".to_owned());
        }
    }

    validate_accounting(document, &inventory_domains, &units, codes);
    validate_equivalence(document, codes);
    if object_value(document, "claims").is_some_and(|claims| {
        bool_value(claims, "productionReady") == Some(true)
            || bool_value(claims, "qualified") == Some(true)
    }) {
        codes.insert("FALSE_PRODUCTION_CLAIM".to_owned());
    }
}

fn collect_identities(
    document: &ObjectValue,
    codes: &mut BTreeSet<String>,
) -> BTreeMap<&'static str, BTreeSet<String>> {
    let mut result: BTreeMap<&'static str, BTreeSet<String>> = BTreeMap::new();
    let mut all_ids = BTreeSet::new();
    let Some(snapshot) = entity_spec("Snapshot") else {
        codes.insert("UNKNOWN_ENTITY_SPEC:Snapshot".to_owned());
        return result;
    };
    for property in snapshot.properties {
        if property.kind != PropertyKind::EntityArray {
            continue;
        }
        let Some(target) = property.target else {
            continue;
        };
        let Some(target_spec) = entity_spec(target) else {
            codes.insert(format!("UNKNOWN_ENTITY_SPEC:{target}"));
            continue;
        };
        let mut seen = BTreeSet::new();
        for item in array_value(document, property.json_name)
            .into_iter()
            .flatten()
        {
            let Some(object) = item.as_object() else {
                codes.insert(format!("INVALID_ID:{}", property.json_name));
                continue;
            };
            let Some(identifier) = string_value(object, target_spec.identity)
                .filter(|identifier| !identifier.is_empty())
            else {
                codes.insert(format!("INVALID_ID:{}", property.json_name));
                continue;
            };
            if !seen.insert(identifier.to_owned()) || !all_ids.insert(identifier.to_owned()) {
                codes.insert(format!("DUPLICATE_ID:{identifier}"));
            }
        }
        result.insert(target, seen);
    }
    result
}

fn validate_generated_references(document: &ObjectValue, codes: &mut BTreeSet<String>) {
    let identities = collect_identities(document, &mut BTreeSet::new());
    let Some(snapshot) = entity_spec("Snapshot") else {
        return;
    };
    for root_property in snapshot.properties {
        if root_property.kind != PropertyKind::EntityArray {
            continue;
        }
        let Some(entity_name) = root_property.target else {
            continue;
        };
        let Some(specification) = entity_spec(entity_name) else {
            continue;
        };
        for item in object_items(document, root_property.json_name) {
            validate_entity_references(item, specification, &identities, codes);
        }
    }
}

fn validate_entity_references(
    entity: &ObjectValue,
    specification: &EntitySpec,
    identities: &BTreeMap<&'static str, BTreeSet<String>>,
    codes: &mut BTreeSet<String>,
) {
    for property in specification.properties {
        if !matches!(
            property.kind,
            PropertyKind::Reference | PropertyKind::ReferenceArray
        ) {
            continue;
        }
        let Some(target) = property.target else {
            continue;
        };
        let Some(known) = identities.get(target) else {
            continue;
        };
        let unknown = match property.kind {
            PropertyKind::Reference => string_value(entity, property.json_name)
                .is_some_and(|identifier| !known.contains(identifier)),
            PropertyKind::ReferenceArray => string_items(entity, property.json_name)
                .iter()
                .any(|identifier| !known.contains(*identifier)),
            _ => false,
        };
        if unknown {
            codes.insert(format!(
                "UNKNOWN_REFERENCE:{}.{}",
                specification.name, property.json_name
            ));
        }
    }
}

fn validate_accounting(
    document: &ObjectValue,
    inventory_domains: &BTreeSet<String>,
    units: &BTreeSet<String>,
    codes: &mut BTreeSet<String>,
) {
    let dispositions = BTreeSet::from([
        "represented",
        "residual",
        "unsupported",
        "unreadable",
        "policy-excluded",
        "duplicate",
    ]);
    let mut source_keys: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for item in object_items(document, "accountingItems") {
        let domain_id = string_value(item, "inventoryDomainId").unwrap_or_default();
        if !inventory_domains.contains(domain_id) {
            codes.insert("UNKNOWN_INVENTORY_DOMAIN_REF".to_owned());
        }
        let source_key = string_value(item, "sourceKey");
        match source_key {
            Some(source_key) if !source_key.is_empty() => {
                if !source_keys
                    .entry(domain_id.to_owned())
                    .or_default()
                    .insert(source_key.to_owned())
                {
                    codes.insert("ACCOUNTING_DUPLICATE_SOURCE_KEY".to_owned());
                }
            }
            _ => {
                codes.insert("ACCOUNTING_SOURCE_KEY_REQUIRED".to_owned());
            }
        }
        if string_value(item, "disposition").is_none_or(|value| !dispositions.contains(value)) {
            codes.insert("ACCOUNTING_DISPOSITION_INVALID".to_owned());
        }
        for unit_id in string_items(item, "unitIds") {
            if !units.contains(unit_id) {
                codes.insert("UNKNOWN_ACCOUNTING_UNIT_REF".to_owned());
            }
        }
    }

    let domains: BTreeMap<&str, &ObjectValue> = object_items(document, "inventoryDomains")
        .filter_map(|domain| {
            string_value(domain, "inventoryDomainId").map(|identifier| (identifier, domain))
        })
        .collect();
    for (domain_id, domain) in &domains {
        if let Some(expected) = integer_value(domain, "expectedCount") {
            let actual = source_keys.get(*domain_id).map_or(0, BTreeSet::len) as u64;
            if expected != actual {
                codes.insert("ACCOUNTING_COUNT_MISMATCH".to_owned());
            }
        }
    }
    for receipt in object_items(document, "censusReceipts") {
        let domain_id = string_value(receipt, "inventoryDomainId").unwrap_or_default();
        if !inventory_domains.contains(domain_id) {
            codes.insert("UNKNOWN_RECEIPT_DOMAIN_REF".to_owned());
        }
        if let Some(expected) = domains
            .get(domain_id)
            .and_then(|domain| integer_value(domain, "expectedCount"))
            && integer_value(receipt, "observedCount") != Some(expected)
        {
            codes.insert("CENSUS_COUNT_MISMATCH".to_owned());
        }
    }
}

fn validate_equivalence(document: &ObjectValue, codes: &mut BTreeSet<String>) {
    let guarantees: BTreeMap<&str, &ObjectValue> = object_items(document, "guaranteeStatuses")
        .filter_map(|status| {
            string_value(status, "guaranteeStatusId").map(|identifier| (identifier, status))
        })
        .collect();
    for certificate in object_items(document, "equivalenceCertificates") {
        let coverage_ids = string_items(certificate, "coverageStatusIds");
        let mut coverage = Vec::new();
        let mut invalid_reference = coverage_ids.is_empty();
        for identifier in coverage_ids {
            match guarantees.get(identifier) {
                Some(status) => coverage.push(*status),
                None => invalid_reference = true,
            }
        }
        if invalid_reference {
            codes.insert("EQUIVALENCE_COVERAGE_REF_INVALID".to_owned());
            continue;
        }
        if string_value(certificate, "outcome") == Some("equivalent")
            && coverage
                .iter()
                .any(|status| string_value(status, "state") != Some("complete"))
        {
            codes.insert("EQUIVALENCE_INSUFFICIENT_COVERAGE".to_owned());
        }
    }
}

fn object_items<'a>(document: &'a ObjectValue, key: &str) -> impl Iterator<Item = &'a ObjectValue> {
    array_value(document, key)
        .into_iter()
        .flatten()
        .filter_map(CanonicalValue::as_object)
}

fn string_items<'a>(document: &'a ObjectValue, key: &str) -> Vec<&'a str> {
    array_value(document, key)
        .into_iter()
        .flatten()
        .filter_map(CanonicalValue::as_str)
        .collect()
}

fn array_value<'a>(document: &'a ObjectValue, key: &str) -> Option<&'a [CanonicalValue]> {
    document.get(key).and_then(CanonicalValue::as_array)
}

fn object_value<'a>(document: &'a ObjectValue, key: &str) -> Option<&'a ObjectValue> {
    document.get(key).and_then(CanonicalValue::as_object)
}

fn string_value<'a>(document: &'a ObjectValue, key: &str) -> Option<&'a str> {
    document.get(key).and_then(CanonicalValue::as_str)
}

fn bool_value(document: &ObjectValue, key: &str) -> Option<bool> {
    document.get(key).and_then(CanonicalValue::as_bool)
}

fn integer_value(document: &ObjectValue, key: &str) -> Option<u64> {
    document
        .get(key)
        .and_then(CanonicalValue::as_number)
        .and_then(crate::JsonNumber::as_u64)
}

fn contains_id(ids: &BTreeSet<String>, document: &ObjectValue, key: &str) -> bool {
    string_value(document, key).is_some_and(|identifier| ids.contains(identifier))
}

#[cfg(test)]
mod tests {
    use super::validate_snapshot_json;

    const POSITIVE: &str = include_str!("../../../fixtures/positive/assertion-first.json");

    #[test]
    fn assertion_first_fixture_validates() -> Result<(), Box<dyn std::error::Error>> {
        let report = validate_snapshot_json(POSITIVE)?;
        assert!(report.is_valid(), "{:?}", report.codes());
        Ok(())
    }
}
