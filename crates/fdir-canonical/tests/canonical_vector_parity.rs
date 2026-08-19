#![forbid(unsafe_code)]

use std::error::Error;
use std::io;

use fdir_canonical::{
    CANONICAL_JSON_VERSION, IDENTITY_VERSION, canonical_string, canonicalize_json, content_digest,
    domain_separated_digest,
};
use fdir_core::{CanonicalValue, ObjectValue};

const VECTORS: &str = include_str!("../../../fixtures/canonical/vector.json");

fn object(value: &CanonicalValue) -> Result<&ObjectValue, Box<dyn Error>> {
    value
        .as_object()
        .ok_or_else(|| io::Error::other("vector entry must be an object").into())
}

fn field<'a>(value: &'a ObjectValue, name: &str) -> Result<&'a CanonicalValue, Box<dyn Error>> {
    value
        .get(name)
        .ok_or_else(|| io::Error::other(format!("vector field is missing: {name}")).into())
}

fn text_field<'a>(value: &'a ObjectValue, name: &str) -> Result<&'a str, Box<dyn Error>> {
    field(value, name)?
        .as_str()
        .ok_or_else(|| io::Error::other(format!("vector field must be text: {name}")).into())
}

fn array_field<'a>(
    value: &'a ObjectValue,
    name: &str,
) -> Result<&'a [CanonicalValue], Box<dyn Error>> {
    field(value, name)?
        .as_array()
        .ok_or_else(|| io::Error::other(format!("vector field must be an array: {name}")).into())
}

#[test]
fn rust_matches_every_published_python_vector() -> Result<(), Box<dyn Error>> {
    let manifest_value = CanonicalValue::parse_json(VECTORS)?;
    let manifest = object(&manifest_value)?;
    assert_eq!(
        text_field(manifest, "canonicalizationVersion")?,
        CANONICAL_JSON_VERSION
    );
    assert_eq!(text_field(manifest, "identityVersion")?, IDENTITY_VERSION);

    let legacy_value = field(manifest, "value")?;
    assert_eq!(
        canonical_string(legacy_value)?,
        text_field(manifest, "canonical")?
    );
    assert_eq!(
        content_digest(legacy_value)?.as_str(),
        text_field(manifest, "digest")?
    );

    for value in array_field(manifest, "positive")? {
        let vector = object(value)?;
        let input = text_field(vector, "input")?;
        let canonical = String::from_utf8(canonicalize_json(input)?)?;
        assert_eq!(canonical, text_field(vector, "canonical")?);
        let parsed = CanonicalValue::parse_json(input)?;
        assert_eq!(
            content_digest(&parsed)?.as_str(),
            text_field(vector, "digest")?
        );
    }

    for value in array_field(manifest, "negative")? {
        let vector = object(value)?;
        let input = text_field(vector, "input")?;
        let expected = text_field(vector, "expectedCode")?;
        let result = canonicalize_json(input);
        assert_eq!(
            result
                .as_ref()
                .err()
                .map(fdir_canonical::CanonicalError::code),
            Some(expected)
        );
    }

    let mut identity_digests = Vec::new();
    for value in array_field(manifest, "identity")? {
        let vector = object(value)?;
        let kind = text_field(vector, "kind")?;
        let envelope = field(vector, "envelope")?;
        assert_eq!(
            canonical_string(envelope)?,
            text_field(vector, "canonical")?
        );
        let digest = domain_separated_digest(kind, envelope)?;
        assert_eq!(digest.as_str(), text_field(vector, "digest")?);
        identity_digests.push(digest);
    }
    identity_digests.sort();
    identity_digests.dedup();
    assert_eq!(
        identity_digests.len(),
        array_field(manifest, "identity")?.len()
    );
    Ok(())
}
