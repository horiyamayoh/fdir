#![forbid(unsafe_code)]

use std::error::Error;
use std::io;

use fdir_contract::{CanonicalValue, validate_snapshot_json, validate_snapshot_value};

const POSITIVE_FIXTURES: &[(&str, &str)] = &[
    (
        "examples/minimal.json",
        include_str!("../../../examples/minimal.json"),
    ),
    (
        "examples/accounted-document.json",
        include_str!("../../../examples/accounted-document.json"),
    ),
    (
        "examples/equivalence-indeterminate.json",
        include_str!("../../../examples/equivalence-indeterminate.json"),
    ),
    (
        "fixtures/positive/assertion-first.json",
        include_str!("../../../fixtures/positive/assertion-first.json"),
    ),
    (
        "fixtures/positive/accounted-document.json",
        include_str!("../../../fixtures/positive/accounted-document.json"),
    ),
    (
        "fixtures/positive/equivalence-indeterminate.json",
        include_str!("../../../fixtures/positive/equivalence-indeterminate.json"),
    ),
    (
        "fixtures/positive/unsupported-visible.json",
        include_str!("../../../fixtures/positive/unsupported-visible.json"),
    ),
    (
        "fixtures/positive/extensions-and-statuses.json",
        include_str!("../../../fixtures/positive/extensions-and-statuses.json"),
    ),
];

const NEGATIVE_FIXTURES: &[(&str, &str, &str)] = &[
    (
        "fixtures/negative/unit-without-assertion.json",
        "UNIT_WITHOUT_ASSERTION",
        include_str!("../../../fixtures/negative/unit-without-assertion.json"),
    ),
    (
        "fixtures/negative/duplicate-accounting-key.json",
        "ACCOUNTING_DUPLICATE_SOURCE_KEY",
        include_str!("../../../fixtures/negative/duplicate-accounting-key.json"),
    ),
    (
        "fixtures/negative/equivalent-with-partial-coverage.json",
        "EQUIVALENCE_INSUFFICIENT_COVERAGE",
        include_str!("../../../fixtures/negative/equivalent-with-partial-coverage.json"),
    ),
    (
        "fixtures/negative/false-production-claim.json",
        "FALSE_PRODUCTION_CLAIM",
        include_str!("../../../fixtures/negative/false-production-claim.json"),
    ),
    (
        "fixtures/negative/unknown-occurrence.json",
        "UNKNOWN_OCCURRENCE_REF",
        include_str!("../../../fixtures/negative/unknown-occurrence.json"),
    ),
];

const NEGATIVE_MANIFEST: &str = include_str!("../../../fixtures/negative/manifest.json");

fn test_error(message: impl Into<String>) -> Box<dyn Error> {
    Box::new(io::Error::other(message.into()))
}

#[test]
fn rust_and_python_oracles_agree_on_shared_fixture_outcomes() -> Result<(), Box<dyn Error>> {
    for (path, input) in POSITIVE_FIXTURES {
        let report = validate_snapshot_json(input)?;
        assert!(report.is_valid(), "{path}: {:?}", report.codes());
    }
    for (path, expected_code, input) in NEGATIVE_FIXTURES {
        let report = validate_snapshot_json(input)?;
        assert!(!report.is_valid(), "{path} unexpectedly validated");
        assert!(
            report.contains(expected_code),
            "{path} did not produce {expected_code}: {:?}",
            report.codes()
        );
    }
    Ok(())
}

#[test]
fn negative_registry_cannot_drift_from_rust_parity_cases() -> Result<(), Box<dyn Error>> {
    let manifest = CanonicalValue::parse_json(NEGATIVE_MANIFEST)?;
    let Some(manifest_object) = manifest.as_object() else {
        return Err(test_error("negative fixture manifest is not an object"));
    };
    let Some(entries) = manifest_object
        .get("fixtures")
        .and_then(CanonicalValue::as_array)
    else {
        return Err(test_error(
            "negative fixture manifest has no fixtures array",
        ));
    };
    let registered: Vec<(&str, &str)> = entries
        .iter()
        .filter_map(CanonicalValue::as_object)
        .filter_map(|entry| {
            Some((
                entry.get("path")?.as_str()?,
                entry.get("expectedCode")?.as_str()?,
            ))
        })
        .collect();
    let expected: Vec<(&str, &str)> = NEGATIVE_FIXTURES
        .iter()
        .map(|(path, code, _)| (*path, *code))
        .collect();
    assert_eq!(registered, expected);
    Ok(())
}

#[test]
fn lossy_projection_never_reconstructs_missing_assertions() -> Result<(), Box<dyn Error>> {
    let input = include_str!("../../../fixtures/positive/assertion-first.json");
    let parsed = CanonicalValue::parse_json(input)?;
    let Some(mut object) = parsed.as_object().cloned() else {
        return Err(test_error("assertion-first fixture is not an object"));
    };
    let projection_before = object.get("acceptedProjections").cloned();
    object.insert("assertions".to_owned(), CanonicalValue::Array(Vec::new()));
    let mutated = CanonicalValue::Object(object);
    let report = validate_snapshot_value(&mutated);
    assert!(report.contains("UNIT_WITHOUT_ASSERTION"));
    assert_eq!(
        mutated
            .as_object()
            .and_then(|value| value.get("acceptedProjections"))
            .cloned(),
        projection_before
    );
    Ok(())
}

#[test]
fn unknown_extensions_survive_dependency_free_round_trip() -> Result<(), Box<dyn Error>> {
    let input = include_str!("../../../fixtures/positive/extensions-and-statuses.json");
    let parsed = CanonicalValue::parse_json(input)?;
    let serialized = parsed.to_json();
    let reparsed = CanonicalValue::parse_json(&serialized)?;
    assert_eq!(parsed, reparsed);
    let report = validate_snapshot_value(&reparsed);
    assert!(report.is_valid(), "{:?}", report.codes());
    Ok(())
}
